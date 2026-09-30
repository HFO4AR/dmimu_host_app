"""Single serial owner, telemetry, reconnect, recordings and serialized actions."""
import base64
import bisect
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
from .storage import atomic_json


class Fault(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


class Service:
    ACTIONS = ("connect", "disconnect", "demo", "record.start", "record.stop", "record.export", "playback.open", "playback.control", "device.configure", "device.calibrate", "device.angle-zero", "device.yaw-zero", "device.read-settings", "protocol.probe")

    def __init__(self, settings, serial_factory=serial.Serial, port_provider=list_ports.comports):
        self.settings = settings
        self.serial_factory, self.port_provider = serial_factory, port_provider
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.reader = self.worker = None
        self.jobs = queue.Queue(maxsize=32)
        self.decoder = Decoder(settings.values["legacy_crc"])
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
        self.reader.start()
        self.worker.start()

    def close(self):
        self.stop_event.set()
        if self.reader:
            self.reader.join(timeout=2)
        if self.worker:
            self.worker.join()
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
        return {"version": "1", "model": "DM-IMU-L1", "actions": list(self.ACTIONS), "single_device": True, "sources": ["live", "demo", "playback"], "units": UNITS,
                "protocol_probe": {"experimental": True, "requests": "V1 documented setting-status request only", "requires_acknowledged": True},
                "device_control": {"profile": self.settings.values["protocol"], "supported": legacy, "verification": "legacy-status-frame" if legacy else "unavailable", "calibration_result_available": False, "reason": None if legacy else "新版 2.x 控制协议未公开；确认设备为 1.x 后可在网页选择旧版协议", "unsupported": ["installation_rotation", "sensor_range", "v2_configuration", "v2_calibration"]}}

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
            return {"source": self.source, "generation": self.generation, "connection": self.connection, "port": self.port, "error": self.error, "seq": self.seq, "host_time": now, "channels": channels, "auto_connect": self.auto_connect,
                    "statistics": {"frames": self.decoder.frames, "crc_errors": self.decoder.crc_errors, "discarded_bytes": self.decoder.discarded_bytes, "uptime_s": time.monotonic() - self.started},
                    "recording": {"id": self.recordings.active, "bytes": self.recordings.bytes, "error": self.record_error}, "playback": replay}

    def samples_since(self, after=0):
        with self.lock:
            data = [s for s in self.history if s["seq"] > after]
            return {"samples": data, "generation": self.generation, "seq": self.seq, "truncated": bool(self.history and after and after < self.history[0]["seq"] - 1)}

    def _ingest(self, raw, stamp, link=None):
        with self.lock:
            if link is not None and link is not self.serial:
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
            for frame in self.decoder.feed(raw):
                self._frame(frame, stamp)

    def _frame(self, frame, stamp):
        self.seq += 1
        if self.source == "playback":
            self.decoder.frames += 1
        item = {"seq": self.seq, "time": stamp, "channel": frame.channel, "values": frame.values, "slave_id": frame.slave_id}
        self.latest[frame.channel] = {"values": frame.values, "updated_at": stamp, "unit": UNITS.get(frame.channel, ""), "slave_id": frame.slave_id, "frame_length": len(frame.raw)}
        times = self.receive_times.setdefault(frame.channel, deque(maxlen=2000))
        times.append(stamp)
        self.history.append(item)
        if self.source == "live":
            self.connection = "streaming"
            self.error = None

    def _clear_data(self):
        self.generation += 1
        self.latest.clear()
        self.receive_times.clear()
        self.history.clear()
        self.decoder = Decoder(self.settings.values["legacy_crc"])

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
                        if time.time() - latest_time > 2:
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
                    if self.auto_connect and now >= self.next_scan:
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
            if len(self.operations) >= 10000:
                raise Fault("OPERATION_LIMIT", "操作记录已达上限，请归档数据目录后再运行", 409)
            op = {"id": uuid.uuid4().hex, "key": key, "signature": signature, "action": action, "params": params, "state": "queued", "created_at": time.time()}
            self.operations[op["id"]] = op
            self.keys[key] = op["id"]
            try:
                self.jobs.put_nowait(op["id"])
            except queue.Full:
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

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                identifier = self.jobs.get(timeout=.2)
            except queue.Empty:
                continue
            with self.lock:
                op = self.operations[identifier]
                op.update(state="running", started_at=time.time())
                self._save_operations()
            try:
                result = self._perform(op["action"], op["params"])
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
                self._save_operations()
            self.jobs.task_done()

    def _perform(self, action, p):
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
                return {"id": self.recordings.start(self.source, self.port, self.decoder.legacy_crc), "source": self.source}
        if action == "record.stop":
            with self.lock:
                return {"id": self.recordings.stop()}
        if action == "record.export":
            with self.lock:
                if p.get("id") == self.recordings.active:
                    raise Fault("RECORDING_ACTIVE", "先停止录制，再导出")
            return self.recordings.export(p["id"], self.stop_event)
        if action == "playback.open":
            return self._open_playback(p["id"])
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
        return self._device_action(action, p)

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

    def _open_playback(self, identifier):
        path = self.recordings.path(identifier)
        if path.stat().st_size > 100 * 1024 * 1024:
            raise Fault("REPLAY_TOO_LARGE", "首版回放限制为 100 MiB，较大录制仍可导出 CSV")
        with self.lock:
            if self.recordings.writer:
                raise Fault("RECORDING_ACTIVE", "先停止录制，再回放")
        samples = []
        for sample in self.recordings.samples(identifier):
            if self.stop_event.is_set():
                raise Fault("CANCELLED", "回放加载已取消")
            if len(samples) >= 500000:
                raise Fault("REPLAY_TOO_LARGE", "首版回放最多 50 万帧，较大录制仍可导出 CSV")
            samples.append(sample)
        if not samples:
            raise Fault("EMPTY_RECORDING", "录制中没有有效帧")
        with self.lock:
            self._close_serial()
            self.auto_connect = False
            self.source, self.connection, self.error = "playback", "playback", None
            self._clear_data()
            self.playback = {"id": identifier, "samples": samples, "times": [s[0] for s in samples], "channels": {s[2].channel for s in samples}, "index": 0, "position": 0, "duration": samples[-1][0], "playing": False, "speed": 1, "last_tick": time.monotonic()}
            self._seek(samples[0][0])
        return {"id": identifier, "duration": self.playback["duration"], "samples": len(samples)}

    def _seek(self, position):
        pb = self.playback
        self._clear_data()
        end = bisect.bisect_right(pb["times"], position)
        latest = {}
        for index in range(end - 1, -1, -1):
            frame = pb["samples"][index][2]
            latest.setdefault(frame.channel, frame)
            if len(latest) == len(pb["channels"]):
                break
        for frame in latest.values():
            self._frame(frame, time.time())
        pb["position"], pb["index"] = position, end

    def _tick_playback(self):
        with self.lock:
            pb = self.playback
            if not pb or not pb["playing"]:
                return
            now = time.monotonic()
            pb["position"] = min(pb["duration"], pb["position"] + (now - pb["last_tick"]) * pb["speed"])
            pb["last_tick"] = now
            while pb["index"] < len(pb["samples"]) and pb["samples"][pb["index"]][0] <= pb["position"]:
                elapsed, stamp, frame = pb["samples"][pb["index"]]
                self._frame(frame, time.time())
                pb["index"] += 1
            if pb["position"] >= pb["duration"]:
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

    def _device_action(self, action, p):
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
