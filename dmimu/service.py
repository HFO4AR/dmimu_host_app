"""Single serial owner, telemetry, reconnect, recordings and serialized actions."""
import base64
from collections import deque
import copy
import hashlib
import json
import math
from pathlib import Path
import queue
import threading
import time
import uuid

import serial
from serial.tools import list_ports

from .protocol import Decoder, UNITS, encode_frame
from .recordings import Recordings
from .playback import StreamingPlayback
from .storage import atomic_json
from . import v2
from .firmware_runtime import FirmwareRuntime
from .trajectory import TrajectoryEstimator


class Fault(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


class Service(FirmwareRuntime):
    ACTIONS = ("connect", "disconnect", "demo", "record.start", "record.stop", "record.export", "playback.open", "playback.control", "device.configure", "device.calibrate", "device.calibration-abort", "device.factory-reset", "device.angle-zero", "device.yaw-zero", "device.read-settings", "device.inspect", "device.calibration-status", "protocol.probe", "allan.analyze", "allan.cancel", "firmware.inspect", "firmware.upgrade", "firmware.cancel", "device.build-info", "device.restart", "trajectory.reference", "trajectory.start", "trajectory.pause", "trajectory.reset", "trajectory.options")

    def __init__(self, settings, serial_factory=serial.Serial, port_provider=list_ports.comports):
        self.settings = settings
        self.serial_factory, self.port_provider = serial_factory, port_provider
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.reader = self.worker = None
        self.jobs = queue.Queue(maxsize=32)
        self.analysis_jobs = queue.Queue(maxsize=4)
        self.analysis_worker = None
        self.analysis_cancel = {}
        self.decoder = Decoder(settings.values["legacy_crc"], self._control_ack)
        self.control_condition = threading.Condition(self.lock)
        self._init_firmware()
        self.control_sequence = 0
        self.control_replies = deque(maxlen=64)
        self.device = {}
        self.calibration_poll = None
        self.device_restarting = False
        self.serial = None
        self.port = None
        self.identity = None
        self.source = "none"
        self.connection = "disconnected"
        self.error = None
        self.auto_connect = settings.values["auto_connect"]
        self.latest = {}
        self.seq = 0
        self.history = deque(maxlen=50000)
        self.probe_capture = None
        self.generation = 0
        self.logs = deque(maxlen=300)
        self.receive_times = {}
        self.rx_bytes = 0
        self.rx_window = deque(maxlen=8192)
        self.slave_frames = {}
        self.main_slave_id = None
        self.trajectory = TrajectoryEstimator()
        self.trajectory_epoch = 0
        self.trajectory.set_source(self.source, self.generation)
        self.recordings = Recordings(settings.directory)
        self.record_error = None
        self.playback = None
        self.next_scan = 0
        self.started = time.monotonic()
        self.operations = {}
        self.keys = {}
        self.operation_path = settings.directory / "operations.json"
        if self.operation_path.exists():
            for op in json.loads(self.operation_path.read_text(encoding="utf-8")):
                if op["state"] in {"queued", "running"}:
                    op.update(state="uncertain", error={"code": "HOST_RESTARTED", "message": "服务重启；原操作结果未知，不会自动重放"})
                self.operations[op["id"]] = op
                self.keys[op["key"]] = op["id"]

    def start(self):
        self.reader = threading.Thread(target=self._reader, name="imu-reader", daemon=True)
        self.worker = threading.Thread(target=self._worker, name="imu-operations", daemon=True)
        self.analysis_worker = threading.Thread(target=self._worker, args=(self.analysis_jobs,), name="imu-analysis", daemon=True)
        self.reader.start()
        self.worker.start()
        self.analysis_worker.start()

    def close(self):
        self.stop_event.set()
        self.firmware_cancel.set()
        with self.lock:
            cancellations = list(self.analysis_cancel.values())
        for cancel in cancellations:
            cancel.set()
        if self.reader:
            self.reader.join(timeout=2)
        if self.worker:
            self.worker.join()
        if self.analysis_worker:
            self.analysis_worker.join()
        with self.lock:
            if self.recordings.writer:
                self.recordings.stop()
            self._close_serial()

    def log(self, message, level="info"):
        with self.lock:
            self.logs.append({"time": time.time(), "level": level, "message": message})

    def ports(self):
        result = []
        for p in self.port_provider():
            if p.device.startswith("/dev/ttyS") and p.vid is None:
                continue
            text = f"{p.description or ''} {p.manufacturer or ''} {getattr(p, 'product', '') or ''}".upper()
            recognized = "DM-IMU" in text or "DM_IMU" in text or "DAMIAO" in text
            result.append({"device": p.device, "description": p.description, "manufacturer": p.manufacturer, "serial_number": p.serial_number, "vid": p.vid, "pid": p.pid, "recognized": recognized, "identity": f"{p.vid}:{p.pid}:{p.serial_number}" if p.serial_number else f"{p.vid}:{p.pid}:{p.description}:{p.device}"})
        return result

    def capabilities(self):
        legacy = self.settings.values["protocol"] == "legacy-v1"
        modern = not legacy and self.device.get("protocol") == "v2"
        return {"version": "1", "model": "DM-IMU-L1", "actions": list(self.ACTIONS), "single_device": True, "sources": ["live", "demo", "playback"], "units": UNITS,
                "firmware": {"upload_limit": 4096 * 255 + 18, "requires_acknowledged": True, "requires_expected_version": True, "requires_expected_identity": True, "minimum_app_major": 2, "hardware_flash_verified": False, "completion_verification": "post-reboot-version-readback"},
                "protocol_probe": {"experimental": True, "requests": "V1 documented setting-status request only", "requires_acknowledged": True},
                "device_control": {"profile": "v2" if modern else self.settings.values["protocol"], "supported": legacy or modern, "verification": "device-ack-and-readback" if modern else ("legacy-status-frame" if legacy else "unavailable"), "calibration_result_available": modern, "reason": None if legacy or modern else "点击读取设备版本，确认新版协议；旧版 1.x 可在网页选择旧版协议", "unsupported": [] if modern else ["installation_rotation", "sensor_range", "v2_configuration", "v2_calibration"]}}

    def snapshot(self):
        now = time.time()
        with self.lock:
            channels = copy.deepcopy(self.latest)
            for key, value in channels.items():
                value["age_ms"] = max(0, (now - value["updated_at"]) * 1000)
                value["stale"] = value["age_ms"] > 2000
                times = self.receive_times.get(key, ())
                value["rate_hz"] = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] and now - times[-1] < 2 else 0
            replay = None
            if self.playback:
                replay = {k: self.playback[k] for k in ("id", "position", "duration", "playing", "speed")}
            self._trim_rx_window()
            self._trajectory_watchdog()
            return {"source": self.source, "generation": self.generation, "connection": self.connection, "port": self.port, "identity": self.identity, "error": self.error, "seq": self.seq, "host_time": now, "channels": channels, "auto_connect": self.auto_connect,
                    "statistics": {"frames": self.decoder.frames, "crc_errors": self.decoder.crc_errors, "discarded_bytes": self.decoder.discarded_bytes, "control_frames": self.decoder.control_frames, "rx_bytes": self.rx_bytes, "rx_bytes_per_s": sum(size for _, size in self.rx_window), "main_slave_id": self.main_slave_id, "slave_frames": dict(self.slave_frames), "other_slave_frames": sum(count for key, count in self.slave_frames.items() if key != str(self.main_slave_id)), "uptime_s": time.monotonic() - self.started},
                    "device": copy.deepcopy(self.device), "trajectory": self.trajectory.snapshot() | {"epoch": self.trajectory_epoch}, "firmware": copy.deepcopy(self.firmware_status), "firmware_busy": self.firmware_reserved is not None, "device_busy": self.firmware_reserved is not None or self.device_restarting, "recording": {"id": self.recordings.active, "bytes": self.recordings.bytes, "error": self.record_error}, "playback": replay}

    def _trajectory_watchdog(self):
        if self.trajectory.set_source(self.source, self.generation):
            self.trajectory_epoch += 1
        if self.source == "playback" and self.playback and not self.playback["playing"]:
            if self.trajectory.active or self.trajectory.calibrating:
                self.trajectory.pause("回放已暂停；恢复回放后手动继续追踪")
        self.trajectory.check_idle()

    def _consume_trajectory(self, samples):
        self._trajectory_watchdog()
        self.trajectory.consume([item for item in samples if item is not None])

    def trajectory_data(self, after=0, epoch=None):
        if type(after) is not int or not 0 <= after <= 12000:
            raise ValueError("轨迹 after 必须为 0–12000 的点索引")
        with self.lock:
            self._trajectory_watchdog()
            if epoch is not None and epoch != self.trajectory_epoch:
                after = 0
            total = len(self.trajectory.points)
            if after > total: after = 0
            return {"status": self.trajectory.snapshot() | {"epoch": self.trajectory_epoch},
                    "epoch": self.trajectory_epoch, "points": copy.deepcopy(self.trajectory.points[after:]),
                    "offset": after, "next": total, "total": total, "generation": self.generation}

    def trajectory_export(self, kind):
        from .trajectory_export import export
        with self.lock:
            self._trajectory_watchdog()
            payload = self.trajectory.export_payload(kind)
        return export(payload)

    def samples_since(self, after=0):
        with self.lock:
            data = []
            for sample in reversed(self.history):
                if sample["seq"] <= after:
                    break
                data.append(sample)
            data.reverse()
            return {"samples": data, "generation": self.generation, "seq": self.seq, "truncated": bool(self.history and after and after < self.history[0]["seq"] - 1)}

    def _ingest(self, raw, stamp, link=None):
        with self.lock:
            if link is not None and link is not self.serial:
                return
            self._count_rx(len(raw))
            if self._capture_firmware(raw):
                return
            if self.probe_capture is not None:
                capture = self.probe_capture
                if capture["bytes"] + len(raw) <= 2 * 1024 * 1024:
                    capture["chunks"].append({"time": stamp, "data": base64.b64encode(raw).decode("ascii")})
                    capture["bytes"] += len(raw)
                else:
                    capture["truncated"] = True
            if self.recordings.writer:
                try:
                    self.recordings.append(raw, stamp)
                except OSError as exc:
                    self.record_error = str(exc)
                    try:
                        self.recordings.stop()
                    except OSError:
                        self.recordings.writer = None
                        self.recordings.active = None
                    self.log("录制写入失败，已停止：" + str(exc), "error")
            batch = [self._frame(frame, stamp) for frame in self.decoder.feed(raw)]
            self._consume_trajectory(batch)

    def _frame(self, frame, stamp, measurement_time=None):
        key = str(frame.slave_id)
        self.slave_frames[key] = self.slave_frames.get(key, 0) + 1
        if self.source == "playback":
            self.decoder.frames += 1
            self._count_rx(len(frame.raw))
        configured = self.device.get("configuration", {}).get("slave_id")
        if self.main_slave_id is None:
            self.main_slave_id = configured if type(configured) is int and 0 <= configured <= 255 else frame.slave_id
        # Forwarded bus frames must not be combined into a single attitude or
        # integrated trajectory. They remain in recordings and receive counts.
        if frame.slave_id != self.main_slave_id:
            return
        self.seq += 1
        item = {"seq": self.seq, "time": stamp, "measurement_time": stamp if measurement_time is None else measurement_time, "channel": frame.channel, "values": frame.values, "slave_id": frame.slave_id}
        self.latest[frame.channel] = {"values": frame.values, "updated_at": stamp, "unit": UNITS.get(frame.channel, ""), "slave_id": frame.slave_id, "frame_length": len(frame.raw)}
        times = self.receive_times.setdefault(frame.channel, deque(maxlen=2000))
        times.append(stamp)
        self.history.append(item)
        if self.source == "live":
            self.connection = "streaming"
            self.error = None
        return item

    def _apply_device_configuration(self, configuration):
        old = self.device.get("configuration", {})
        keys = ("installation_rotation", "accel_range", "gyro_range", "slave_id")
        changed = any(key in old and old.get(key) != configuration.get(key) for key in keys)
        if changed or self.main_slave_id is not None and self.main_slave_id != configuration.get("slave_id"):
            self._invalidate_reference()
        self.device.update(configuration=configuration, updated_at=time.time())

    def _invalidate_reference(self):
        # Coordinate/range/calibration changes invalidate browser zero references.
        self.generation += 1
        self.latest.clear()
        self.receive_times.clear()
        self.history.clear()
        self.main_slave_id = None
        if hasattr(self, "trajectory") and self.trajectory.set_source(self.source, self.generation):
            self.trajectory_epoch += 1

    def _clear_data(self):
        self._invalidate_reference()
        self.decoder = Decoder(self.settings.values["legacy_crc"], self._control_ack)
        self.device = {}
        self.calibration_poll = None
        self.control_replies.clear()
        self.rx_bytes = 0
        self.rx_window.clear()
        self.slave_frames.clear()
        if self.source == "playback" and self.playback:
            recorded = self.playback.get("header", {}).get("device", {})
            if isinstance(recorded, dict):
                self.device = copy.deepcopy(recorded)

    def _trim_rx_window(self):
        cutoff = time.monotonic() - 1
        while self.rx_window and self.rx_window[0][0] < cutoff:
            self.rx_window.popleft()

    def _count_rx(self, size):
        self.rx_bytes += size
        self.rx_window.append((time.monotonic(), size))
        self._trim_rx_window()

    def _close_serial(self):
        if self.serial:
            with self.io_lock:
                self.serial.close()
            self.serial = None

    def _connect(self, device, identity=None):
        with self.lock:
            if self.recordings.writer:
                raise Fault("RECORDING_ACTIVE", "先停止录制，再切换设备")
            self._close_serial()
            kwargs = {"port": device, "baudrate": self.settings.values["baudrate"], "timeout": 0.02, "write_timeout": 1}
            import os
            if os.name != "nt":
                kwargs["exclusive"] = True
            try:
                self.serial = self.serial_factory(**kwargs)
            except (OSError, serial.SerialException) as exc:
                self.error = str(exc)
                self.connection = "error"
                self.log("打开串口失败：" + str(exc), "error")
                raise Fault("SERIAL_OPEN_FAILED", str(exc), 409) from None
            self.port, self.identity = device, identity or device
            self.source, self.connection = "live", "connected"
            self.playback = None
            self.error = None
            self.connected_at = time.time()
            self._clear_data()
            self.settings.values["preferred_device"] = self.identity
            self.settings.save()
            self.log(f"已打开 {device}，等候有效数据；未向设备发送配置")

    def _reader(self):
        last_demo = 0
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    link, source = self.serial, self.source
                if link:
                    with self.io_lock:
                        if link is not self.serial:
                            continue
                        raw = link.read(max(1, min(link.in_waiting, 65536)))
                    if raw:
                        self._ingest(raw, time.time(), link)
                    with self.lock:
                        latest_time = max((v["updated_at"] for v in self.latest.values()), default=self.connected_at)
                        if not (self.firmware_reserved or self.device_restarting) and time.time() - latest_time > 2:
                            self.connection = "no-data"
                            self.error = "串口已打开，但超过两秒没有有效数据；检查输出接口、通道和固件配置"
                elif source == "demo":
                    if time.monotonic() - last_demo >= 0.01:
                        last_demo = time.monotonic()
                        t = last_demo - self.started
                        r, p, y = 20 * math.sin(t * .6), 30 * math.sin(t * .4), (t * 12 + 180) % 360 - 180
                        self._ingest(encode_frame(1, [math.sin(t) * 1.2, math.cos(t) * .7, 9.80665]) + encode_frame(2, [.2 * math.cos(t*.6), .2 * math.cos(t*.4), .2094]) + encode_frame(3, [r, p, y]), time.time())
                    self.stop_event.wait(.004)
                elif source == "playback":
                    self._tick_playback()
                    self.stop_event.wait(.01)
                else:
                    now = time.monotonic()
                    if not (self.firmware_reserved or self.device_restarting) and self.auto_connect and now >= self.next_scan:
                        self.next_scan = now + 2
                        ports = self.ports()
                        preferred = self.settings.values["preferred_device"]
                        candidates = [p for p in ports if p["identity"] == preferred] or [p for p in ports if p["recognized"]]
                        if len(candidates) == 1:
                            self._connect(candidates[0]["device"], candidates[0]["identity"])
                        elif len(candidates) > 1:
                            with self.lock:
                                self.error = "发现多个 IMU，请在连接栏选择设备"
                    self.stop_event.wait(.1)
            except (OSError, serial.SerialException, Fault) as exc:
                with self.lock:
                    if link and link is not self.serial:
                        continue
                    if self.serial:
                        self.log("设备断开：" + str(exc), "warning")
                        try:
                            self._close_serial()
                        except OSError:
                            self.serial = None
                        if self.recordings.writer:
                            self.recordings.stop()
                            self.record_error = "设备断开，录制已结束；重连后需重新开始录制"
                    self.source, self.connection = "none", "disconnected"
                    self.error = str(exc)
                self.stop_event.wait(.5)

    def submit(self, action, params, key):
        if action not in self.ACTIONS:
            raise Fault("UNKNOWN_ACTION", "不支持的操作")
        if not isinstance(params, dict) or not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise Fault("INVALID_REQUEST", "参数必须是对象，并提供 1–128 字符的幂等键")
        signature = hashlib.sha256(json.dumps([action, params], sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.lock:
            if key in self.keys:
                previous = self.operations[self.keys[key]]
                if previous["signature"] != signature:
                    raise Fault("IDEMPOTENCY_CONFLICT", "相同幂等键对应不同操作", 409)
                return copy.deepcopy(previous)
            if (self.firmware_reserved or self.device_restarting) and action not in {"firmware.cancel", "firmware.inspect", "allan.analyze", "allan.cancel", "record.export"}:
                raise Fault("FIRMWARE_BUSY", "升级或重启占用设备；完成前不能切换、录制或配置", 409)
            if action == "firmware.cancel":
                if set(params) != {"operation_id"} or params["operation_id"] != self.firmware_reserved:
                    raise Fault("UPGRADE_NOT_RUNNING", "指定升级操作未运行", 409)
                self.firmware_cancel.set()
                self.firmware_condition.notify_all()
            if action == "allan.cancel":
                if set(params) != {"operation_id"} or params["operation_id"] not in self.analysis_cancel:
                    raise Fault("ANALYSIS_NOT_RUNNING", "指定分析操作未在排队或运行", 409)
                self.analysis_cancel[params["operation_id"]].set()
            if len(self.operations) >= 10000:
                raise Fault("OPERATION_LIMIT", "操作记录已达上限，请归档数据目录后再运行", 409)
            op = {"id": uuid.uuid4().hex, "key": key, "signature": signature, "action": action, "params": params, "state": "queued", "created_at": time.time()}
            self.operations[op["id"]] = op
            self.keys[key] = op["id"]
            if action in {"firmware.cancel", "allan.cancel"}:
                op.update(state="succeeded", result={"cancellation_requested": True, "operation_id": params["operation_id"]}, finished_at=time.time())
                self._save_operations()
                return copy.deepcopy(op)
            if action == "allan.analyze":
                self.analysis_cancel[op["id"]] = threading.Event()
            if action == "firmware.upgrade":
                self.firmware_reserved = op["id"]
                self.firmware_status = None
                self.firmware_cancel.clear()
            try:
                target = self.analysis_jobs if action == "allan.analyze" else self.jobs
                target.put_nowait(op["id"])
            except queue.Full:
                self.analysis_cancel.pop(op["id"], None)
                if action == "firmware.upgrade":
                    self.firmware_reserved = None
                del self.operations[op["id"]]
                del self.keys[key]
                raise Fault("QUEUE_FULL", "操作队列已满", 409) from None
            self._save_operations()
            return copy.deepcopy(op)

    def _save_operations(self):
        atomic_json(self.operation_path, list(self.operations.values()))

    def operation(self, identifier):
        with self.lock:
            if identifier not in self.operations:
                raise Fault("OPERATION_NOT_FOUND", "操作不存在", 404)
            return copy.deepcopy(self.operations[identifier])

    def _worker(self, jobs=None):
        jobs = self.jobs if jobs is None else jobs
        while not self.stop_event.is_set():
            try:
                identifier = jobs.get(timeout=.2)
            except queue.Empty:
                if jobs is self.jobs and not self.firmware_reserved and self.calibration_poll and self.source == "live":
                    self._poll_calibration()
                continue
            with self.lock:
                op = self.operations[identifier]
                op.update(state="running", started_at=time.time())
                self._save_operations()
            try:
                result = self._perform(op["action"], op["params"], operation_id=identifier)
                state = "uncertain" if result.get("uncertain") else "succeeded"
                with self.lock:
                    op.update(state=state, result=result)
            except (Fault, ValueError, OSError, KeyError, TypeError) as exc:
                with self.lock:
                    op.update(state="failed", error={"code": getattr(exc, "code", "ACTION_FAILED"), "message": str(exc)})
                self.log(f"{op['action']}：{exc}", "error")
            except Exception as exc:
                with self.lock:
                    op.update(state="failed", error={"code": "INTERNAL_ERROR", "message": str(exc)})
                self.log(f"{op['action']} 内部错误：{exc}", "error")
            with self.lock:
                op["finished_at"] = time.time()
                if op["action"] == "allan.analyze":
                    self.analysis_cancel.pop(identifier, None)
                self._save_operations()
            jobs.task_done()

    def _perform(self, action, p, operation_id=None):
        if action.startswith("trajectory."):
            with self.lock:
                self._trajectory_watchdog()
                if action in {"trajectory.reference", "trajectory.start"} and self.source == "playback" and self.playback and not self.playback["playing"]:
                    raise Fault("PLAYBACK_PAUSED", "先开始回放，再建立参考或继续追踪", 409)
                if action == "trajectory.options":
                    self.trajectory.set_options(p)
                else:
                    if p: raise ValueError("此轨迹动作不接受参数")
                    if action == "trajectory.reference":
                        if self.source == "none" or self.source == "live" and not self.latest:
                            raise Fault("NO_DATA", "建立参考需要有效数据源", 409)
                        if self.firmware_reserved or self.device_restarting:
                            raise Fault("DEVICE_BUSY", "设备升级或重启期间不能建立参考", 409)
                        self.trajectory.begin_reference(); self.trajectory_epoch += 1
                    elif action == "trajectory.start":
                        if not self.trajectory.start():
                            raise Fault("REFERENCE_REQUIRED", self.trajectory.message, 409)
                    elif action == "trajectory.pause": self.trajectory.pause()
                    elif action == "trajectory.reset":
                        self.trajectory.reset(); self.trajectory_epoch += 1
                return {"trajectory": self.trajectory.snapshot() | {"epoch": self.trajectory_epoch}, "verification": "host-inertial-estimate", "device_modified": False}
        if action == "firmware.inspect":
            if set(p) != {"id"}:
                raise ValueError("固件检查需要 id")
            return self.inspect_firmware(p["id"])
        if action == "firmware.upgrade":
            try:
                return self._upgrade_firmware(p, operation_id)
            finally:
                with self.lock:
                    self.firmware_reserved = None
        if (self.firmware_reserved or self.device_restarting) and action not in {"allan.analyze", "allan.cancel", "record.export"}:
            raise Fault("FIRMWARE_BUSY", "升级或重启占用设备", 409)
        if action == "device.restart":
            return self._restart_device(p, operation_id)
        if action == "device.build-info":
            if p:
                raise ValueError("读取编译信息不接受参数")
            info = v2.build_info(self._v2_request(v2.BUILD_INFO))
            with self.lock:
                self.device.update(build_info=info, updated_at=time.time())
            return {"build_info": info, "verification": "device-ack"}
        if action == "allan.analyze":
            if set(p) != {"recording_id", "channel", "sample_rate"}:
                raise ValueError("Allan 参数需要 recording_id、channel、sample_rate")
            from .allan import analyze_recording
            identifier = operation_id or uuid.uuid4().hex
            with self.lock:
                cancel = self.analysis_cancel.setdefault(identifier, threading.Event())
            def progress(data):
                with self.lock:
                    if identifier in self.operations:
                        self.operations[identifier]["progress"] = data
            try:
                if cancel.is_set():
                    raise Fault("CANCELLED", "分析在执行前已取消", 409)
                result = analyze_recording(self.recordings, p["recording_id"], p["channel"], p["sample_rate"], cancel=lambda: self.stop_event.is_set() or cancel.is_set(), progress=progress)
                result["id"] = identifier
                directory = self.settings.directory / "analyses"
                directory.mkdir(exist_ok=True, mode=0o700)
                atomic_json(directory / (identifier + ".json"), result)
                return {"id": identifier, "sample_count": result["sample_count"], "verification": "original-recording-analysis"}
            finally:
                with self.lock:
                    self.analysis_cancel.pop(identifier, None)
        if action == "allan.cancel":
            if set(p) != {"operation_id"}:
                raise ValueError("取消分析需要 operation_id")
            with self.lock:
                event = self.analysis_cancel.get(p["operation_id"])
                if not event:
                    raise Fault("ANALYSIS_NOT_RUNNING", "分析尚未执行或已经结束", 409)
                event.set()
            return {"cancellation_requested": True, "operation_id": p["operation_id"]}
        if action == "connect":
            port = next((port for port in self.ports() if port["device"] == p.get("port")), None)
            if not port:
                raise Fault("PORT_NOT_FOUND", "请选择系统当前存在的串口")
            self._connect(port["device"], port["identity"])
            self.auto_connect = True
            return {"port": self.port, "state": "connected", "stream_verified": bool(self.latest)}
        if action in {"disconnect", "demo"}:
            with self.lock:
                if self.recordings.writer:
                    raise Fault("RECORDING_ACTIVE", "先停止录制，再切换数据源")
                self._close_serial()
                self.auto_connect = False
                self.source = "demo" if action == "demo" else "none"
                self.connection = "demo" if action == "demo" else "disconnected"
                self.playback = None
                self.error = None
                self._clear_data()
            return {"source": self.source}
        if action == "record.start":
            with self.lock:
                if self.source not in {"live", "demo"} or not self.latest:
                    raise Fault("NO_DATA", "需要有效实时或演示数据才能录制")
                self.record_error = None
                return {"id": self.recordings.start(self.source, self.port, self.decoder.legacy_crc, device=copy.deepcopy(self.device)), "source": self.source}
        if action == "record.stop":
            with self.lock:
                return {"id": self.recordings.stop()}
        if action == "record.export":
            with self.lock:
                if p.get("id") == self.recordings.active:
                    raise Fault("RECORDING_ACTIVE", "先停止录制，再导出")
            return self.recordings.export(p["id"], self.stop_event)
        if action == "playback.open":
            return self._open_playback(p["id"], operation_id=operation_id)
        if action == "playback.control":
            with self.lock:
                if not self.playback:
                    raise Fault("NO_PLAYBACK", "先打开录制")
                if "speed" in p:
                    speed = float(p["speed"])
                    if speed not in {.25, .5, 1, 2, 4}:
                        raise ValueError("回放倍速必须为 0.25、0.5、1、2 或 4")
                    self.playback["speed"] = speed
                if "position" in p:
                    position = float(p["position"])
                    if not math.isfinite(position) or not 0 <= position <= self.playback["duration"]:
                        raise ValueError("无效回放位置")
                    self._seek(position)
                if "playing" in p:
                    if type(p["playing"]) is not bool:
                        raise ValueError("playing 必须是布尔值")
                    self.playback["playing"] = p["playing"]
                self.playback["last_tick"] = time.monotonic()
                return {"position": self.playback["position"], "playing": self.playback["playing"], "speed": self.playback["speed"]}
        if action == "protocol.probe":
            return self._probe(p)
        if action == "device.inspect":
            if p:
                raise ValueError("device.inspect does not accept parameters")
            parsed = v2.versions(self._v2_request(v2.READ_VERSION))
            with self.lock:
                self.device["version"] = parsed
                self.device["protocol"] = "v2" if parsed["app"][0] >= 2 else "legacy-v1"
            if parsed["app"][0] < 2:
                return {"version": parsed, "verification": "device-ack"}
            config = v2.configuration(self._v2_request(v2.READ_CONFIGURATION))
            static = v2.static_status(self._v2_request(v2.STATIC_STATUS))
            six = v2.six_face_status(self._v2_request(v2.SIX_FACE_STATUS))
            with self.lock:
                self._apply_device_configuration(config)
                self.device.update(static_calibration=static, six_face_calibration=six, updated_at=time.time())
            return {"version": parsed, "configuration": config, "static_calibration": static, "six_face_calibration": six, "verification": "device-ack"}
        if action == "device.calibration-status":
            if p:
                raise ValueError("device.calibration-status does not accept parameters")
            result = {"static_calibration": v2.static_status(self._v2_request(v2.STATIC_STATUS)),
                      "six_face_calibration": v2.six_face_status(self._v2_request(v2.SIX_FACE_STATUS))}
            with self.lock:
                self.device.update(result, updated_at=time.time())
            return result | {"verification": "device-ack"}
        return self._device_action(action, p)

    def _control_ack(self, ack):
        with self.control_condition:
            self.control_sequence += 1
            self.control_replies.append((self.control_sequence, ack))
            self.control_condition.notify_all()

    def _v2_request(self, command, payload=b"", timeout=2):
        # Operation worker is the only requester. The reader remains the sole
        # consumer of the serial link; no direct reads or automatic retries.
        with self.lock:
            if self.source != "live" or not self.serial:
                raise Fault("NOT_LIVE", "需要真实 USB 设备", 409)
            link = self.serial
            marker = self.control_sequence
        self._write(v2.request(command, payload))
        deadline = time.monotonic() + timeout
        with self.control_condition:
            while not self.stop_event.is_set():
                if self.serial is not link:
                    raise Fault("DEVICE_CHANGED", "查询期间设备连接已变化", 409)
                reply = next((ack for sequence, ack in self.control_replies if sequence > marker and ack.command == command), None)
                if reply is not None:
                    if reply.code != 0:
                        raise Fault("DEVICE_ACK_ERROR", f"设备拒绝命令 0x{command:02X}，ACK_CODE={reply.code}", 409)
                    return reply.payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Fault("DEVICE_ACK_TIMEOUT", f"未收到命令 0x{command:02X} 的有效应答；不会自动重发", 409)
                self.control_condition.wait(min(.1, remaining))
        raise Fault("CANCELLED", "服务正在关闭")

    def _probe(self, p):
        if p.get("acknowledged") is not True or set(p) != {"acknowledged"}:
            raise Fault("ACKNOWLEDGEMENT_REQUIRED", "协议探测会暂时进入设置模式；显式提供 acknowledged=true")
        if self.source != "live" or not self.serial:
            raise Fault("NOT_LIVE", "协议探测需要真实 USB IMU", 409)
        if self.recordings.writer:
            raise Fault("RECORDING_ACTIVE", "先停止录制，再探测协议", 409)
        identifier = uuid.uuid4().hex
        with self.lock:
            self.probe_capture = {"bytes": 0, "chunks": [], "truncated": False}
        requests = []
        failure = None
        try:
            # The only query published in V1.2: enter setting mode to report state.
            # Three bounded attempts, never enumerate command IDs or write settings.
            for _ in range(3):
                request = b"\xaa\x06\x01\x0d"
                requests.append({"time": time.time(), "hex": request.hex(" ")})
                self._write(request)
                self.stop_event.wait(.5)
        except (Fault, OSError) as exc:
            failure = str(exc)
        finally:
            try:
                request = b"\xaa\x06\x00\x0d"
                requests.append({"time": time.time(), "hex": request.hex(" ")})
                self._write(request)
                self.stop_event.wait(.2)
            except (Fault, OSError) as exc:
                failure = str(exc)
        with self.lock:
            chunks = self.probe_capture["chunks"]
            truncated = self.probe_capture["truncated"]
            self.probe_capture = None
        decoder = Decoder(self.decoder.legacy_crc)
        frame_types = {}
        status = None
        for item in chunks:
            for frame in decoder.feed(base64.b64decode(item["data"])):
                key = str(frame.kind)
                frame_types[key] = frame_types.get(key, 0) + 1
                if frame.kind == 7:
                    status = frame.values
        path = self.settings.directory / "protocol-probes"
        path.mkdir(exist_ok=True, mode=0o700)
        report = {"version": 1, "id": identifier, "port": self.port, "requests": requests, "chunks": chunks, "capture_truncated": truncated, "frame_types": frame_types, "state_report": status, "error": failure}
        atomic_json(path / (identifier + ".json"), report)
        self.log(f"协议探测 {identifier[:8]}：{len(chunks)} 个数据块，状态应答{'已观察到' if status else '未观察到'}")
        return {"id": identifier, "chunks": len(chunks), "frame_types": frame_types, "state_report_observed": status is not None, "firmware_version_verified": False, "configuration_write_supported": False, "capture_truncated": truncated, "uncertain": failure is not None, "message": failure or "探测记录已保存；查询完成不代表已识别新版控制协议"}

    def _open_playback(self, identifier, operation_id=None):
        with self.lock:
            if self.recordings.writer:
                raise Fault("RECORDING_ACTIVE", "先停止录制，再回放")
        last_progress = -1.
        def progress(value):
            nonlocal last_progress
            now = time.monotonic()
            if now - last_progress >= .5 or value["fraction"] >= 1:
                last_progress = now
                if operation_id:
                    with self.lock:
                        if operation_id in self.operations:
                            self.operations[operation_id]["progress"] = value
        stream = StreamingPlayback(self.recordings.path(identifier), cancel=self.stop_event, progress=progress)
        with self.lock:
            # Validation/indexing does not change the current source. Only a
            # successfully indexed recording is allowed to replace it.
            if self.recordings.writer:
                raise Fault("RECORDING_ACTIVE", "先停止录制，再回放")
            self._close_serial()
            self.auto_connect = False
            self.source, self.connection, self.error = "playback", "playback", None
            self.playback = {"id": identifier, "stream": stream, "header": stream.header, "channels": stream.channels,
                             "position": 0, "duration": stream.duration, "playing": False, "speed": 1,
                             "last_tick": time.monotonic()}
            self._seek(stream.first_elapsed)
        return {"id": identifier, "duration": stream.duration, "samples": stream.sample_count,
                "streaming": True, "checkpoints": len(stream.checkpoints)}

    def _seek(self, position):
        pb = self.playback
        # Read first so corruption/file replacement leaves the current view
        # intact rather than incrementing generation on a failed seek.
        samples = pb["stream"].seek(position)
        self._clear_data()
        self.main_slave_id = pb["stream"].primary_slave_id
        for elapsed, recorded_stamp, frame in samples:
            self._frame(frame, time.time(), measurement_time=recorded_stamp)
        pb["position"] = position

    def _tick_playback(self):
        with self.lock:
            pb = self.playback
            if not pb or not pb["playing"]:
                return
            now = time.monotonic()
            pb["position"] = min(pb["duration"], pb["position"] + (now - pb["last_tick"]) * pb["speed"])
            pb["last_tick"] = now
            try:
                samples, drained = pb["stream"].advance(pb["position"])
            except ValueError as exc:
                pb["playing"] = False
                raise Fault("REPLAY_CHANGED", str(exc), 409) from None
            batch = []
            for elapsed, stamp, frame in samples:
                item = self._frame(frame, time.time(), measurement_time=stamp)
                if item is not None:
                    batch.append(item)
            if batch and hasattr(self, "_consume_trajectory"):
                self._consume_trajectory(batch)
            if pb["position"] >= pb["duration"] and drained:
                pb["playing"] = False

    def _write(self, packet):
        with self.lock:
            if self.source != "live" or not self.serial:
                raise Fault("NOT_LIVE", "此操作需要真实 USB 设备", 409)
            link = self.serial
        with self.io_lock:
            if link is not self.serial:
                raise Fault("DEVICE_CHANGED", "设备连接已变化，结果未知", 409)
            if link.write(packet) != len(packet):
                raise Fault("SERIAL_WRITE_FAILED", "串口写入不完整，结果未知", 409)
            link.flush()

    def _poll_calibration(self):
        kind, deadline = self.calibration_poll
        command, parse = (v2.STATIC_STATUS, v2.static_status) if kind == "gyro" else (v2.SIX_FACE_STATUS, v2.six_face_status)
        try:
            status = parse(self._v2_request(command, timeout=1))
            with self.lock:
                self.device["static_calibration" if kind == "gyro" else "six_face_calibration"] = status | {"updated_at": time.time()}
            if status["state"] in (3, 4) or time.monotonic() > deadline:
                self.calibration_poll = None
                if status["state"] == 3:
                    with self.lock:
                        self._invalidate_reference()
                self.log("校准状态：" + ("设备报告完成" if status["state"] == 3 else "设备报告失败" if status["state"] == 4 else "等待完成超时，结果未知"))
        except (Fault, ValueError, OSError) as exc:
            self.calibration_poll = None
            self.log("校准状态查询停止，结果未知：" + str(exc), "warning")

    def _v2_device_action(self, action, p):
        if self.source != "live" or not self.serial:
            raise Fault("NOT_LIVE", "需要真实 USB 设备", 409)
        if self.recordings.writer:
            raise Fault("RECORDING_ACTIVE", "先停止录制，再修改设备", 409)
        if action == "device.read-settings":
            if p:
                raise ValueError("读取配置不接受参数")
            result = v2.configuration(self._v2_request(v2.READ_CONFIGURATION))
            with self.lock:
                self._apply_device_configuration(result)
            return {"configuration": result, "verification": "device-ack"}
        if self.calibration_poll and action not in {"device.calibration-abort"}:
            raise Fault("CALIBRATION_ACTIVE", "校准正在进行，请等待完成或取消六面校准", 409)
        if action == "device.configure":
            current = self.device.get("configuration", {})
            commands = v2.configuration_commands(p, current)
            entered = False
            outcome = None
            try:
                entered = True
                self._v2_request(2, b"\x01")
                for command, payload in commands:
                    self._v2_request(command, payload)
                actual = v2.configuration(self._v2_request(v2.READ_CONFIGURATION))
                with self.lock:
                    if {"installation_rotation", "accel_range", "gyro_range"} & p.keys():
                        self._invalidate_reference()
                if any(actual.get(k) != value for k, value in p.items()):
                    outcome = {"uncertain": True, "configuration": actual, "message": "当前参数回读不匹配；没有发送保存指令"}
                else:
                    self._v2_request(0x0C)
                    with self.lock:
                        self.device.update(configuration=actual, updated_at=time.time())
                    outcome = {"configuration": actual, "verification": "device-ack-and-readback", "save_acknowledged": True,
                               "persistent_storage_verified": False, "recalibration_required": bool({"installation_rotation", "accel_range", "gyro_range"} & p.keys())}
            except (Fault, OSError, ValueError) as exc:
                with self.lock:
                    if {"installation_rotation", "accel_range", "gyro_range"} & p.keys():
                        self._invalidate_reference()
                outcome = {"uncertain": True, "message": str(exc) + "；参数操作结果未知，不会自动重发"}
            finally:
                if entered:
                    try:
                        self._v2_request(2, b"\x00")
                    except (Fault, OSError) as exc:
                        outcome = (outcome or {}) | {"uncertain": True, "exit_acknowledged": False, "message": "退出设置模式未确认：" + str(exc)}
                        self.log("退出设置模式未确认：" + str(exc), "warning")
            return outcome
        if action in {"device.calibrate", "device.yaw-zero", "device.factory-reset", "device.calibration-abort"}:
            if p.get("acknowledged") is not True:
                raise Fault("ACKNOWLEDGEMENT_REQUIRED", "请显式提供 acknowledged=true 确认设备操作")
            if action == "device.calibrate":
                if set(p) != {"kind", "acknowledged"} or p["kind"] not in {"gyro", "six-face"}:
                    raise ValueError("校准类型为 gyro 或 six-face")
                command, payload = (v2.STATIC_START, b"") if p["kind"] == "gyro" else (v2.SIX_FACE_CONTROL, b"\x00")
            else:
                if set(p) != {"acknowledged"}:
                    raise ValueError("包含未知设备操作参数")
                command, payload = {"device.yaw-zero": (0x17, b""), "device.factory-reset": (0x0D, b""), "device.calibration-abort": (v2.SIX_FACE_CONTROL, b"\x01")}[action]
            if action in {"device.yaw-zero", "device.factory-reset"}:
                with self.lock:
                    self._invalidate_reference()
            try:
                self._v2_request(command, payload)
            except (Fault, OSError) as exc:
                return {"uncertain": True, "message": str(exc) + "；设备操作结果未知，不会自动重发"}
            if action == "device.calibrate":
                self.calibration_poll = (p["kind"], time.monotonic() + (30 if p["kind"] == "gyro" else 600))
                return {"kind": p["kind"], "started": True, "completed": False, "verification": "device-start-ack", "message": "设备已接受校准；进度与结果以设备后续状态应答为准"}
            if action == "device.calibration-abort":
                self.calibration_poll = None
            if action == "device.factory-reset":
                with self.lock:
                    self.device.pop("configuration", None)
            return {"verification": "device-ack", "message": "设备已确认指令"}
        if action == "device.angle-zero":
            raise Fault("UNSUPPORTED_PROTOCOL", "新版固件支持航向归零，请使用 device.yaw-zero", 409)
        raise Fault("UNKNOWN_ACTION", "未知设备操作")

    def _device_action(self, action, p):
        if self.settings.values["protocol"] != "legacy-v1" and self.device.get("protocol") == "v2":
            return self._v2_device_action(action, p)
        if action == "device.yaw-zero":
            raise Fault("UNSUPPORTED_PROTOCOL", "航向单独归零需要新版协议；旧版只公开了角度置零", 409)
        if self.settings.values["protocol"] != "legacy-v1":
            raise Fault("UNSUPPORTED_PROTOCOL", "新版控制协议尚未公开。仅确认固件为 1.x 时才能选择旧版协议", 409)
        if self.source != "live" or not self.serial:
            raise Fault("NOT_LIVE", "此操作需要真实 USB 设备", 409)
        if self.recordings.writer:
            raise Fault("RECORDING_ACTIVE", "先停止录制，再配置或校准", 409)
        if action in {"device.calibrate", "device.angle-zero"} and p.get("acknowledged") is not True:
            raise Fault("ACKNOWLEDGEMENT_REQUIRED", "明确提供 acknowledged=true 后执行设备校准或归零")
        packets = []
        expected = {}
        if action == "device.configure":
            allowed = {"acceleration_enabled", "gyro_enabled", "euler_enabled", "quaternion_enabled", "interval_ms", "heating_enabled", "target_temperature"}
            if not p or set(p) - allowed:
                raise Fault("INVALID_PARAMETER", "仅支持输出通道、上报周期和温控参数")
            for k, v in p.items():
                if k.endswith("enabled"):
                    if type(v) is not bool:
                        raise ValueError(f"{k} 必须是布尔值")
                    if k == "heating_enabled":
                        packets.append(bytes((0xAA, 4, int(v), 13)))
                    else:
                        code = {"acceleration_enabled": 4, "gyro_enabled": 5, "euler_enabled": 6, "quaternion_enabled": 7}[k]
                        packets.append(bytes((0xAA, 1, code + (16 if v else 0), 13)))
                    expected[k] = int(v)
                elif k == "interval_ms":
                    if type(v) is not int or not 1 <= v <= 1000:
                        raise ValueError("上报周期必须为 1–1000 ms")
                    packets.append(b"\xaa\x02" + v.to_bytes(2, "little") + b"\x0d")
                    expected[k] = v
                else:
                    if type(v) is not int or not 20 <= v <= 60:
                        raise ValueError("温控目标限定为 20–60°C")
                    packets.append(bytes((0xAA, 5, v, 13)))
                    expected["target"] = v
        elif action == "device.calibrate":
            kind = p.get("kind")
            if kind not in {"gyro", "six-face"}:
                raise ValueError("校准类型必须是 gyro 或 six-face")
            packets.append(bytes((0xAA, 3, 2 if kind == "gyro" else 3, 13)))
        elif action == "device.angle-zero":
            packets.append(b"\xaa\x0c\x01\x0d")
        elif action != "device.read-settings":
            raise Fault("UNKNOWN_ACTION", "不支持的设备操作")
        # Validate all parameters before the first device write.
        sent = False
        outcome = None
        try:
            sent = True
            self._write(b"\xaa\x06\x01\x0d")
            time.sleep(.1)
            for packet in packets:
                self._write(packet)
                time.sleep(.03)
            if action == "device.calibrate":
                outcome = {"uncertain": True, "sent": True, "message": "校准指令已发送；旧版协议不报告完成结果，请观察设备指示灯，不要重复发送", "kind": p["kind"]}
                return outcome
            start = time.time()
            if action == "device.configure":
                self._write(b"\xaa\x03\x01\x0d")
            # Read-back is volatile state; persistent flash verification needs power cycle.
            self._write(b"\xaa\x06\x01\x0d")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not self.stop_event.is_set():
                with self.lock:
                    state = self.latest.get("device_status", {})
                    temp = self.latest.get("temperature", {})
                    actual = state.get("values", {}) | temp.get("values", {})
                    fresh = state.get("updated_at", 0) > start and (not any(k in expected for k in ("interval_ms", "target")) or temp.get("updated_at", 0) > start)
                    if fresh and action in {"device.configure", "device.read-settings"} and all(actual.get(k) == v for k, v in expected.items()):
                        outcome = {"verification": "device-readback", "configuration": actual, "persistent_storage_verified": False, "save_sent": action == "device.configure"}
                        return outcome
                time.sleep(.02)
            outcome = {"uncertain": True, "sent": True, "message": "指令已发送，但未收到可验证的完成结果；不要换幂等键重试", "kind": p.get("kind")}
            return outcome
        except (OSError, Fault) as exc:
            if sent:
                outcome = {"uncertain": True, "sent": True, "message": str(exc)}
                return outcome
            raise
        finally:
            if sent and action != "device.calibrate":
                try:
                    self._write(b"\xaa\x06\x00\x0d")
                except (OSError, Fault) as exc:
                    if outcome is not None:
                        outcome.update(uncertain=True, message="退出设置模式结果未知：" + str(exc))
                    self.log("退出设置模式未确认：" + str(exc), "warning")
