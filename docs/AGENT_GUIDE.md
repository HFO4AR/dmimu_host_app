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
python3 agent_cli.py action playback.open --params '{"id":"RECORDING_ID"}' --idempotency-key replay-001
python3 agent_cli.py action playback.control --params '{"playing":true,"speed":2}' --idempotency-key play-001
python3 agent_cli.py action playback.control --params '{"position":1.2,"playing":false}' --idempotency-key seek-001
```

先停止录制再导出、下载或切换来源。下载拒绝覆盖现有文件。回放会释放真实串口并暂停自动连接；恢复采集需显式 `connect`。

原始单位：加速度 m/s²、角速度 rad/s、欧拉角 deg、四元数 w/x/y/z、温度 °C。API 时间是主机接收 Unix 秒，不是设备同步时间；不要把不同通道更新时间当成同一采样时刻。

## 设备控制与协议探测

先检查 `capabilities.data.device_control`。默认 `auto` 仅接收，2.x 写指令尚未支持。只有用户在本机页面确认 1.x 固件并选择 `legacy-v1` 后开放公开旧指令。

```bash
python3 agent_cli.py action device.read-settings --idempotency-key settings-read-001
python3 agent_cli.py action device.configure --params '{"interval_ms":10,"euler_enabled":true}' --idempotency-key configure-001
python3 agent_cli.py action device.calibrate --params '{"kind":"gyro","acknowledged":true}' --idempotency-key gyro-calibrate-001
python3 agent_cli.py action device.calibrate --params '{"kind":"six-face","acknowledged":true}' --idempotency-key six-face-001
python3 agent_cli.py action device.angle-zero --params '{"acknowledged":true}' --idempotency-key angle-zero-001
```

`device.configure` 接受四个通道开关、`interval_ms`、`heating_enabled` 和 `target_temperature`。先校验全部参数，再进入设置模式发送指令。回读成功只证明当前状态匹配；`persistent_storage_verified` 始终为 false，断电保存需另行实测。

旧校准协议缺少完成回报，状态会是 `uncertain`。观察实际设备指示灯和后续测量，不能把指令发送当作校准成功。`device.yaw-zero` 不会用旧版角度置零替代，返回不支持。

用户授权协议调查时，可执行有界探测：

```bash
python3 agent_cli.py action protocol.probe --params '{"acknowledged":true}' --idempotency-key protocol-probe-001
python3 agent_cli.py probe-download PROBE_ID --output /tmp/imu-probe.json
```

此操作会暂时进入设置模式，最多三次发送已公开状态查询，再退出；最多保存 2 MiB 原始接收数据。返回 `state_report_observed`、帧类型、是否截断等证据，不自动判定固件版本或开放参数写入。没有设备时返回 `NOT_LIVE`。不提供任意串口写入或命令 ID 穷举接口。
