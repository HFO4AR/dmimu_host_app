"""Damiao firmware package/boot protocol, reconstructed from the official host.

This module never opens a serial port. UpgradeSession is a deterministic protocol
state machine: the caller owns transport, authorization and reconnection. Native
packet layout is verified; actual flashing has not been exercised on hardware.
"""
from dataclasses import dataclass
import hashlib
import math
import struct
import time

from .protocol import crc16

PAGE_SIZE = 4096
MAX_PAGES = 255
FOOTER_SIZE = 18
MAX_PACKAGE_SIZE = PAGE_SIZE * MAX_PAGES + FOOTER_SIZE
ENTER_UPGRADE = bytes.fromhex("AA 06 02 0D")
LEGACY_READ_VERSION = bytes.fromhex("AA 07 55 0D")
REBOOT = bytes.fromhex("AA 00 00 0D")
PROTOCOL_EVIDENCE = "official-native-host-2026-09-24"


def crc16_native_legacy(data):
    """Official older host algorithm; its table is CCITT but shift is ONE bit.

    Native addresses 1403732a0 (table generation) and 140373320 (fold).
    This is intentionally not described as a standard CCITT CRC.
    """
    table = []
    for value in range(256):
        value <<= 8
        for _ in range(8):
            value = ((value << 1) ^ (0x1021 if value & 0x8000 else 0)) & 0xffff
        table.append(value)
    crc = 0xffff
    for value in data:
        crc = ((crc << 1) ^ table[(crc >> 8) ^ value]) & 0xffff
    return crc


class FirmwareError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def version_tuple(version):
    if isinstance(version, str):
        parts = version.strip().lstrip("vV").split(".")
        if len(parts) != 4 or any(not p.isdecimal() for p in parts):
            raise FirmwareError("INVALID_VERSION", "版本必须为四段数字，例如 2.0.3.0")
        version = tuple(int(p) for p in parts)
    if (not isinstance(version, (tuple, list)) or len(version) != 4
            or any(type(v) is not int or not 0 <= v <= 255 for v in version)):
        raise FirmwareError("INVALID_VERSION", "版本必须包含四个 0 至 255 的整数")
    return tuple(version)


def version_string(version):
    return ".".join(map(str, version))


@dataclass(frozen=True)
class FirmwareImage:
    """Opaque firmware body and metadata; no decryption or repacking."""
    body: bytes
    app_version: tuple
    boot_version: tuple
    release: str
    metadata: bytes
    file_size: int
    sha256: str

    @classmethod
    def from_bytes(cls, content):
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise FirmwareError("INVALID_PACKAGE", "固件文件必须为二进制")
        content = bytes(content)
        if len(content) <= FOOTER_SIZE:
            raise FirmwareError("EMPTY_FIRMWARE", "固件内容为空或缺少尾部元数据")
        if len(content) > MAX_PACKAGE_SIZE:
            raise FirmwareError("FIRMWARE_TOO_LARGE", "固件最多支持 255 个 4096 字节分页")
        footer = content[-FOOTER_SIZE:]
        if footer[:2] != b"\x55\xaa":
            raise FirmwareError("INVALID_FOOTER", "文件尾部缺少达妙固件标记 55 AA")
        try:
            release = footer[10:18].decode("ascii").rstrip("\x00 ")
        except UnicodeDecodeError as error:
            raise FirmwareError("INVALID_RELEASE", "固件发行信息不是 ASCII") from error
        if any(ord(c) < 32 or ord(c) > 126 for c in release):
            raise FirmwareError("INVALID_RELEASE", "固件发行信息包含控制字符")
        return cls(content[:-FOOTER_SIZE], tuple(footer[2:6]), tuple(footer[6:10]), release,
                   footer[2:], len(content), hashlib.sha256(content).hexdigest())

    @property
    def page_count(self):
        return (len(self.body) + PAGE_SIZE - 1) // PAGE_SIZE

    @property
    def padded_size(self):
        return self.page_count * PAGE_SIZE

    def page(self, index):
        if type(index) is not int or not 1 <= index <= self.page_count:
            raise FirmwareError("INVALID_PAGE", "分页序号从 1 开始，不能超过文件分页总数")
        data = self.body[(index - 1) * PAGE_SIZE:index * PAGE_SIZE]
        return data + b"\xff" * (PAGE_SIZE - len(data))

    def metadata_packet(self):
        return b"\xaa\xdd" + self.metadata + b"\x0d"

    def erase_packet(self):
        # Native builder takes file length modulo 65536, INCLUDING footer.
        # This field is not a whole-image CRC.
        return b"\xaa\xee" + bytes([self.page_count]) + struct.pack("<H", self.file_size & 0xffff) + b"\x0d"

    def page_packet(self, index, crc_mode="ccitt-false"):
        checksum = {"ccitt-false": crc16, "native-legacy": crc16_native_legacy}.get(crc_mode)
        if checksum is None:
            raise FirmwareError("UNVERIFIED_BOOT_CRC", "Boot CRC 模式未知；不能猜测校验算法")
        data = self.page(index)
        return b"\xaa\xff" + bytes([index]) + struct.pack("<H", PAGE_SIZE) + data + struct.pack("<H", checksum(data)) + b"\x0d"

    def upgrade_guard(self, current_version):
        current = version_tuple(current_version)
        if current >= (2, 0, 0, 0) and self.app_version < (2, 0, 0, 0):
            raise FirmwareError("V2_DOWNGRADE_FORBIDDEN", "2.x 固件不能降级为 1.x")
        if self.app_version <= current:
            raise FirmwareError("VERSION_NOT_NEWER", "固件版本必须严格高于设备当前版本")
        return current

    def inspect(self, current_version=None):
        allowed = None
        blocker = None
        if current_version is not None:
            try:
                self.upgrade_guard(current_version)
                allowed = True
            except FirmwareError as error:
                allowed = False
                blocker = {"code": error.code, "message": str(error)}
        return {"app_version": version_string(self.app_version), "boot_version": version_string(self.boot_version),
                "release": self.release, "file_size": self.file_size, "body_size": len(self.body),
                "page_count": self.page_count, "padded_size": self.padded_size, "page_size": PAGE_SIZE,
                "sha256": self.sha256, "upgrade_allowed": allowed, "blocker": blocker,
                "protocol_evidence": PROTOCOL_EVIDENCE, "hardware_flash_verified": False,
                "package_authenticated": False, "bitmap_covers_all_pages": self.page_count <= 24}


@dataclass(frozen=True)
class BootAck:
    device_id: int
    command: int
    value: int
    bitmap: int
    raw: bytes


class BootAckDecoder:
    """8-byte boot ACKs; separate from 19/23-byte measurement frames.

    Official parser does not attach/check a CRC to these ACKs. This decoder must
    only be enabled while the single serial owner is in an explicit boot session.
    """
    def __init__(self):
        self.buffer = bytearray()
        self.discarded = 0

    def clear(self):
        self.buffer.clear()

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while self.buffer:
            start = self.buffer.find(b"\x55\xaa")
            if start < 0:
                keep = int(self.buffer[-1] == 0x55)
                self.discarded += len(self.buffer) - keep
                self.buffer[:] = self.buffer[-keep:] if keep else b""
                break
            if start:
                self.discarded += start
                del self.buffer[:start]
            if len(self.buffer) < 4:
                break
            if self.buffer[3] not in (0xaa, 0xbb, 0xcc):
                self.discarded += 1
                del self.buffer[0]
                continue
            if len(self.buffer) < 8:
                break
            if self.buffer[7] != 0x0a:
                self.discarded += 1
                del self.buffer[0]
                continue
            raw = bytes(self.buffer[:8]); del self.buffer[:8]
            result.append(BootAck(raw[2], raw[3], raw[4], int.from_bytes(raw[4:7], "big") if raw[3] == 0xcc else 0, raw))
        return result


@dataclass(frozen=True)
class Outbound:
    packet: bytes
    stage: str
    page: int
    attempt: int
    timeout: float


class UpgradeSession:
    """ACK-driven boot transaction, without access to actual hardware.

    start/feed/tick return an Outbound packet for the authorized serial owner.
    Before each send, discard prior RX ACKs (native host does the same). A batch
    of generic AA ACKs cannot advance several stages without new transmissions.
    Completion requires reconnect-time version readback, not just write ACKs.
    """
    def __init__(self, image, current_version, device_id, *, max_attempts=5, timeout=2.0, erase_timeout=15.0, crc_mode=None):
        if not isinstance(image, FirmwareImage):
            raise FirmwareError("INVALID_PACKAGE", "先解析固件文件")
        self.current_version = image.upgrade_guard(current_version)
        if crc_mode is None:
            if self.current_version[0] < 2:
                raise FirmwareError("UNVERIFIED_BOOT_CRC", "1.x 设备须从有效版本/测量帧确认 Boot CRC 模式")
            crc_mode = "ccitt-false"
        if crc_mode not in ("ccitt-false", "native-legacy") or (self.current_version[0] >= 2 and crc_mode != "ccitt-false"):
            raise FirmwareError("UNVERIFIED_BOOT_CRC", "Boot CRC 模式与设备协议不匹配")
        self.crc_mode = crc_mode
        if type(device_id) is not int or not 0 <= device_id <= 255:
            raise FirmwareError("INVALID_DEVICE_ID", "升级目标设备地址无效")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise FirmwareError("INVALID_RETRY", "升级发送最多允许 5 次尝试")
        if any(not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) or t <= 0 for t in (timeout, erase_timeout)):
            raise FirmwareError("INVALID_TIMEOUT", "升级超时必须为正数")
        self.image, self.device_id = image, device_id
        self.max_attempts, self.timeout, self.erase_timeout = max_attempts, float(timeout), float(erase_timeout)
        self.decoder = BootAckDecoder()
        self.state = "ready"
        self.page_index = 0
        self.acked_pages = 0
        self.retries = 0
        self.attempt = 0
        self.deadline = None
        self.outbound = None
        self.error = None
        self.bitmap = None
        self.version_verified = False
        self.destructive_started = False

    def _now(self, now):
        return time.monotonic() if now is None else now

    def _send(self, stage, packet, now, *, retry=False):
        self.state = stage
        self.attempt = self.attempt + 1 if retry else 1
        timeout = self.erase_timeout if stage == "erasing" else self.timeout
        self.deadline = now + timeout
        self.outbound = Outbound(packet, stage, self.page_index, self.attempt, timeout)
        self.decoder.clear()
        return self.outbound

    def start(self, now=None):
        if self.state != "ready":
            raise FirmwareError("UPGRADE_ALREADY_STARTED", "升级会话已经开始，不能重放入口指令")
        self.destructive_started = True
        return self._send("entering", ENTER_UPGRADE, self._now(now))

    def _fail(self, code, message):
        self.state = "failed"
        self.error = {"code": code, "message": message}
        self.outbound = None
        self.deadline = None

    def _handle(self, ack, now):
        if ack.device_id != self.device_id:
            return None, False
        if self.state in {"entering", "announcing", "erasing"} and ack.command == 0xaa:
            if self.state == "entering":
                return self._send("announcing", self.image.metadata_packet(), now), True
            if self.state == "announcing":
                return self._send("erasing", self.image.erase_packet(), now), True
            self.page_index = 1
            return self._send("writing", self.image.page_packet(1, self.crc_mode), now), True
        if self.state == "writing" and ack.command == 0xbb and ack.value == self.page_index:
            self.acked_pages += 1
            if self.page_index < self.image.page_count:
                self.page_index += 1
                return self._send("writing", self.image.page_packet(self.page_index, self.crc_mode), now), True
            self.state = "waiting_completion"
            self.outbound = None
            self.deadline = now + self.erase_timeout
            return None, True
        if self.state == "waiting_completion" and ack.command == 0xcc:
            self.bitmap = ack.bitmap
            if self.image.page_count <= 24:
                # The native host proves bit count, not bit-to-page ordering.
                # Do not invent a low-bit numbering contract from that check.
                if ack.bitmap.bit_count() < self.image.page_count:
                    self._fail("INCOMPLETE_BITMAP", "设备返回的分页完成位图计数少于文件分页数")
                    return None, True
            # Outer official UI workflow: 3e8b07 -> 3da800 -> 89c5a0 ->
            # RestartDeviceAsync 3e55e0, which emits AA00000D once.
            self.state = "waiting_reconnect"
            self.deadline = None
            self.outbound = Outbound(REBOOT, "rebooting", self.page_index, 1, 0.0)
            return self.outbound, True
        return None, False

    def feed(self, data, now=None):
        now = self._now(now)
        # Timeouts must be processed before a late ACK can advance the session.
        if self.deadline is not None and now >= self.deadline:
            return self.tick(now)
        for ack in self.decoder.feed(data):
            outbound, accepted = self._handle(ack, now)
            if outbound is not None:
                return outbound  # remaining generic ACKs in this batch are stale
            if accepted and self.state in {"failed", "waiting_reconnect"}:
                break
        return None

    def tick(self, now=None):
        now = self._now(now)
        if self.deadline is None or now < self.deadline:
            return None
        if self.state == "waiting_completion":
            self._fail("COMPLETION_TIMEOUT", "未收到设备升级完成信息，写入结果未知")
            return None
        if self.outbound is None:
            return None
        if self.attempt >= self.max_attempts:
            self._fail("ACK_TIMEOUT", f"{self.state} 阶段 {self.attempt} 次尝试后仍无匹配应答")
            return None
        self.retries += 1
        return self._send(self.state, self.outbound.packet, now, retry=True)

    def verify_reconnected_version(self, version):
        if self.state != "waiting_reconnect":
            raise FirmwareError("UPGRADE_NOT_COMPLETE", "设备尚未回报升级写入完成")
        if version_tuple(version) != self.image.app_version:
            self._fail("VERSION_READBACK_MISMATCH", "重连后的固件版本与升级文件不一致")
            return False
        self.state = "succeeded"
        self.outbound = None
        self.version_verified = True
        return True

    def cancel(self):
        if self.state in {"succeeded", "failed", "cancelled"}:
            return
        self.state = "cancelled"
        self.deadline = None
        self.outbound = None
        self.error = {"code": "UPGRADE_CANCELLED", "message": "升级已停止发送；已写入部分无法自动撤回" if self.destructive_started else "升级已取消"}

    def snapshot(self):
        return {"state": self.state, "page": self.page_index, "acked_pages": self.acked_pages,
                "total_pages": self.image.page_count, "progress": self.acked_pages / self.image.page_count,
                "attempt": self.attempt, "retries": self.retries, "error": self.error,
                "bitmap": self.bitmap, "bitmap_covers_all_pages": self.image.page_count <= 24,
                "version_verified": self.version_verified, "write_result_uncertain": self.destructive_started and not self.version_verified,
                "crc_mode": self.crc_mode, "protocol_evidence": PROTOCOL_EVIDENCE, "hardware_flash_verified": False}
