"""Versioned raw recordings, deterministic replay and SI-unit CSV export."""
import base64
import csv
import json
import hashlib
import math
import os
from pathlib import Path
import re
import struct
import time
import uuid
from datetime import datetime, timezone

from .protocol import Decoder


class Recordings:
    def __init__(self, directory):
        self.directory = Path(directory) / "recordings"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        self.writer = None
        self.active = None
        self.started = 0
        self.bytes = self.chunks = 0

    def path(self, identifier, extension="dmimulog"):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier or ""):
            raise ValueError("无效的录制 ID")
        return self.directory / f"{identifier}.{extension}"

    def start(self, source, port, legacy_crc=False, device=None):
        if self.writer:
            raise ValueError("已有录制正在进行")
        identifier = uuid.uuid4().hex
        self.writer = self.path(identifier).open("x", encoding="utf-8")
        self.active = identifier
        self.started = time.monotonic()
        self.bytes = self.chunks = 0
        self.writer.write(json.dumps({"format": "dmimu.raw", "version": 1, "created_at": time.time(), "source": source, "port": port, "legacy_crc": legacy_crc, "device": device or {}}, ensure_ascii=False) + "\n")
        self.writer.flush()
        return identifier

    def append(self, raw, received_at):
        if not self.writer:
            return
        self.writer.write(json.dumps({"time": received_at, "elapsed": time.monotonic() - self.started, "data": base64.b64encode(raw).decode("ascii")}, separators=(",", ":")) + "\n")
        self.bytes += len(raw)
        self.chunks += 1
        if self.chunks % 50 == 0:
            self.writer.flush()

    def stop(self):
        if not self.writer:
            raise ValueError("没有进行中的录制")
        identifier = self.active
        self.writer.flush()
        self.writer.close()
        self.writer = None
        self.active = None
        return identifier

    def listing(self):
        result = []
        for path in sorted(self.directory.glob("*.dmimulog"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with path.open(encoding="utf-8") as src:
                    meta = json.loads(src.readline())
                result.append({"id": path.stem, "created_at": meta["created_at"], "source": meta["source"], "size": path.stat().st_size, "active": path.stem == self.active})
            except (OSError, ValueError, KeyError):
                continue
        return result

    def samples(self, identifier):
        if identifier == self.active:
            raise ValueError("先停止录制，再进行回放或导出")
        with self.path(identifier).open(encoding="utf-8") as src:
            header = json.loads(src.readline())
            if header.get("format") != "dmimu.raw" or header.get("version") != 1:
                raise ValueError("不支持的录制格式")
            decoder = Decoder(legacy_crc=header.get("legacy_crc", False))
            previous = -1
            for line in src:
                try:
                    item = json.loads(line)
                except ValueError:
                    # Recover only an interrupted last write, never skip interior corruption.
                    if src.read(1):
                        raise ValueError("录制文件中间存在损坏数据") from None
                    break
                elapsed = float(item["elapsed"])
                if elapsed < previous:
                    raise ValueError("录制时间戳顺序无效")
                previous = elapsed
                for frame in decoder.feed(base64.b64decode(item["data"], validate=True)):
                    yield elapsed, float(item["time"]), frame

    def import_stream(self, incoming, maximum=1024 * 1024 * 1024):
        """Validate before publishing; imports are deduplicated by content hash."""
        from tempfile import TemporaryFile, NamedTemporaryFile
        digest = hashlib.sha256()
        with TemporaryFile(dir=self.directory) as source:
            total = 0
            while chunk := incoming.read(65536):
                total += len(chunk)
                if total > maximum:
                    raise ValueError("录制文件最多 1 GiB")
                digest.update(chunk); source.write(chunk)
            if not total:
                raise ValueError("录制文件为空")
            source.seek(0)
            magic = source.read(8); source.seek(0)
            official = magic == b"IMULOG01"
            if official:
                source.seek(8)
                raw = source.read(4)
                if len(raw) != 4:
                    raise ValueError("官方录制头部不完整")
                length = struct.unpack("<i", raw)[0]
                if not 1 <= length <= 16384:
                    raise ValueError("官方录制元数据长度无效")
                raw = source.read(length)
                if len(raw) != length:
                    raise ValueError("官方录制元数据不完整")
                original = json.loads(raw)
                if not isinstance(original, dict) or original.get("Protocol") != "bosch-api-v1" or original.get("TimeBasis") != "host-receive-seconds":
                    raise ValueError("仅支持官方 bosch-api-v1 / host-receive-seconds 录制")
                started = original.get("StartedUtc")
                try:
                    date = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    if date.tzinfo is None: raise ValueError()
                    origin = date.timestamp()
                except (ValueError, TypeError, AttributeError, OverflowError):
                    raise ValueError("官方录制缺少有效 StartedUtc，不能构造真实接收时间") from None
                if not math.isfinite(origin): raise ValueError("官方录制起始时间无效")
                header = dict(format="dmimu.raw", version=1, created_at=origin,
                              source="import", port=original.get("Source", ""), legacy_crc=False,
                              imported_format="IMULOG01", original_metadata=original)
            else:
                line = source.readline(65537)
                if len(line) > 65536: raise ValueError("录制头部过大")
                header = json.loads(line)
                if not isinstance(header, dict) or header.get("format") != "dmimu.raw" or header.get("version") != 1:
                    raise ValueError("支持 .dmimulog 或官方 IMULOG01 .imulog")
                if type(header.get("legacy_crc", False)) is not bool:
                    raise ValueError("无效 CRC 设置")
                if type(header.get("created_at")) not in (int, float) or not math.isfinite(header["created_at"]):
                    raise ValueError("录制起始时间无效")
            decoder = Decoder(legacy_crc=header.get("legacy_crc", False))
            identifier = digest.hexdigest()[:32]
            frames = blocks = 0; previous = -1.; complete = not official
            temporary = None
            try:
                with NamedTemporaryFile("w", encoding="utf-8", dir=self.directory, delete=False) as dst:
                    temporary = Path(dst.name)
                    # No embedded credentials/settings are interpreted on import.
                    dst.write(json.dumps(header, ensure_ascii=False, allow_nan=False) + "\n")
                    while True:
                        if official:
                            raw = source.read(12)
                            if not raw: break
                            if len(raw) != 12:
                                complete = False; break
                            elapsed, length = struct.unpack("<di", raw)
                            if not math.isfinite(elapsed) or elapsed < previous or elapsed < 0:
                                raise ValueError("官方录制时间戳无效")
                            if length == -1:
                                count = source.read(8)
                                if len(count) != 8 or struct.unpack("<Q", count)[0] != blocks or source.read(1):
                                    raise ValueError("官方录制结束标记或块数无效")
                                complete = True; break
                            if not 1 <= length <= 65536: raise ValueError("官方录制块长度无效")
                            raw = source.read(length)
                            if len(raw) != length:
                                complete = False; break
                            stamp = origin + elapsed
                        else:
                            line = source.readline(131073)
                            if not line: break
                            if len(line) > 131072: raise ValueError("录制数据行过大")
                            try: item = json.loads(line)
                            except ValueError:
                                if source.read(1): raise ValueError("录制文件中间损坏") from None
                                complete = False; break
                            if not isinstance(item, dict): raise ValueError("录制数据块必须是对象")
                            elapsed, stamp = item["elapsed"], item["time"]
                            if any(type(v) not in (int, float) or not math.isfinite(v) for v in (elapsed, stamp)) or elapsed < previous or elapsed < 0:
                                raise ValueError("录制时间戳无效")
                            raw = base64.b64decode(item["data"], validate=True)
                            if not 1 <= len(raw) <= 65536: raise ValueError("录制数据块长度无效")
                        previous = elapsed; blocks += 1
                        frames += len(decoder.feed(raw))
                        dst.write(json.dumps(dict(time=stamp, elapsed=elapsed, data=base64.b64encode(raw).decode()), separators=(",", ":")) + "\n")
                    dst.flush(); os.fsync(dst.fileno())
                if not frames:
                    raise ValueError("没有 CRC 有效的 IMU 数据帧；旧版 CRC 文件需使用已确认的兼容设置")
                destination = self.path(identifier)
                if not destination.exists(): temporary.replace(destination)
                return dict(id=identifier, samples=frames, blocks=blocks, duration_s=max(0, previous),
                            imported_format="IMULOG01" if official else "dmimu.raw", complete=complete,
                            warning=None if complete else "文件未正常关闭，仅导入尾部之前的完整数据块")
            finally:
                if temporary: temporary.unlink(missing_ok=True)

    def official_export(self, identifier):
        """Stream the official binary recording container without changing raw bytes."""
        if identifier == self.active: raise ValueError("先停止录制")
        with self.path(identifier).open(encoding="utf-8") as source:
            header = json.loads(source.readline())
            if header.get("format") != "dmimu.raw" or header.get("version") != 1:
                raise ValueError("不支持的录制格式")
            origin = header["created_at"]
            metadata = dict(StartedUtc=datetime.fromtimestamp(origin, timezone.utc).isoformat(),
                            Source=header.get("port") or header.get("source", ""), BaudRate=921600,
                            Protocol="bosch-api-v1", TimeBasis="host-receive-seconds")
            encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
            yield b"IMULOG01" + struct.pack("<i", len(encoded)) + encoded
            blocks = 0; previous = 0
            for line in source:
                item = json.loads(line); raw = base64.b64decode(item["data"], validate=True)
                elapsed = float(item["elapsed"])
                if not math.isfinite(elapsed) or elapsed < previous or not 1 <= len(raw) <= 65536:
                    raise ValueError("录制时间或数据块无效")
                yield struct.pack("<di", elapsed, len(raw)) + raw
                blocks += 1; previous = elapsed
            yield struct.pack("<diQ", previous, -1, blocks)

    def export(self, identifier, cancel=None):
        path = self.path(identifier, "csv")
        temporary = path.with_suffix(".csv.tmp")
        count = 0
        try:
            with temporary.open("w", encoding="utf-8-sig", newline="") as out:
                writer = csv.writer(out)
                writer.writerow(["host_time_unix_s", "elapsed_s", "slave_id", "channel", "unit", "x", "y", "z", "roll", "pitch", "yaw", "w", "target", "current", "interval_ms"])
                from .protocol import UNITS
                for elapsed, stamp, frame in self.samples(identifier):
                    if cancel is not None and cancel.is_set():
                        raise ValueError("导出已取消")
                    writer.writerow([stamp, elapsed, frame.slave_id, frame.channel, UNITS.get(frame.channel, ""), *[frame.values.get(k, "") for k in ("x", "y", "z", "roll", "pitch", "yaw", "w", "target", "current", "interval_ms")]])
                    count += 1
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return {"id": identifier, "samples": count, "filename": path.name}
