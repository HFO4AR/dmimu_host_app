"""Versioned raw recordings, deterministic replay and SI-unit CSV export."""
import base64
import csv
import json
from pathlib import Path
import re
import time
import uuid

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

    def start(self, source, port, legacy_crc=False):
        if self.writer:
            raise ValueError("已有录制正在进行")
        identifier = uuid.uuid4().hex
        self.writer = self.path(identifier).open("x", encoding="utf-8")
        self.active = identifier
        self.started = time.monotonic()
        self.bytes = self.chunks = 0
        self.writer.write(json.dumps({"format": "dmimu.raw", "version": 1, "created_at": time.time(), "source": source, "port": port, "legacy_crc": legacy_crc}, ensure_ascii=False) + "\n")
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
