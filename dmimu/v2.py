"""Independent implementation of the official September 2026 V2 USB protocol.

Wire layouts were checked against the published NativeAOT host's encoder and
parsers. See docs/V2_PROTOCOL.md for provenance and validation boundaries.
"""
from dataclasses import dataclass
import struct

from .protocol import crc16

READ_VERSION = 0x0E
READ_CONFIGURATION = 0x10
STATIC_START, STATIC_STATUS = 0x11, 0x12
SIX_FACE_CONTROL, SIX_FACE_STATUS = 0x13, 0x14
CAN_BAUDRATES = (1000000, 500000, 400000, 250000, 200000, 100000, 50000, 25000)
UART_BAUDRATES = (9600, 115200, 230400, 460800, 500000, 921600, 1000000, 1500000, 2000000, 2500000, 3000000, 3500000, 4000000)


@dataclass(frozen=True)
class Ack:
    command: int
    code: int
    payload: bytes
    raw: bytes


def request(command, payload=b""):
    if type(command) is not int or not 0 <= command <= 255:
        raise ValueError("Invalid V2 command")
    payload = bytes(payload)
    if len(payload) > 65535:
        raise ValueError("V2 payload is too large")
    body = bytes([command]) + struct.pack("<H", len(payload)) + payload
    return b"\xa5" + body + struct.pack("<H", crc16(body)) + b"\x5a"


def parse_ack(raw):
    if len(raw) < 8 or raw[0] != 0xA5 or raw[-1] != 0x5A:
        return None
    length = int.from_bytes(raw[3:5], "little")
    if length > 32 or len(raw) != length + 8:
        return None
    if crc16(raw[1:-3]) != int.from_bytes(raw[-3:-1], "little"):
        return None
    return Ack(raw[1], raw[2], raw[5:-3], bytes(raw))


def versions(payload):
    if len(payload) != 8:
        raise ValueError("Version response must contain eight bytes")
    return {"boot": list(payload[:4]), "app": list(payload[4:]),
            "boot_text": ".".join(map(str, payload[:4])),
            "app_text": ".".join(map(str, payload[4:]))}


def configuration(payload):
    if len(payload) not in (17, 18, 22):
        raise ValueError("Unsupported V2 configuration length")
    if payload[5] > 3 or payload[16] > 3:
        raise ValueError("Invalid configuration enum")
    interval = int.from_bytes(payload[6:8], "little")
    if not 1 <= interval <= 1000:
        raise ValueError("Invalid output interval")
    result = dict(zip(("acceleration_enabled", "gyro_enabled", "euler_enabled", "quaternion_enabled", "can_active"), map(bool, payload[:5])))
    result.update(interval_ms=interval, heating_enabled=bool(payload[8]),
                  target_temperature=payload[9], slave_id=int.from_bytes(payload[10:12], "little"),
                  master_id=int.from_bytes(payload[12:14], "little"),
                  can_baud_code=payload[14], uart_baud_code=payload[15],
                  communication=payload[5], reserved_mode=payload[16],
                  payload_length=len(payload))
    result["can_baudrate"] = CAN_BAUDRATES[payload[14]] if payload[14] < len(CAN_BAUDRATES) else None
    result["uart_baudrate"] = UART_BAUDRATES[payload[15]] if payload[15] < len(UART_BAUDRATES) else None
    if len(payload) >= 18:
        if payload[17] > 23:
            raise ValueError("Invalid installation rotation")
        result["installation_rotation"] = payload[17]
    if len(payload) == 22:
        if any(value > maximum for value, maximum in zip(payload[18:22], (3, 2, 4, 1))):
            raise ValueError("Invalid extended sensor configuration")
        result.update(accel_range=payload[18], accel_bandwidth=payload[19],
                      gyro_range=payload[20], gyro_bandwidth=payload[21])
    return result


def static_status(payload):
    if len(payload) != 24 or payload[1] > 4 or payload[2] > 3:
        raise ValueError("Invalid static calibration status")
    count, target, stability, threshold = struct.unpack_from("<4H", payload, 4)
    result = {"still": payload[0] == 1, "state": payload[1], "error": payload[2],
              "samples": count, "target_samples": target,
              "stability_deg_s": stability * .01, "threshold_deg_s": threshold * .01,
              "has_result": payload[1] == 3}
    if result["has_result"]:
        result["bias_deg_s"] = [v * .001 for v in struct.unpack_from("<3h", payload, 12)]
        result["noise_deg_s"] = [v * .001 for v in struct.unpack_from("<3H", payload, 18)]
    return result


def six_face_status(payload):
    if (len(payload) != 21 or payload[0] > 4 or payload[1] > 4 or
            payload[2] > 6 or payload[3] not in (*range(6), 255)):
        raise ValueError("Invalid six-face calibration status")
    count, target = struct.unpack_from("<2H", payload, 5)
    return {"state": payload[0], "error": payload[1], "completed_faces": payload[2],
            "current_face": None if payload[3] == 255 else payload[3],
            "has_result": payload[4] == 1, "samples": count, "target_samples": target,
            "bias_m_s2": [v * .001 for v in struct.unpack_from("<3h", payload, 9)],
            "scale": [v * .0001 for v in struct.unpack_from("<3H", payload, 15)]}


def configuration_commands(params, current):
    """Validate an entire patch before creating any hardware writes."""
    flags = {"acceleration_enabled": 0, "gyro_enabled": 1, "euler_enabled": 2,
             "quaternion_enabled": 3, "can_active": 4}
    integer_fields = {"interval_ms": (1, 1000, 4), "target_temperature": (0, 60, 6),
                      "slave_id": (0, 255, 7), "master_id": (0, 255, 8),
                      "communication": (0, 3, 9), "installation_rotation": (0, 23, 0x15),
                      "accel_range": (0, 3, None), "gyro_range": (0, 4, None)}
    allowed = flags.keys() | integer_fields.keys() | {"heating_enabled", "can_baudrate", "uart_baudrate"}
    if not params or set(params) - allowed:
        raise ValueError("无效的 V2 参数名称")
    commands = []
    for name, value in params.items():
        if name in flags or name == "heating_enabled":
            if type(value) is not bool:
                raise ValueError(name + " 必须为布尔值")
            commands.append((3, bytes((flags[name], int(value)))) if name in flags else (5, bytes((int(value),))))
        elif name in ("can_baudrate", "uart_baudrate"):
            table = CAN_BAUDRATES if name == "can_baudrate" else UART_BAUDRATES
            if type(value) is not int or value not in table:
                raise ValueError(name + " 不在设备支持的速率表中")
            commands.append((0x0A if name == "can_baudrate" else 0x0B, bytes((table.index(value),))))
        else:
            low, high, command = integer_fields[name]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} 必须为 {low}–{high} 的整数")
            if name == "installation_rotation" and name not in current:
                raise ValueError("当前固件未报告安装方向能力")
            if command is not None:
                commands.append((command, value.to_bytes(2 if name == "interval_ms" else 1, "little")))
    if "accel_range" in params or "gyro_range" in params:
        if not {"accel_range", "gyro_range"} <= current.keys():
            raise ValueError("当前固件未报告传感器量程能力")
        commands.append((0x16, bytes((params.get("accel_range", current["accel_range"]), params.get("gyro_range", current["gyro_range"])))))
    return commands


BUILD_INFO = 0x19


def build_info(payload):
    """Official host decodes a string then trims NUL/space (1403E2F30-57).

    Preserve bytes rather than infer a date layout or guess non-ASCII encoding.
    """
    payload = bytes(payload)
    if len(payload) > 32:
        raise ValueError("Build information exceeds the verified ACK payload limit")
    trimmed = payload.strip(b"\0 ")
    ascii_text = all(32 <= value <= 126 for value in trimmed)
    return {"text": trimmed.decode("ascii") if ascii_text else None,
            "raw_hex": payload.hex(), "payload_length": len(payload),
            "text_encoding_verified": "ascii-subset" if ascii_text else None}
