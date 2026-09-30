"""Overlapping Allan deviation for original, uniformly sampled IMU measurements.

Host USB arrival timestamps are diagnostic metadata, not device sample clocks.
A caller must supply the nominal sample rate. No interpolation or detrending is
performed; subtracting a constant mean only improves prefix-sum precision.
"""
from array import array
import math
import time

import numpy as np

MAX_SAMPLES = 3_600_000
BLOCK = 65_536
AXES = ("x", "y", "z")
CHANNELS = {"acceleration": "m/s²", "angular_velocity": "rad/s"}
BI_SCALE = math.sqrt(2 * math.log(2) / math.pi)


def _check(cancel):
    if cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
        raise ValueError("Allan 分析已取消")


def _notify(progress, stage, fraction, **extra):
    if progress is not None:
        progress({"stage": stage, "fraction": float(fraction), **extra})


def _fit(tau, deviation, target):
    valid = np.isfinite(deviation) & (deviation > 0)
    if np.count_nonzero(valid) < 4:
        return {"identified": False, "reason": "没有足够的正值偏差点"}
    valid_indices = np.flatnonzero(valid)
    x = np.log10(tau[valid])
    y = np.log10(deviation[valid])
    best = None
    # Require a continuous segment covering at least a factor of three in tau.
    # Fixed-slope intercepts define the coefficient; fitted slopes assess whether
    # the record actually supports that model instead of forcing every parameter.
    for first in range(len(x) - 3):
        for end in range(first + 4, min(len(x), first + 12) + 1):
            if np.any(np.diff(valid_indices[first:end]) != 1):
                continue
            xx, yy = x[first:end], y[first:end]
            if xx[-1] - xx[0] < math.log10(3):
                continue
            cx, cy = xx - xx.mean(), yy - yy.mean()
            slope = float(np.dot(cx, cy) / np.dot(cx, cx))
            intercept = float(np.mean(yy - target * xx))
            residual = yy - (target * xx + intercept)
            rms = float(np.sqrt(np.mean(residual * residual)))
            tolerance = .12 if target == 0 else .16
            if abs(slope - target) > tolerance or rms > .10:
                continue
            total = float(np.dot(cy, cy))
            r2 = 1 - float(np.dot(yy - (slope * xx + yy.mean() - slope * xx.mean()), yy - (slope * xx + yy.mean() - slope * xx.mean()))) / total if total > 1e-20 else None
            if target != 0 and (r2 is None or r2 < .85):
                continue
            score = abs(slope - target) + rms
            if best is None or score < best[0]:
                best = (score, {"identified": True, "slope": slope, "model_slope": target,
                               "intercept_log10": intercept, "tau_start_s": float(10 ** xx[0]),
                               "tau_end_s": float(10 ** xx[-1]), "fit_points": len(xx),
                               "r2": r2, "residual_log10_rms": rms})
    return best[1] if best else {"identified": False, "reason": "没有足够跨度且符合斜率的连续区段"}


def _metrics(tau, values, channel):
    gyro = channel == "angular_velocity"
    definitions = {
        "white_noise": (-.5, "rad/√s" if gyro else "m/s/√s", 1),
        "rate_random_walk": (.5, "rad/s^(3/2)" if gyro else "m/s^(5/2)", math.sqrt(3)),
        "quantization": (-1, "rad" if gyro else "m/s", 1 / math.sqrt(3)),
        "rate_ramp": (1, "rad/s²" if gyro else "m/s³", math.sqrt(2)),
    }
    result = {}
    for index, axis in enumerate(AXES):
        v = values[:, index]
        minimum = int(np.argmin(v))
        plateau = _fit(tau, v, 0)
        parameters = {"minimum": {"deviation": float(v[minimum]), "tau_s": float(tau[minimum])},
                      "bias_instability": {"value": float(v[minimum] / BI_SCALE), "unit": CHANNELS[channel],
                                           "identified": plateau["identified"], "scale": BI_SCALE,
                                           "basis": "minimum_allan_deviation / sqrt(2*ln(2)/pi)",
                                           "assumption": "静止闪烁噪声平台模型估计；曲线最小值本身不证明零偏不稳定性",
                                           "plateau_fit": plateau},
                      "reference_time_s": float(tau[minimum]),
                      "reference_time_note": "最小偏差对应时间；不是独立测得的自相关时间常数"}
        for name, (slope, unit, factor) in definitions.items():
            fit = _fit(tau, v, slope)
            fit["unit"] = unit
            fit["value"] = float(10 ** fit["intercept_log10"] * factor) if fit["identified"] else None
            if name == "quantization":
                fit["assumption"] = "积分相位量化模型的 −1 斜率估计；不能与输出 rate 的数值分辨率等同"
            parameters[name] = fit
        result[axis] = parameters
    return result


def _host_quality(timestamps, rate, n):
    quality = {"host_time_is_device_clock": False, "warnings": []}
    if timestamps is None:
        return quality
    stamps = np.asarray(timestamps, dtype=np.float64)
    if stamps.shape != (n,) or not np.all(np.isfinite(stamps)):
        raise ValueError("主机时间戳必须为有限的一维数组，且与样本数一致")
    duplicate = backwards = gaps = 0
    for start in range(1, n, BLOCK):
        end = min(n, start + BLOCK)
        delta = stamps[start:end] - stamps[start - 1:end - 1]
        duplicate += int(np.count_nonzero(delta == 0))
        backwards += int(np.count_nonzero(delta < 0))
        gaps += int(np.count_nonzero(delta > max(1., 100 / rate)))
    duration = float(stamps[-1] - stamps[0])
    quality.update({"host_duration_s": duration, "duplicate_host_timestamps": duplicate,
                    "backwards_host_timestamps": backwards, "large_host_gaps": gaps,
                    "host_arrival_rate_estimate_hz": (n - 1) / duration if duration > 0 else None})
    if duplicate:
        quality["warnings"].append("USB 批量帧存在相同主机到达时间；计算采用显式采样率而不是逐点主机时间间隔")
    if backwards:
        quality["warnings"].append("主机接收时间发生倒退；主机时钟不能证明传感器连续采样")
    if gaps:
        quality["warnings"].append("存在超过阈值的主机到达间隔；可能有掉线或缺帧，不会自动补零或插值")
    if duration > 0 and abs((n - 1) / duration - rate) / rate > .1:
        quality["warnings"].append("主机平均到达速率与输入采样率相差超过 10%；核对输出频率和录制缺口")
    return quality


def compute(samples, sample_rate, *, channel="angular_velocity", timestamps=None,
            cancel=None, progress=None, m_values=None):
    """Return JSON-compatible results; rows are original [x,y,z] measurements.

    Explicit m_values support reference validation. Production defaults cover
    log-spaced integer cluster sizes up to N/10, with no skipped input samples.
    cancel is an Event or a zero-argument predicate; progress receives a dict.
    """
    _check(cancel)
    if channel not in CHANNELS:
        raise ValueError("Allan 分析只支持加速度或角速度")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, float)) or not math.isfinite(sample_rate) or not 0 < sample_rate <= 10000:
        raise ValueError("显式采样频率必须大于 0 且不超过 10,000 Hz")
    if hasattr(samples, "__len__") and len(samples) > MAX_SAMPLES:
        raise ValueError("Allan 分析每通道最多 3,600,000 点")
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3 or values.shape[0] < 4:
        raise ValueError("至少需要四个原始 XYZ 采样点")
    n = values.shape[0]
    if n > MAX_SAMPLES:
        raise ValueError("Allan 分析每通道最多 3,600,000 点")
    if m_values is None:
        scales = np.unique(np.rint(np.geomspace(1, max(1, (n - 1) // 10), 50)).astype(np.int64))
    else:
        raw_scales = list(m_values)
        if not raw_scales or len(raw_scales) > 100 or any(isinstance(m, bool) or not isinstance(m, (int, np.integer)) or not 1 <= m <= n // 2 for m in raw_scales):
            raise ValueError("无效的 Allan 聚合尺度")
        scales = np.array(sorted(set(raw_scales)), dtype=np.int64)
    total = np.zeros(3, dtype=np.float64)
    for start in range(0, n, BLOCK):
        _check(cancel)
        block = values[start:start + BLOCK]
        if not np.all(np.isfinite(block)):
            raise ValueError("Allan 采样值必须是有限数值")
        total += block.sum(axis=0)
        _notify(progress, "validate", .03 * min(1, (start + BLOCK) / n), samples=n)
    mean = total / n
    prefix = np.empty((n + 1, 3), dtype=np.float64)
    prefix[0] = 0
    for start in range(0, n, BLOCK):
        _check(cancel)
        end = min(n, start + BLOCK)
        prefix[start + 1:end + 1] = np.cumsum(values[start:end] - mean, axis=0) + prefix[start]
        _notify(progress, "integrate", .03 + .07 * end / n, samples=n)
    output = []
    for scale_index, m in enumerate(scales):
        m = int(m)
        pairs = n - 2 * m + 1
        sums = np.zeros(3, dtype=np.float64)
        for start in range(0, pairs, BLOCK):
            _check(cancel)
            end = min(pairs, start + BLOCK)
            delta = prefix[start + 2 * m:end + 2 * m] - 2 * prefix[start + m:end + m]
            delta += prefix[start:end]
            sums += np.sum(delta * delta, axis=0)
            _notify(progress, "deviation", .1 + .85 * (scale_index + end / pairs) / len(scales), scale=m, scales_done=scale_index, scales_total=len(scales))
        deviation = np.sqrt(sums / (2 * m * m * pairs))
        if not np.all(np.isfinite(deviation)):
            raise ValueError("Allan 数值超出可计算范围")
        output.append({"m": m, "tau_s": m / sample_rate, "pairs": pairs,
                       "duration_over_tau": (n - 1) / m,
                       "deviation": {axis: float(deviation[i]) for i, axis in enumerate(AXES)}})
    _check(cancel)
    tau = np.array([p["tau_s"] for p in output])
    deviations = np.array([[p["deviation"][a] for a in AXES] for p in output])
    quality = _host_quality(timestamps, sample_rate, n)
    duration = (n - 1) / sample_rate
    if duration < 1800:
        quality["warnings"].append("记录短于推荐的 30 分钟；长时间常数和噪声系数仅用于初步观察")
    quality["warnings"].append("样本对是重叠的，不代表独立自由度；此结果没有统计置信区间")
    result = {"schema": "dmimu.allan", "version": 1, "channel": channel, "axes": list(AXES),
              "unit": CHANNELS[channel], "sample_count": n, "sample_rate_hz": float(sample_rate),
              "sample_rate_basis": "explicit_user_nominal_rate", "duration_s": duration,
              "method": "overlapping_allan_deviation", "mean_removed": mean.tolist(),
              "detrended": False, "interpolated": False, "points": output,
              "metrics": _metrics(tau, deviations, channel), "quality": quality,
              "created_at": time.time()}
    _notify(progress, "complete", 1., samples=n)
    return result


def analyze_recording(recordings, identifier, channel, sample_rate, cancel=None, progress=None):
    """Stream one channel from a completed recording, then analyze its XYZ data.

    Multi-device recordings are rejected so two slave streams cannot be silently
    interpreted as one uniformly sampled sensor. Existing samples() validates the
    recording path/header/CRC and excludes active recordings.
    """
    if channel not in CHANNELS:
        raise ValueError("Allan 分析只支持加速度或角速度")
    raw = array("d")
    stamps = array("d")
    slave = None
    decoded = 0
    for elapsed, stamp, frame in recordings.samples(identifier):
        _check(cancel)
        decoded += 1
        if frame.channel == channel:
            if slave is None:
                slave = frame.slave_id
            elif slave != frame.slave_id:
                raise ValueError("录制包含多个设备；不能混合作为一个传感器的 Allan 数据")
            if len(stamps) >= MAX_SAMPLES:
                raise ValueError("Allan 分析每通道最多 3,600,000 点")
            raw.extend(frame.values[a] for a in AXES)
            stamps.append(stamp)
        if decoded % 4096 == 0:
            _notify(progress, "read_recording", 0., decoded_frames=decoded, samples=len(stamps))
    _check(cancel)
    _notify(progress, "read_recording", 0., decoded_frames=decoded, samples=len(stamps))
    values = np.frombuffer(raw, dtype=np.float64).reshape(-1, 3)
    result = compute(values, sample_rate, channel=channel,
                     timestamps=np.frombuffer(stamps, dtype=np.float64), cancel=cancel, progress=progress)
    result.update({"recording_id": identifier, "slave_id": slave})
    return result
