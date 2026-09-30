"""Private firmware storage and single-reader/single-writer OTA integration.

Nothing here runs at startup: only an explicitly acknowledged upgrade action
may request the first bootloader packet. Tests use injected serial transports.
"""
from collections import deque
import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid

from .firmware import FirmwareImage, UpgradeSession, MAX_PACKAGE_SIZE, version_tuple, version_string
from .storage import atomic_json, private_file
from . import v2


class FirmwareRuntime:
    def _init_firmware(self):
        self.firmware_storage_lock = threading.Lock()
        self.firmware_reserved = None
        self.firmware_session = None
        self.firmware_cancel = threading.Event()
        self.firmware_condition = threading.Condition(self.lock)
        self.firmware_rx = deque()
        self.firmware_rx_bytes = 0
        self.firmware_rx_overflow = False
        self.firmware_boot_mode = False
        self.firmware_status = None

    def _firmware_path(self, identifier, suffix="bin"):
        from .service import Fault
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise Fault("INVALID_ID", "无效固件 ID")
        return self.settings.directory / "firmware" / (identifier + "." + suffix)

    def store_firmware(self, raw, filename="firmware.bin"):
        from .service import Fault
        image = FirmwareImage.from_bytes(raw)
        # Names are labels only, never filesystem paths.
        name = str(filename).replace("\\", "/").split("/")[-1][:160]
        name = "".join(c for c in name if c.isprintable()) or "firmware.bin"
        with self.firmware_storage_lock:
            directory = self.settings.directory / "firmware"
            directory.mkdir(exist_ok=True, mode=0o700)
            if len(list(directory.glob("*.json"))) >= 128:
                raise Fault("FIRMWARE_STORAGE_LIMIT", "私有固件目录最多保存 128 个文件", 409)
            identifier = uuid.uuid4().hex
            path = self._firmware_path(identifier)
            fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=directory)
            try:
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                if os.name == "nt":
                    private_file(temporary)
                os.replace(temporary, path)
                metadata = image.inspect() | {"id": identifier, "filename": name, "uploaded_at": time.time()}
                atomic_json(self._firmware_path(identifier, "json"), metadata)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return self.inspect_firmware(identifier)

    def _load_firmware(self, identifier):
        from .service import Fault
        path = self._firmware_path(identifier)
        if not path.is_file() or not self._firmware_path(identifier, "json").is_file():
            raise Fault("FIRMWARE_NOT_FOUND", "私有固件文件不存在", 404)
        with path.open("rb") as stream:
            raw = stream.read(MAX_PACKAGE_SIZE + 1)
        image = FirmwareImage.from_bytes(raw)
        metadata = json.loads(self._firmware_path(identifier, "json").read_text(encoding="utf-8"))
        if image.sha256 != metadata["sha256"]:
            raise Fault("FIRMWARE_CHANGED", "私有固件文件 SHA256 校验失败", 409)
        return image, metadata

    def inspect_firmware(self, identifier):
        image, metadata = self._load_firmware(identifier)
        with self.lock:
            current = self.device.get("version", {}).get("app") if self.source == "live" and self.serial else None
            return image.inspect(current) | {"id": identifier, "filename": metadata["filename"], "uploaded_at": metadata["uploaded_at"], "current_version": version_string(current) if current is not None else None}

    def firmware_listing(self):
        paths = sorted((self.settings.directory / "firmware").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [self.inspect_firmware(path.stem) for path in paths]

    def _capture_firmware(self, raw):
        # Called only by the existing serial reader while holding self.lock.
        if not self.firmware_boot_mode:
            return False
        if self.firmware_rx_bytes + len(raw) > 1024 * 1024:
            self.firmware_rx_overflow = True
        else:
            self.firmware_rx.append(bytes(raw))
            self.firmware_rx_bytes += len(raw)
        self.firmware_condition.notify_all()
        return True

    def _firmware_progress(self, operation_id):
        with self.lock:
            if self.firmware_session:
                self.firmware_status = self.firmware_session.snapshot() | {"operation_id": operation_id}
                if operation_id in self.operations:
                    self.operations[operation_id]["progress"] = copy.deepcopy(self.firmware_status)

    def _write_firmware(self, packet):
        # Cancellation acknowledgement and each new Boot write share the same
        # lock. An in-flight write may finish before cancel is acknowledged;
        # after acknowledgement, no additional Boot packet can be started.
        with self.lock:
            if self.stop_event.is_set() or self.firmware_cancel.is_set():
                return False
            self._write(packet)
            return True

    def _upgrade_firmware(self, p, operation_id):
        from .service import Fault
        required = {"id", "acknowledged", "expected_version", "expected_identity"}
        if set(p) != required or p.get("acknowledged") is not True:
            raise Fault("ACKNOWLEDGEMENT_REQUIRED", "升级需要 id、acknowledged=true、expected_version 和 expected_identity")
        image, _ = self._load_firmware(p["id"])
        with self.lock:
            if self.source != "live" or not self.serial:
                raise Fault("NOT_LIVE", "升级需要真实 USB 设备", 409)
            if self.recordings.writer or self.calibration_poll or self.probe_capture:
                raise Fault("DEVICE_BUSY", "先停止录制、校准或协议探测，再升级", 409)
            identity, link = self.identity, self.serial
            if not identity or identity != p["expected_identity"]:
                raise Fault("DEVICE_CHANGED", "设备身份与确认时不同", 409)
            if self.firmware_cancel.is_set():
                return {"cancelled": True, "uncertain": False, "message": "升级在发送入口指令前已取消"}
        # Read the version immediately before any bootloader write. Cached UI
        # versions are insufficient to authorize a destructive transaction.
        actual = v2.versions(self._v2_request(v2.READ_VERSION))
        if version_tuple(p["expected_version"]) != tuple(actual["app"]):
            raise Fault("VERSION_CHANGED", "当前设备版本与确认时不同", 409)
        if actual["app"][0] < 2:
            raise Fault("UNVERIFIED_BOOT_CRC", "网页升级目前仅开放已确认的 2.x 设备", 409)
        with self.lock:
            if self.serial is not link or self.identity != identity:
                raise Fault("DEVICE_CHANGED", "升级前设备连接已变化", 409)
            # Boot ACKs use a one-byte device ID, obtained from fresh settings.
        config = v2.configuration(self._v2_request(v2.READ_CONFIGURATION))
        session = UpgradeSession(image, actual["app"], config["slave_id"])
        with self.lock:
            if self.serial is not link or self.identity != identity:
                raise Fault("DEVICE_CHANGED", "升级前设备连接已变化", 409)
            self.device.update(version=actual, configuration=config)
            self.firmware_session = session
            self.firmware_boot_mode = True
            self.firmware_rx.clear(); self.firmware_rx_bytes = 0; self.firmware_rx_overflow = False
            self._invalidate_reference()
            self.connection = "upgrading"
        try:
            pending = None if self.firmware_cancel.is_set() else session.start()
            if pending is None:
                session.cancel()
            while session.state not in {"failed", "cancelled", "succeeded", "waiting_reconnect"}:
                if self.stop_event.is_set() or self.firmware_cancel.is_set():
                    session.cancel(); break
                if pending is not None:
                    with self.lock:
                        if self.serial is not link or self.identity != identity:
                            session._fail("DEVICE_CHANGED", "升级途中设备连接已变化，写入结果未知"); break
                        self.firmware_rx.clear(); self.firmware_rx_bytes = 0
                        session.decoder.clear()
                    if not self._write_firmware(pending.packet):
                        session.cancel(); break
                    pending = None
                self._firmware_progress(operation_id)
                with self.firmware_condition:
                    if self.firmware_rx_overflow:
                        session._fail("BOOT_RX_OVERFLOW", "Boot 应答接收溢出，写入结果未知"); break
                    if self.serial is not link:
                        session._fail("DEVICE_DISCONNECTED", "升级完成前设备断开，写入结果未知"); break
                    if not self.firmware_rx:
                        self.firmware_condition.wait(.05)
                    raw = self.firmware_rx.popleft() if self.firmware_rx else b""
                    self.firmware_rx_bytes -= len(raw)
                pending = session.feed(raw) if raw else session.tick()
            if session.state == "waiting_reconnect":
                # Last CC generates exactly one reboot packet. Cancellation can
                # suppress it; no retry ever replays reboot, erase or full OTA.
                if self.stop_event.is_set() or self.firmware_cancel.is_set():
                    session.cancel()
                else:
                    if not self._write_firmware(pending.packet):
                        session.cancel()
                    else:
                        with self.lock:
                            self.firmware_boot_mode = False
                            self.decoder.buffer.clear()
                            self.connection = "reconnecting"
                        self._firmware_progress(operation_id)
                        self._firmware_verify_reconnect(session, identity)
        except Exception as exc:
            # Once entry was requested, even an unexpected transport/runtime
            # exception cannot be downgraded to a safely retryable failure.
            session._fail(getattr(exc, "code", "UPGRADE_FAILED"), str(exc))
        finally:
            with self.lock:
                self.firmware_boot_mode = False
                self.firmware_rx.clear(); self.firmware_rx_bytes = 0
                self._firmware_progress(operation_id)
                if session.state != "succeeded":
                    self.connection = "firmware-uncertain" if self.serial else "disconnected"
                    self.error = "固件操作结果未确认；请检查设备，不会自动重放升级或重启"
        return {"firmware": session.snapshot(), "uncertain": session.snapshot()["write_result_uncertain"], "cancelled": session.state == "cancelled", "verification": "post-reboot-device-version" if session.version_verified else "unverified", "message": "设备重连版本已核实" if session.version_verified else "固件操作未确认完成"}

    def _firmware_verify_reconnect(self, session, identity):
        from .service import Fault
        deadline = time.monotonic() + 30
        # Allow reboot to take effect before sending a harmless version query.
        self.stop_event.wait(.5)
        while time.monotonic() < deadline:
            if self.stop_event.is_set() or self.firmware_cancel.is_set():
                session.cancel(); return
            with self.lock:
                connected = self.serial is not None and self.identity == identity
            if not connected:
                candidates = [p for p in self.ports() if p["identity"] == identity]
                if len(candidates) == 1:
                    try:
                        self._connect(candidates[0]["device"], identity)
                    except Fault:
                        pass
                self.stop_event.wait(.2); continue
            try:
                observed = v2.versions(self._v2_request(v2.READ_VERSION, timeout=1))
                session.verify_reconnected_version(observed["app"])
                with self.lock:
                    self.device.update(version=observed, protocol="v2", updated_at=time.time())
                    if session.version_verified:
                        self.connection = "connected"
                        self.error = None
                return
            except (Fault, ValueError, OSError):
                self.stop_event.wait(.2)
        session._fail("RECONNECT_TIMEOUT", "重启后未读到目标设备版本，写入结果未知")

    def _restart_device(self, p, operation_id):
        from .service import Fault
        from .firmware import REBOOT
        if set(p) != {"acknowledged"} or p.get("acknowledged") is not True:
            raise Fault("ACKNOWLEDGEMENT_REQUIRED", "设备重启需要且仅接受 acknowledged=true")
        with self.lock:
            if self.source != "live" or not self.serial:
                raise Fault("NOT_LIVE", "重启需要真实 USB 设备", 409)
            if self.recordings.writer or self.calibration_poll or self.probe_capture:
                raise Fault("DEVICE_BUSY", "先停止录制、校准或协议探测，再重启", 409)
            identity, link = self.identity, self.serial
            if not identity:
                raise Fault("DEVICE_IDENTITY_REQUIRED", "设备身份尚未确认", 409)
            self.device_restarting = True
        sent = False
        verified = False
        try:
            before = v2.versions(self._v2_request(v2.READ_VERSION))
            if before["app"][0] < 2:
                raise Fault("UNSUPPORTED_PROTOCOL", "当前重启验证流程需要 2.x 版本协议", 409)
            with self.lock:
                if self.serial is not link or self.identity != identity:
                    raise Fault("DEVICE_CHANGED", "重启前设备连接已变化", 409)
                self._invalidate_reference()
                self.connection = "restarting"
                sent = True
            self._write(REBOOT)  # Exactly once: no write/ACK retry.
            self.stop_event.wait(.5)
            deadline = time.monotonic() + 30
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                with self.lock:
                    connected = self.serial is not None and self.identity == identity
                if not connected:
                    candidates = [port for port in self.ports() if port["identity"] == identity]
                    if len(candidates) == 1:
                        try:
                            self._connect(candidates[0]["device"], identity)
                        except Fault:
                            pass
                    self.stop_event.wait(.2);continue
                try:
                    after = v2.versions(self._v2_request(v2.READ_VERSION, timeout=1))
                    if after["app"] != before["app"]:
                        return {"uncertain": True, "message": "重启后设备版本与重启前不符", "version": after, "verification": "version-mismatch"}
                    with self.lock:
                        self.device.update(version=after, protocol="v2", updated_at=time.time())
                        self.connection, self.error = "connected", None
                    verified = True
                    return {"version": after, "verification": "post-reboot-device-version", "message": "重启请求已发送一次，设备当前版本已回读核实", "physical_restart_verified": False}
                except (Fault, OSError, ValueError):
                    self.stop_event.wait(.2)
            return {"uncertain": True, "message": "重启后未读到目标设备版本；不会补发重启"}
        except Exception as exc:
            if sent:
                return {"uncertain": True, "message": str(exc) + "；重启结果未知，不会重发"}
            raise
        finally:
            with self.lock:
                self.device_restarting = False
                if sent and not verified:
                    self.connection = "restart-uncertain" if self.serial else "disconnected"
                    self.error = "重启后版本尚未核实；不会重复发送重启"
