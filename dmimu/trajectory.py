"""Bounded short-term six-axis inertial estimates; no hardware writes.

The browser and Agent share the same physical assumptions and thresholds. Host
receive time is not a device clock. A single static reference cannot separate
body bias from gravity; these outputs are estimates, never absolute positions.
"""
import copy
import math
import time

GRAVITY = 9.80665
DEFAULTS = {"maxGap": .1, "maxAttitudeAge": .05, "referenceSeconds": 2,
            "referenceSamples": 50, "maxReferenceWait": 30, "stillGyro": .035,
            "stillAcceleration": .12, "stillSeconds": .35, "maxSeconds": 120,
            "maxPoints": 12000, "pointPeriod": .01}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _vector(value):
    if not isinstance(value, dict) or not all(_finite(value.get(key)) for key in ("x", "y", "z")):
        return None
    return [value[key] for key in ("x", "y", "z")]


def _norm(vector):
    return math.hypot(*vector)


def normalize_quaternion(value):
    if not isinstance(value, dict) or not all(_finite(value.get(key)) for key in ("w", "x", "y", "z")):
        return None
    result = [value[key] for key in ("w", "x", "y", "z")]
    length = _norm(result)
    return [v / length for v in result] if .1 < length < 2 else None


def euler_quaternion(value):
    if not isinstance(value, dict) or not all(_finite(value.get(key)) for key in ("roll", "pitch", "yaw")):
        return None
    x, y, z = (value[key] * math.pi / 360 for key in ("roll", "pitch", "yaw"))
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    return [cx * cy * cz + sx * sy * sz, sx * cy * cz - cx * sy * sz,
            cx * sy * cz + sx * cy * sz, cx * cy * sz - sx * sy * cz]


def rotate_vector(vector, quaternion):
    w, x, y, z = quaternion
    a, b, c = vector
    return [(1 - 2 * (y * y + z * z)) * a + 2 * (x * y - z * w) * b + 2 * (x * z + y * w) * c,
            2 * (x * y + z * w) * a + (1 - 2 * (x * x + z * z)) * b + 2 * (y * z - x * w) * c,
            2 * (x * z - y * w) * a + 2 * (y * z + x * w) * b + (1 - 2 * (x * x + y * y)) * c]


class TrajectoryEstimator:
    """Caller serializes access (Host Service already owns its lock).

    ``consume`` accepts service history samples, not raw serial bytes. A receive
    chunk should be delivered as one batch so its same-time channels can match.
    ``snapshot``/``check_idle`` pause a running estimator after 1.2 s without
    acceleration; this is the Agent equivalent of the browser watchdog.
    """
    def __init__(self, options=None, clock=None):
        self.options = DEFAULTS | (options or {})
        self._clock = clock or time.monotonic
        self.zupt = True
        self.source, self.generation = "none", -1
        self.reset()

    def reset(self):
        self.active = self.calibrating = False
        self.reference = None
        self.reference_window = []
        self.reference_started = None
        self.reference_progress = 0
        self.position, self.velocity, self.linear = [0., 0., 0.], [0., 0., 0.], [0., 0., 0.]
        self.distance, self.points, self.last, self.origin, self.segment = 0., [], None, None, 0
        self.stationary, self.still_since = False, None
        self.q = self.euler = self.gyro = self.orientation = None
        self.orientation_kind = "none"
        self.last_seen = self.last_point_time = self._last_activity = None
        self.coalesced = self.skipped = self.accepted = self.gaps = 0
        self.message = "先保持模块静止，建立参考"
        self.time_basis = "recorded_time" if self.source == "playback" else "host_receive_time"

    def set_source(self, source, generation):
        if source not in {"none", "live", "demo", "playback"} or type(generation) is not int:
            raise ValueError("无效的轨迹来源或代次")
        if self.source == source and self.generation == generation:
            return False
        self.source, self.generation = source, generation
        self.reset()
        self.message = "数据源已变化；轨迹已清空，请重新建立静止参考"
        return True

    def set_options(self, options):
        if not isinstance(options, dict) or set(options) != {"zupt"} or type(options["zupt"]) is not bool:
            raise ValueError("轨迹选项仅接受布尔值 zupt")
        self.zupt = options["zupt"]
        self.still_since, self.stationary = None, False
        return {"zupt": self.zupt}

    def begin_reference(self):
        if self.source == "none":
            raise ValueError("先选择数据源并建立静止参考")
        self.reset()
        self.calibrating = True
        self._last_activity = self._clock()
        self.message = "保持静止至少 2 秒；这是上位机估计，不修改模块校准"

    def start(self):
        if not self.reference or self.source == "none":
            self.message = "先选择数据源并建立静止参考"
            return False
        if len(self.points) >= self.options["maxPoints"] or (self.points and self.points[-1]["elapsed"] >= self.options["maxSeconds"]):
            self.message = "已达到本次追踪上限，请清空后重新建立参考"
            return False
        self.active, self.calibrating = True, False
        self.velocity, self.last, self.still_since, self.stationary = [0., 0., 0.], None, None, False
        self.segment += 1
        self._last_activity = self._clock()
        self.message = "追踪中 · 六轴积分估算，会随时间漂移"
        return True

    def pause(self, reason="已暂停；恢复时速度从零开始"):
        self.active = self.calibrating = False
        self.last, self.velocity, self.still_since, self.stationary = None, [0., 0., 0.], None, False
        self.message = reason

    def gap(self, reason="数据有缺口，已暂停；不会跨缺口积分"):
        self.gaps += 1
        self.pause(reason)

    def check_idle(self):
        if ((self.active or self.calibrating) and self._last_activity is not None
                and self._clock() - self._last_activity > 1.2):
            self.gap("超过 1.2 秒没有收到采样，已暂停")
            return True
        return False

    def consume(self, samples):
        groups = {}
        for sample in samples:
            if not isinstance(sample, dict) or sample.get("channel") not in {"acceleration", "angular_velocity", "quaternion", "euler"}:
                continue
            stamp = sample.get("measurement_time") if self.source == "playback" else sample.get("time")
            if not _finite(stamp):
                if self.source == "playback" and (self.active or self.calibrating):
                    self.gap("回放缺少原始采样时间，已暂停；不能用回放发送时间积分")
                continue
            host = sample.get("time")
            if not _finite(host):
                continue
            identifier = sample.get("slave_id")
            group = groups.setdefault(stamp, {"time": stamp, "host_time": host, "acc": [], "id": identifier})
            if group["id"] != identifier:
                group["mixed"] = True
                continue
            channel, value = sample["channel"], sample.get("values")
            if channel == "acceleration":
                vector = _vector(value)
                if vector is not None:
                    group["acc"].append(vector)
            elif channel == "angular_velocity":
                vector = _vector(value)
                if vector is not None:
                    group["gyro"] = vector
            elif channel == "quaternion":
                quat = normalize_quaternion(value)
                if quat is not None:
                    group["q"] = quat
            else:
                quat = euler_quaternion(value)
                if quat is not None:
                    group["euler"] = quat
        for stamp in sorted(groups):
            self._consume_group(groups[stamp])

    def _consume_group(self, group):
        stamp = group["time"]
        for name in ("q", "euler", "gyro"):
            if name in group:
                setattr(self, name, {"time": stamp, "value": group[name], "id": group["id"]})
        if not group["acc"]:
            return
        if self.last_seen is not None and stamp <= self.last_seen:
            self.skipped += 1
            return
        self.last_seen, self._last_activity = stamp, self._clock()
        self.coalesced += max(0, len(group["acc"]) - 1)
        if group.get("mixed"):
            if self.active or self.calibrating:
                self.gap("同时间出现不同从机 ID，已暂停")
            return
        def valid(record):
            return (record is not None and record["id"] == group["id"]
                    and -1e-9 <= stamp - record["time"] <= self.options["maxAttitudeAge"])
        if valid(self.q):
            attitude, kind = self.q["value"], "quaternion"
        elif valid(self.euler):
            attitude, kind = self.euler["value"], "euler_zyx"
        else:
            self.skipped += 1
            if self.active or self.calibrating:
                self.gap("姿态数据缺失或相差超过 50 ms，已暂停")
            return
        self.orientation, self.orientation_kind = attitude, kind
        acceleration = [sum(row[i] for row in group["acc"]) / len(group["acc"]) for i in range(3)]
        world = rotate_vector(acceleration, attitude)
        gyro = _norm(self.gyro["value"]) if valid(self.gyro) else None
        if not all(map(_finite, world)) or _norm(world) > 500:
            if self.active or self.calibrating:
                self.gap("加速度异常，已暂停")
            return
        if self.calibrating:
            self._capture_reference(stamp, world, gyro)
            return
        if not self.active:
            return
        if len(self.points) >= self.options["maxPoints"]:
            self.pause("轨迹点数达到 12000 上限，请先导出，再清空重试")
            return
        linear = [value - self.reference[i] for i, value in enumerate(world)]
        self.linear = linear
        if gyro is not None and gyro < self.options["stillGyro"] and _norm(linear) < self.options["stillAcceleration"]:
            if self.still_since is None:
                self.still_since = stamp
            self.stationary = self.zupt and stamp - self.still_since >= self.options["stillSeconds"]
        else:
            self.still_since, self.stationary = None, False
        if self.origin is not None and stamp - self.origin >= self.options["maxSeconds"]:
            self.pause("本次追踪达到 120 秒上限；长时间积分漂移明显，请清空后重试")
            return
        if self.last is None:
            if self.origin is None:
                self.origin = stamp
            self.last = {"time": stamp, "acc": linear}
            self._append_point(stamp, group["host_time"])
            return
        dt = stamp - self.last["time"]
        if dt > self.options["maxGap"]:
            self.gap("采样间隔超过 100 ms，已暂停；恢复时重新从零速度积分")
            return
        old = self.position.copy()
        if self.stationary:
            self.velocity = [0., 0., 0.]
        else:
            for i in range(3):
                average = (self.last["acc"][i] + linear[i]) / 2
                self.position[i] += self.velocity[i] * dt + .5 * average * dt * dt
                self.velocity[i] += average * dt
        self.distance += _norm([value - old[i] for i, value in enumerate(self.position)])
        self.last = {"time": stamp, "acc": linear}
        self.accepted += 1
        self._append_point(stamp, group["host_time"])

    def _capture_reference(self, stamp, world, gyro):
        if self.reference_started is None:
            self.reference_started = stamp
        if stamp - self.reference_started > self.options["maxReferenceWait"]:
            self.pause("30 秒内未获得稳定参考；检查加速度、角速度和姿态通道")
            return
        magnitude = _norm(world)
        aligned = world[2] > 0 and world[2] / magnitude > math.cos(15 * math.pi / 180)
        if gyro is None or gyro > self.options["stillGyro"] or abs(magnitude - GRAVITY) > .6 or not aligned:
            self.reference_window, self.reference_progress = [], 0
            self.message = ("等待同一从机的角速度数据" if gyro is None else
                            "世界重力方向不符合 +Z 约定；请核对姿态和安装方向" if not aligned else "检测到运动，请保持模块静止")
            return
        if self.reference_window and stamp - self.reference_window[-1]["time"] > self.options["maxGap"]:
            self.reference_window = []
        if self.reference_window:
            initial = self.reference_window[0]["world"]
            if _norm([v - initial[i] for i, v in enumerate(world)]) > .15:
                self.reference_window, self.reference_progress = [], 0
                self.message = "加速度不稳定，重新累计静止时间"
        self.reference_window.append({"time": stamp, "world": world})
        elapsed = stamp - self.reference_window[0]["time"]
        self.reference_progress = min(1, elapsed / self.options["referenceSeconds"])
        if elapsed < self.options["referenceSeconds"] or len(self.reference_window) < self.options["referenceSamples"]:
            return
        self.reference = [sum(row["world"][i] for row in self.reference_window) / len(self.reference_window) for i in range(3)]
        self.reference_window, self.calibrating, self.reference_progress = [], False, 1
        self.message = "静止参考已建立；点击开始追踪。参考只适用于本次短时估计"

    def _append_point(self, stamp, host):
        if self.last_point_time is not None and stamp - self.last_point_time < self.options["pointPeriod"]:
            return
        self.last_point_time = stamp
        self.points.append({"time": stamp, "host_time": host, "elapsed": stamp - self.origin,
                            "segment": self.segment, "position": self.position.copy(), "velocity": self.velocity.copy(),
                            "acceleration": self.linear.copy(), "distance": self.distance, "stationary": self.stationary,
                            "orientation_kind": self.orientation_kind, "quaternion": self.orientation.copy()})

    def snapshot(self):
        self.check_idle()
        return {"active": self.active, "calibrating": self.calibrating, "reference": copy.copy(self.reference),
                "referenceProgress": self.reference_progress, "position": self.position.copy(), "velocity": self.velocity.copy(),
                "acceleration": self.linear.copy(), "distance": self.distance, "elapsed": self.points[-1]["elapsed"] if self.points else 0,
                "stationary": self.stationary, "source": self.source, "generation": self.generation, "points": len(self.points),
                "coalesced": self.coalesced, "skipped": self.skipped, "gaps": self.gaps, "message": self.message,
                "timeBasis": self.time_basis, "orientationKind": self.orientation_kind, "zupt": self.zupt}

    def export_payload(self, format):
        if format not in {"mat", "xlsx", "csv"}:
            raise ValueError("轨迹导出格式必须为 mat、xlsx 或 csv")
        return {"format": format, "source": self.source, "generation": self.generation,
                "gravity_reference": self.reference.copy() if self.reference else [0., 0., GRAVITY],
                "timing": self.time_basis, "estimate": True, "points": copy.deepcopy(self.points)}
