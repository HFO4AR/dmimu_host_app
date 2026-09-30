"""Streaming DM-IMU USB CDC decoder. No serial dependency or device writes.

Reference: official DM-IMU-L1 V1.3 appendix E, upstream ba758fd.
Temperature length is inconsistent in the manual; only CRC-validated 19/23
byte variants are accepted, and the observed length is exposed.
"""
from dataclasses import dataclass
import math
import struct

CHANNELS = {1: "acceleration", 2: "angular_velocity", 3: "euler", 4: "quaternion", 5: "temperature", 7: "device_status"}
UNITS = {"acceleration": "m/s²", "angular_velocity": "rad/s", "euler": "deg", "quaternion": "wxyz", "temperature": "°C"}


def crc16(data: bytes) -> int:
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ (0x1021 if value & 0x8000 else 0)) & 0xFFFF
    return value


@dataclass(frozen=True)
class Frame:
    slave_id: int
    kind: int
    values: dict
    raw: bytes

    @property
    def channel(self):
        return CHANNELS[self.kind]


class Decoder:
    def __init__(self, legacy_crc=False):
        self.buffer = bytearray()
        self.legacy_crc = legacy_crc
        self.frames = self.crc_errors = self.discarded_bytes = 0

    def feed(self, chunk: bytes) -> list[Frame]:
        self.buffer.extend(chunk)
        result = []
        while len(self.buffer) >= 4:
            pos = self.buffer.find(b"\x55\xaa")
            if pos < 0:
                keep = 1 if self.buffer[-1] == 0x55 else 0
                self.discarded_bytes += len(self.buffer) - keep
                self.buffer = self.buffer[-keep:] if keep else bytearray()
                break
            if pos:
                self.discarded_bytes += pos
                del self.buffer[:pos]
            kind = self.buffer[3]
            lengths = {1: (19,), 2: (19,), 3: (19,), 4: (23,), 5: (19, 23), 7: (19,)}.get(kind)
            if lengths is None:
                del self.buffer[0]
                self.discarded_bytes += 1
                continue
            matched = None
            for length in lengths:
                if len(self.buffer) < length:
                    continue
                raw = bytes(self.buffer[:length])
                if raw[-1] != 10:
                    continue
                wire = struct.unpack_from("<H", raw, length - 3)[0]
                if crc16(raw[:-3]) == wire or (self.legacy_crc and crc16(raw[2:-3]) == wire):
                    matched = raw
                    break
            if matched is None:
                if len(self.buffer) < max(lengths):
                    break
                self.crc_errors += 1
                self.discarded_bytes += 1
                del self.buffer[0]
                continue
            del self.buffer[:len(matched)]
            values = self.decode(kind, matched[4:-3])
            if values is None:
                self.discarded_bytes += len(matched)
                continue
            result.append(Frame(matched[2], kind, values, matched))
            self.frames += 1
        return result

    @staticmethod
    def decode(kind, payload):
        if kind in (1, 2, 3, 4):
            count = 4 if kind == 4 else 3
            vals = struct.unpack_from("<" + "f" * count, payload)
            if not all(math.isfinite(v) for v in vals):
                return None
            if kind == 4 and not 0.5 < sum(v * v for v in vals) < 1.5:
                return None
            return dict(zip(("w", "x", "y", "z") if kind == 4 else (("roll", "pitch", "yaw") if kind == 3 else ("x", "y", "z")), vals))
        if kind == 5:
            target, current, interval = struct.unpack_from("<ffH", payload)
            if not math.isfinite(target) or not math.isfinite(current):
                return None
            return {"target": target, "current": current, "interval_ms": interval}
        slave, master = struct.unpack_from("<HH", payload)
        keys = ("gyro_stable", "accel_stable", "acceleration_enabled", "gyro_enabled", "euler_enabled", "quaternion_enabled", "heating_enabled", "communication")
        return {"slave_id": slave, "master_id": master, **dict(zip(keys, payload[4:12]))}


def encode_frame(kind, values, slave_id=1):
    """Fixture/demo encoder, never a USB control command."""
    data = b"\x55\xaa" + bytes((slave_id, kind)) + struct.pack("<" + "f" * len(values), *values)
    return data + struct.pack("<H", crc16(data)) + b"\x0a"
