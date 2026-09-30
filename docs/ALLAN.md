# Allan 分析

工作区从已完成的原始录制选择加速度或角速度 XYZ，计算重叠 Allan 偏差。它评估静止条件下的随机噪声随平均时间的变化，不修改设备设置，也不替代静置或六面校准。

推荐保持模块静止、温度稳定、输出频率固定，录制至少 30 分钟，最好 1 小时。短记录也能计算，但不具备相同的长时间常数支持。官方手册建议记录时长至少为关注时间常数的 10 倍，最好 100 倍；本实现默认最长平均时间不超过记录时长的十分之一。

## 使用

1. 在实时监测中录制并停止，进入 Allan 工作区。
2. 选择已完成录制和分析通道，填写录制期间的设备标称采样频率。不能填写当前回放倍速对应的接收速率。
3. 开始分析。读取阶段显示已解码帧/选中采样数，计算阶段显示聚合尺度进度；后端可取消时提供取消按钮。
4. 查看 XYZ 双对数曲线，框选缩放、双击恢复；悬停查看 τ、各轴偏差、重叠样本对数和 `T/τ`。
5. 通过结果 CSV 导出各尺度数值，或 PNG 导出当前曲线和主要估计。完整拟合依据、质量提醒和输入约定保留在分析 JSON 中。

三条曲线可独立显隐。恒定输入的偏差可以是零；双对数坐标不能绘制零值，此时页面显示明确空图说明，最小值与结果文件仍保留零。

## 时基与数据质量

主机 USB 接收时间不是传感器采样时钟。多个数据帧可能共用一次 USB 读取时间，因此时间戳重复不会被解释为无限采样频率。本算法采用用户明确输入的标称频率构造均匀采样时间；原始值不插值、不补零、不重采样。

结果列出主机重复时间、时间倒退、较大到达间隔及整段平均到达速率。若输入频率与主机平均速率相差超过 10%，或检测到明显到达缺口，会提示核对录制。到达时间诊断不能证明设备没有丢帧，存在缺口或频率变更的录制不适合据此作精度结论。

同一选定通道若包含多个 slave ID，拒绝混合作为一个传感器序列。正在录制的文件不能分析。记录读取使用既有录制格式、路径、时间顺序与 CRC 校验。

## 算法与有界资源

输入为 N 个原始 XYZ 值，采样率为 `Fs`。先减去各轴的整体常量均值，仅用于降低累计和中的数值相消，不去除线性趋势。令 `P[0]=0`、`P[k+1]=P[k]+(sample[k]-mean)`。每个整数聚合尺度 m：

```text
tau = m / Fs
pairs = N - 2*m + 1
D[i] = P[i+2*m] - 2*P[i+m] + P[i], i = 0 .. N-2*m
adev = sqrt(sum(D[i]^2) / (2 * m^2 * pairs))
```

默认最多 50 个对数分布尺度，整数化后去重。计算不跳过输入样本，使用全部重叠窗口。每通道最多 3,600,000 点，NumPy float64 累计和与平方求和以 65,536 点分块，块间检查取消。读取时使用紧凑 double 数组，避免为每点保存 Python 字典。

重叠样本对不是独立自由度。本实现不输出未经噪声模型验证的统计置信区间，也不把样本对数当成独立样本数。

依赖固定为 NumPy 2.3.5，其[官方发行说明](https://numpy.org/doc/stable/release/2.3.5-notes.html)列明支持 Python 3.11–3.14。

## 系数的含义

对 log10(τ)–log10(偏差) 进行连续区段拟合，至少四个有效点、τ 至少跨三倍。候选段需要符合目标斜率及残差要求；非零目标斜率还检查 R²。无支持区段时返回 `identified=false`、`value=null` 和原因。

| 指标 | 目标斜率 | 从该区段估算 |
| --- | --- | --- |
| 白噪声 / 角度或速度随机游走 N | −1/2 | `adev * sqrt(tau)` |
| 速率随机游走 K | +1/2 | `adev * sqrt(3/tau)` |
| 积分相位量化模型 Q | −1 | `adev * tau / sqrt(3)` |
| 速率斜坡 R | +1 | `adev * sqrt(2) / tau` |

量化模型系数不能直接等同于 IMU 输出浮点值的数值分辨率。每项都记录单位、拟合区间、实际斜率和残差。

页面同时报告实际最小偏差及其 τ。零偏不稳定性假设估计为 `minimum / sqrt(2*ln(2)/pi)`，即约除以 0.664；是否观察到近零斜率平台单独标记。最小值本身不证明闪烁噪声平台，最小值对应的 τ 只是参考时间，不能冒充独立测得的相关时间常数。

这一类 Allan 偏差算法可参考 [NIST SP 1065](https://www.nist.gov/publications/handbook-frequency-stability-analysis)；IMU 白噪声、随机游走和零偏平台换算可参考 [MathWorks 惯性传感器噪声分析](https://www.mathworks.com/help/fusion/ug/inertial-sensor-noise-analysis-using-allan-variance.html)。拟合窗口和可识别阈值属于本实现的工程选择，不声称复现官方程序的内部拟合策略。

## 输出结构与接线

`dmimu.allan.compute(samples, sample_rate, *, channel, timestamps, cancel, progress)` 接受 N×3 原始数值数组。`analyze_recording(recordings, identifier, channel, sample_rate, cancel, progress)` 流式读取选定通道。

JSON 使用 `schema="dmimu.allan"`、`version=1`，包含：

- `sample_count`、`sample_rate_hz`、`duration_s`、`sample_rate_basis`、`channel`、`unit`。
- `points[]`：`m`、`tau_s`、`pairs`、`duration_over_tau`、`deviation.{x,y,z}`。
- `metrics.{x,y,z}`：最小值、零偏假设、各拟合系数及依据。
- `quality`：时钟诊断与限制提醒；`detrended=false`、`interpolated=false`。
- 从录制读取时追加 `recording_id`、`slave_id`。

前端导出 `AllanWorkspace`，接线为：

```javascript
const allan = new AllanWorkspace(container);
allan.setHandlers({
  listRecordings: async () => recordings,
  analyze: async (params, onProgress) => resultOrAnalysisId,
  loadResult: async id => result,
  // Optional: cancel: async () => cancelCurrentAnalysis(),
  // Optional: downloadCSV: async result => downloadSavedResult(result.id),
});
allan.setVisible(true);
```

`params` 为 `{recording_id, channel, sample_rate}`。分析回调可以返回直接结果或 `{id}` / `{analysis_id}`。`setProgress()` 支持 `{stage,fraction,samples,scales_done,scales_total}`；`theme()` 重建配色，`setResult()` 支持载入既有结果。

## 验证

`tests/test_allan.py` 独立验证手算重叠窗口、直接窗口均值的参考结果、跨 65k 块边界、恒定加速度与大直流分量、白噪声已知系数、非均匀/重复主机时间不改变计算时基、非法数据、取消与多设备混流拒绝。

本机 3,600,000 点合成 XYZ 压力测试得到 48 个尺度，从生成合成数据到计算完成约 3 秒、峰值 RSS 约 207 MiB。此性能证据是离线数值计算，不包含真实录制解码，不保证其它机器有相同速度。

浏览器验证使用合成结果和伪造的 demo 录制选项，未对真实设备写参数、断开或校准。公开图片与演示数据不得混入真实传感器测量或设备元数据。
