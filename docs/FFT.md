# 实时 FFT

波形页下半部显示当前可见区间末尾 N 个原始样本的频谱，每 400 ms 最多计算一次。FFT 在独立 Web Worker 内执行，采集和图表缩放继续工作。波形暂停后分析冻结数据；切换通道、数据源或参数会废弃旧的计算结果。

支持加速度 XYZ、角速度 XYZ、欧拉角及四元数 WXYZ。N 可选 256–16384，Hann、Hamming 或矩形窗；幅值可用线性或对数轴显示。去除样本均值，以窗函数 coherent gain 校正幅值。采用单边峰值幅值：正频率乘 2，DC 与奈奎斯特频点不乘 2。显示的是幅值频谱，不是 PSD；频率分辨率为 Fs/N。直流不绘制，导出仍保留 0 Hz 原始频点。对数图的显示下限不会改变导出数值。

Fs 必须是模块实际输出频率。点击“使用设备周期”将使用只读参数中 interval_ms 的倒数；未读取参数时手动填写已知 Fs。USB CDC 同批样本可能共享主机接收时间，不能用相邻主机时间差当作设备采样时钟。窗口明显缺数据或持续时间与 Fs 不符时拒绝计算；这项检查不能证明没有丢帧，因为协议没有样本序号或设备采样时间戳。

频谱支持 CSV、Excel `.xlsx` 和 MATLAB Level 5 `.mat`。导出完整 N/2+1 个频点、每轴校正后的峰值幅值及来源、窗函数、Fs、N、起止时间和单位。角速度始终使用原始 rad/s，波形显示中的 °/s 选择不改变频谱单位。

MAT 文件中的 `spectrum` 每行一频点，列为 `frequency_hz,amplitude_x,amplitude_y,amplitude_z`；四元数改为 `amplitude_w,...`。`spectrum_columns`、`unit`、`sample_rate_hz`、`window` 等变量说明分析设置。

```matlab
s = load('dmimu-spectrum.mat');
plot(s.spectrum(:,1), s.spectrum(:,2:4));
xlabel('Frequency / Hz'); ylabel('Peak amplitude / rad s^{-1}');
```

`node tests/fft_math.mjs` 用已知正弦频率/幅值、常量、奈奎斯特信号验证 FFT、窗增益与幅值倍率。实际 IMU 频谱的采样率和物理峰值仍需根据测量环境核对。
