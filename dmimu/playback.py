"""Bounded-memory raw recording replay with byte-offset decoder checkpoints.

Only the index is cached. Raw chunks stay in the original file; elapsed and
host timestamps are never regenerated from playback speed or wall clock.
"""
import base64
import bisect
from collections import deque
from dataclasses import dataclass
import json
import math
from pathlib import Path

from .protocol import Decoder


@dataclass(frozen=True)
class Checkpoint:
    elapsed: float
    offset: int
    residual: bytes
    latest: tuple


class StreamingPlayback:
    MAX_LINE = 131072
    MAX_RAW = 65536
    MAX_CHECKPOINTS = 4096
    MIN_STRIDE = 256 * 1024

    def __init__(self, path, *, cancel=None, progress=None, checkpoint_bytes=None):
        self.path = Path(path)
        self.signature = self._signature()
        size = self.signature[2]
        self.stride = max(self.MIN_STRIDE if checkpoint_bytes is None else int(checkpoint_bytes),
                          math.ceil(size / self.MAX_CHECKPOINTS), 1)
        self.sample_count = 0
        self.first_elapsed = self.duration = None
        self.primary_slave_id = None
        self.channels = set()
        self.checkpoints = []
        latest = {}
        with self.path.open('rb') as source:
            line = source.readline(65537)
            if len(line) > 65536:
                raise ValueError('录制头部过大')
            header = json.loads(line)
            if not isinstance(header, dict) or header.get('format') != 'dmimu.raw' or header.get('version') != 1:
                raise ValueError('不支持的录制格式')
            self.header = header
            device = header.get('device')
            configuration = device.get('configuration') if isinstance(device, dict) else None
            configured = configuration.get('slave_id') if isinstance(configuration, dict) else None
            if type(configured) is int and 0 <= configured <= 255:
                self.primary_slave_id = configured
            self.legacy_crc = header.get('legacy_crc', False)
            if type(self.legacy_crc) is not bool:
                raise ValueError('无效 CRC 设置')
            decoder = Decoder(self.legacy_crc)
            previous = -1.
            next_checkpoint = source.tell() + self.stride
            self.checkpoints.append(Checkpoint(previous, source.tell(), b'', ()))
            while True:
                if cancel is not None and (cancel.is_set() if hasattr(cancel, 'is_set') else cancel()):
                    raise ValueError('回放索引已取消')
                offset = source.tell()
                if offset >= next_checkpoint:
                    self.checkpoints.append(Checkpoint(previous, offset, bytes(decoder.buffer), tuple(latest.values())))
                    next_checkpoint = offset + self.stride
                block = self._read_block(source, previous)
                if block is None:
                    break
                elapsed, stamp, raw = block
                previous = elapsed
                frames = decoder.feed(raw)
                for frame in frames:
                    if self.primary_slave_id is None:
                        self.primary_slave_id = frame.slave_id
                    self.channels.add(frame.channel)
                    if frame.slave_id == self.primary_slave_id:
                        latest[frame.channel] = (elapsed, stamp, frame)
                if frames:
                    if self.first_elapsed is None:
                        self.first_elapsed = elapsed
                    self.duration = elapsed
                    self.sample_count += len(frames)
                if progress:
                    progress({'stage': 'index_recording', 'bytes': source.tell(), 'total_bytes': size,
                              'samples': self.sample_count, 'fraction': min(1., source.tell() / max(1, size))})
        if not self.sample_count:
            raise ValueError('录制中没有有效帧')
        self._check_file()
        self.checkpoint_times = [point.elapsed for point in self.checkpoints]
        self._offset = self.checkpoints[0].offset
        self._previous = -1.
        self._decoder = Decoder(self.legacy_crc)
        self._pending = None
        self._eof = False
        # No persistent OS file descriptor to leak when switching replay source.

    def _signature(self):
        stat = self.path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _check_file(self):
        if self._signature() != self.signature:
            raise ValueError('录制文件在回放期间发生变化，请重新打开')

    @classmethod
    def _read_block(cls, source, previous):
        line = source.readline(cls.MAX_LINE + 1)
        if not line:
            return None
        if len(line) > cls.MAX_LINE:
            raise ValueError('录制数据行过大')
        try:
            item = json.loads(line)
        except ValueError:
            # Match interrupted recording recovery: ignore an incomplete final
            # JSON write, but never silently skip interior corruption.
            if source.read(1):
                raise ValueError('录制文件中间存在损坏数据') from None
            return None
        if not isinstance(item, dict):
            raise ValueError('录制数据块必须是对象')
        elapsed, stamp = item.get('elapsed'), item.get('time')
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (elapsed, stamp)) or elapsed < 0 or elapsed < previous:
            raise ValueError('录制时间戳顺序无效')
        try:
            raw = base64.b64decode(item['data'], validate=True)
        except (ValueError, TypeError, KeyError):
            raise ValueError('录制原始数据编码无效') from None
        if not 1 <= len(raw) <= cls.MAX_RAW:
            raise ValueError('录制数据块长度无效')
        return float(elapsed), float(stamp), raw

    def _load(self, source):
        block = self._read_block(source, self._previous)
        self._offset = source.tell()
        if block is None:
            self._eof = True
            return False
        elapsed, stamp, raw = block
        self._previous = elapsed
        self._pending = (elapsed, stamp, deque(self._decoder.feed(raw)))
        return True

    def seek(self, position):
        """Return latest primary-slave samples <= position; retain next chunk.

        At equal elapsed times all complete chunks belong to the closed seek
        boundary. Partial frames crossing the checkpoint are decoded once.
        """
        if not math.isfinite(position) or not 0 <= position <= self.duration:
            raise ValueError('无效回放位置')
        self._check_file()
        checkpoint = self.checkpoints[max(0, bisect.bisect_right(self.checkpoint_times, position) - 1)]
        self._offset, self._previous = checkpoint.offset, checkpoint.elapsed
        self._decoder = Decoder(self.legacy_crc)
        self._decoder.buffer.extend(checkpoint.residual)
        self._pending, self._eof = None, False
        latest = {sample[2].channel: sample for sample in checkpoint.latest}
        with self.path.open('rb') as source:
            source.seek(self._offset)
            while self._load(source):
                elapsed, stamp, frames = self._pending
                if elapsed > position:
                    break
                for frame in frames:
                    if frame.slave_id == self.primary_slave_id:
                        latest[frame.channel] = (elapsed, stamp, frame)
                self._pending = None
        return sorted(latest.values(), key=lambda sample: (sample[0], sample[1]))

    def advance(self, position, maximum=4096):
        """Yield at most maximum due frames and report whether target is drained.

        A 64 KiB input block and its decoded frames are the only lookahead.
        Extra due frames remain pending for a subsequent bounded tick.
        """
        if maximum < 1:
            raise ValueError('回放批次帧数必须为正数')
        self._check_file()
        result = []
        blocks = scanned_bytes = 0
        with self.path.open('rb') as source:
            source.seek(self._offset)
            while len(result) < maximum:
                if self._pending is None:
                    if self._eof:
                        return result, True
                    if blocks >= 1024 or scanned_bytes >= 1024 * 1024:
                        return result, False
                    before = self._offset
                    if not self._load(source):
                        return result, True
                    blocks += 1
                    scanned_bytes += self._offset - before
                elapsed, stamp, frames = self._pending
                if elapsed > position:
                    return result, True
                while frames and len(result) < maximum:
                    result.append((elapsed, stamp, frames.popleft()))
                if frames:
                    return result, False
                self._pending = None
        # A conservative False costs a final empty tick, but never truncates
        # the last block when the file ends exactly at a batch boundary.
        return result, False
