# Agent 使用指南

## 入口与预检

在项目目录使用 `python3 agent_cli.py`；Windows 可用 `py -3`。所有全局选项放在子命令之前。

```bash
python3 agent_cli.py capabilities
python3 agent_cli.py status
python3 agent_cli.py ports
```

检查 `ok`、`data.source`、`data.connection` 与每个通道的 `stale`。`source` 必须是 `live` 才能把数据作为实机观测；`demo` 和 `playback` 不证明当前设备状态。

同用户 CLI 自动读取用户状态目录中的私有服务描述和 Token。禁用接入、凭据错误或服务未启动时报告错误，不改浏览器设置绕过。Agent 不直接打开串口。

自定义目录和远程访问：

```bash
python3 agent_cli.py --data-dir /private/path capabilities
python3 agent_cli.py --host-url https://imu.example.com --token-file /private/agent-token capabilities
```

Token 文件是 `{"token":"…"}` JSON，必须私有。不要将内容输出到终端、聊天、仓库或日志。CLI 使用系统 CA 验证 HTTPS，不提供跳过证书验证的开关。

## 操作与幂等

通过 `action NAME --params JSON --idempotency-key KEY` 提交操作；默认等到终态，`--no-wait` 只提交。复杂参数尤其在 Windows 下使用 UTF-8 JSON 文件：

```bash
python3 agent_cli.py action connect --params-file connect.json --idempotency-key connection-001
```

`connect.json` 示例为 `{"port":"COM5"}`，Linux 使用实际 `/dev/ttyACM*` 或 `/dev/ttyUSB*`。

操作状态：`queued`、`running`、`succeeded`、`failed`、`uncertain`。同一个键和相同参数返回原操作；同键不同参数返回 `IDEMPOTENCY_CONFLICT`。

```bash
python3 agent_cli.py operation OPERATION_ID
```

超时或 `uncertain` 时保存操作 ID 与幂等键，查询原操作；没有收到操作 ID 时可用**完全相同参数和原键**重试提交。不要换键重复设备写操作。服务重启后，未结束的操作恢复为 `uncertain`，不会自动重放。操作日志持久化，最多 1 万条。

## 采集、录制与回放

```bash
python3 agent_cli.py telemetry
python3 agent_cli.py action record.start --idempotency-key recording-001
python3 agent_cli.py action record.stop --idempotency-key recording-stop-001
python3 agent_cli.py recordings
python3 agent_cli.py action record.export --params '{"id":"RECORDING_ID"}' --idempotency-key export-001
python3 agent_cli.py download RECORDING_ID --format csv --output /tmp/imu.csv
python3 agent_cli.py download RECORDING_ID --format raw --output /tmp/imu.dmimulog
python3 agent_cli.py download RECORDING_ID --format imulog --output /tmp/imu.imulog
python3 agent_cli.py recording-import --input /private/official.imulog
python3 agent_cli.py action playback.open --params '{"id":"RECORDING_ID"}' --idempotency-key replay-001
python3 agent_cli.py action playback.control --params '{"playing":true,"speed":2}' --idempotency-key play-001
python3 agent_cli.py action playback.control --params '{"position":1.2,"playing":false}' --idempotency-key seek-001
```

官方 `.imulog` 二进制容器和工作台 `.dmimulog` 支持导入；native 格式已对照官方读写机器码并进行合成 roundtrip，尚未在 Windows 官方 EXE 中实测打开工作台导出的文件。导入大小1GiB；详情见 [录制格式](RECORDINGS.md)。

先停止录制再导出、下载或切换来源。下载拒绝覆盖现有文件。回放会释放真实串口并暂停自动连接；恢复采集需显式 `connect`。

原始单位：加速度 m/s²、角速度 rad/s、欧拉角 deg、四元数 w/x/y/z、温度 °C。`time` 是当前服务接收 Unix 秒，`measurement_time` 在回放中保留录制时的原始接收时间；分析回放时优先使用后者，倍速不改变物理时基。两者都不是设备同步时钟；不要把不同通道更新时间当成同一采样时刻。

## 新版设备读取与控制

先检查 `capabilities.data.device_control`。默认连接被动接收，显式 `device.inspect` 通过已核实的新版只读指令读取 Boot/APP 版本、配置、静止和六面状态，确认 APP>=2 才开放新版写能力。版本未知不会套用旧控制。

```bash
python3 agent_cli.py action device.inspect --idempotency-key inspect-001
python3 agent_cli.py action device.read-settings --idempotency-key settings-read-001
python3 agent_cli.py action device.calibration-status --idempotency-key calibration-status-001
```

以下示例只说明调用格式；实际设备修改应在用户已授权具体操作后执行：

```bash
python3 agent_cli.py action device.configure --params '{"interval_ms":10,"euler_enabled":true}' --idempotency-key configure-001
python3 agent_cli.py action device.calibrate --params '{"kind":"gyro","acknowledged":true}' --idempotency-key gyro-calibrate-001
python3 agent_cli.py action device.calibrate --params '{"kind":"six-face","acknowledged":true}' --idempotency-key six-face-001
python3 agent_cli.py action device.calibration-abort --params '{"acknowledged":true}' --idempotency-key six-abort-001
python3 agent_cli.py action device.yaw-zero --params '{"acknowledged":true}' --idempotency-key yaw-zero-001
python3 agent_cli.py action device.factory-reset --params '{"acknowledged":true}' --idempotency-key factory-001
```

V2 `device.configure` 接受：acceleration_enabled、gyro_enabled、euler_enabled、quaternion_enabled、can_active、interval_ms、heating_enabled、target_temperature、slave_id、master_id、communication、can_baudrate、uart_baudrate、installation_rotation、accel_range、gyro_range。取值与能力见 [V2协议](V2_PROTOCOL.md)。安装方向/量程仅在当前固件回报这些字段时可改。切换接口可能停止 USB 测量。

全部参数先校验，进入设置模式后逐条等待ACK，再回读匹配才保存。`persistent_storage_verified=false`；保存确认不证明断电存储。改变安装方向/量程需重新校准并重新建立轨迹参考。

校准启动终态成功不代表物理完成：result中的 `started=true,completed=false` 只表示设备接受开始。后台读取校准状态，或调用 `device.calibration-status`；state=3/4分别为设备完成/失败，核对结果有效标记/error。超时、中断返回 `uncertain`，不重发。六面取消是独立已确认命令。

旧版1.x仅在本机页面明确选择 `legacy-v1` 后开放历史公开指令；无完成回报时校准返回 uncertain。`device.angle-zero` 是旧版角度置零，不替代新版独立航向归零。

## Allan 分析与数据导出

```bash
python3 agent_cli.py --timeout 600 action allan.analyze --params '{"recording_id":"RECORDING_ID","channel":"angular_velocity","sample_rate":1000}' --idempotency-key allan-001 --no-wait
python3 agent_cli.py operation ANALYSIS_OPERATION_ID
python3 agent_cli.py action allan.cancel --params '{"operation_id":"ANALYSIS_OPERATION_ID"}' --idempotency-key allan-cancel-001
```

分析采用已完成原始录制，sample_rate填写录制时标称输出频率；不把USB批量到达时间反推为同步设备时钟。操作progress显示阶段，完成后result.id标识 `/api/agent/v1/analyses/ID` JSON及 `/csv` 附件。取消请求不是分析完成，继续查原操作。

波形 `/waveforms/export`、频谱 `/spectra/export`、估算轨迹 `/trajectories/export` 支持 MAT/XLSX/CSV 二进制下载，使用同一 Agent Bearer 认证。payload与大小见 [API](API.md)。CLI已封装文件响应并拒绝覆盖：

```bash
python3 agent_cli.py analysis-download ANALYSIS_ID --format csv --output /private/allan.csv
python3 agent_cli.py waveform-export --input /private/waveform.json --format mat --output /private/waveform.mat
python3 agent_cli.py spectrum-export --input /private/spectrum.json --format xlsx --output /private/spectrum.xlsx
python3 agent_cli.py trajectory-export --input /private/trajectory.json --format csv --output /private/trajectory.csv
```

输入JSON携带该接口需要的原始数组和元数据，CLI按 `--format` 选择文件类型，不改变数值。网页显示单位不改变导出原始单位。

## Agent 持续三维轨迹

Service 是唯一常驻估计器，网页和 Agent 共用相同状态。关闭网页不会终止已显式启动的任务；来源变化、重连或回放跳转清空参考与点集。没有数据会由服务watchdog停止，位置不会被无限外推。

```bash
python3 agent_cli.py action trajectory.reference --idempotency-key trajectory-ref-001
python3 agent_cli.py status
python3 agent_cli.py action trajectory.start --idempotency-key trajectory-start-001
python3 agent_cli.py action trajectory.options --params '{"zupt":false}' --idempotency-key trajectory-zupt-001
python3 agent_cli.py action trajectory.pause --idempotency-key trajectory-pause-001
python3 agent_cli.py trajectory-download --format mat --output /private/trajectory.mat
python3 agent_cli.py action trajectory.reset --idempotency-key trajectory-reset-001
```

先固定模块，在live或播放中的recorded数据上建立静止参考；status.trajectory.reference有效后再start。reference/start的命令成功不代表已有有效轨迹点，检查active/calibrating/referenceProgress/message。没有外部位置真值不能声称绝对定位精度。零速更新假设实际静止，匀速平移可能被误判，options可关闭。

参考使用稳健窗口，少量噪声尖峰不清零进度。`status.trajectory.referenceOptions` 是当前容差，`referenceQuality` 提供样本数、内点噪声和拒绝原因。`trajectory.options` 可按 [轨迹说明](TRAJECTORY.md) 设置参考容差；数值须使用 API 的 rad/s、m/s²、秒和 0–1 比例，网页上的 °/s、百分比不能直接照抄。修改参考参数会清空参考和轨迹，先导出；它不改变 ZUPT 阈值，也不校准模块。

`GET /api/agent/v1/trajectory?after=INDEX&epoch=EPOCH`提供增量点与状态；epoch改变时清空客户端缓存。`GET /api/agent/v1/trajectory/csv|mat|xlsx`只读导出服务当前点集。POST离线导出仍接受source、generation、原始时基与estimate=true；不能用离线文件代替正在运行任务状态。

## 固件

固件包可先上传并读取元数据，不触发设备擦写：

```bash
python3 agent_cli.py firmware-upload --input /private/dm_imu_app_v2.0.4.0.bin
python3 agent_cli.py action firmware.inspect --params '{"id":"FIRMWARE_ID"}' --idempotency-key firmware-inspect-001
```

已获得具体升级授权后，`firmware.upgrade`参数为id、acknowledged=true、expected_version（当前设备版本）、expected_identity（ports中确认的USB身份）。设备与版本必须与确认时相同，候选包严格高于设备版本；仅开放已确认2.x。`firmware.cancel`接受原upgrade的operation_id。包格式、ACK/取消、重连版本核验和实机未刷写边界见 [固件说明](FIRMWARE.md)。上传/解析不等于开始擦写，分页进度不是最终成功。

## 有界协议调查

用户授权协议调查时，可执行固定旧版查询：

```bash
python3 agent_cli.py action protocol.probe --params '{"acknowledged":true}' --idempotency-key protocol-probe-001
python3 agent_cli.py probe-download PROBE_ID --output /tmp/imu-probe.json
```

此操作暂时进入设置模式，最多三次已公开状态查询再退出，最多2MiB原始接收数据。不穷举未知命令，不修改参数/保存，也不自动由旧应答识别新版能力。正常V2读取使用device.inspect。没有真实设备返回NOT_LIVE。

具体设备应答、传感器记录、Token、用户照片和本机固件都保留私有目录，不随源码推送。
