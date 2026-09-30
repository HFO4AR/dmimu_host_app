# DM IMU Workbench · 达妙惯性测量工作台

独立的 **DM-IMU-L1 USB 上位机**，提供浏览器工作台和 Agent 接口。Python 启动方式、侧栏和卡片布局参考 IterxAI Host App，默认端口 **5050**。支持新版模块配置/校准协议、波形与频谱、Allan 偏差、录制回放及三维轨迹估计。

## 启动

需要 Python **3.11+**。浏览器三维和图表资源随仓库提供，无需 Node.js、ROS、CDN 或官方 Windows 上位机；Node 只用于开发检查。

```bash
git clone git@github.com:HFO4AR/dmimu_host_app.git
cd dmimu_host_app
./start.sh
```

Windows 安装 Python Launcher 后双击 `start.bat`。打开 **http://127.0.0.1:5050**，用 Type-C 数据线连接模块。后端独占串口，浏览器不需要 Web Serial 权限。

首次启动自动创建 `.venv`、安装依赖、下载并转换官方 STEP 模型。后续启动验证模型缓存，不重复下载或创建转换环境。CAD 准备失败会显示原因，采集和分析服务仍启动；三维窗口提示模型尚未准备。首次 CAD 转换的 OpenCascade/VTK 依赖较大，且需要当前平台有对应 Python wheel。

跳过模型准备或指定缓存：

```bash
DMIMU_SKIP_MODEL=1 ./start.sh
DMIMU_MODEL_CACHE=/path/to/model-cache ./start.sh
.venv/bin/python scripts/prepare_model.py --install  # 稍后单独准备
```

PowerShell 中使用 `$env:DMIMU_SKIP_MODEL='1'` 或 `$env:DMIMU_MODEL_CACHE='C:\path\models'` 后运行 `./start.bat`。官方 CAD、Windows EXE 和固件不随源码再分发。模型来源及坐标依据见 [模型说明](docs/MODEL.md)。

手动启动：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/prepare_model.py --install  # 可选
.venv/bin/python app.py --port 5050
```

Windows 对应使用 `.venv\Scripts\python.exe`。

### Type-C 自动接入

服务每两秒扫描已识别的 DM-IMU USB 设备，唯一候选时自动连接，多个候选时手动选择；成功连接后按 USB 身份优先重连。手动断开、演示或回放会暂停自动连接。使用“连接”或上位机设置恢复。

已识别描述为 `DM-IMU-L1`、VID/PID `6877:4D55` 的模块。通用 CDC/STM32 描述需要首次手动选串口；程序不会自动打开所有通用串口。默认波特率 921600。

连接后先被动接收，不自动配置、归零、校准或升级。端口打开与有效数据接收分别显示，通道超过两秒未更新标为过期。设备控制先点击“读取版本与配置”，确认协议与能力；未知版本不套用旧指令。

## 功能

| 工作区 | 已实现的功能 |
| --- | --- |
| 实时监测 | 官方 STEP 壳体/接口/安装孔网格，实物标记对应 XYZ、四元数优先、Euler ZYX、显示归零、三轴/温度/接收频率 |
| 波形分析 | 四类通道同步时间轴，通道开关，框选缩放、平移、暂停、数值范围、双游标与差值、Y 轴锁定、极值保留、PNG |
| 数据导出 | 当前波形窗口原始点导出 CSV、Excel `.xlsx`、MATLAB Level 5 `.mat`；保留来源、时基、通道与原始单位 |
| 实时 FFT | Web Worker，256–16384 点，Hann/Hamming/矩形窗、去均值、窗增益校正、单边幅值、主峰与频谱 CSV/XLSX/MAT |
| Allan 偏差 | 已完成录制的重叠 Allan 偏差、进度/取消、双对数图、噪声系数与拟合区间、JSON/CSV/PNG |
| 三维轨迹 | Service 常驻估计与 Agent 控制、稳健静止参考、噪声诊断与可调容差、世界重力补偿、可选静止零速、分段/缺口停止、有界点集、CSV/XLSX/MAT |
| 录制与回放 | 原始 USB 数据及接收时间，回放定位/倍速，CSV、原始 `.dmimulog` 下载，官方 `.imulog` 双向容器转换；分析使用录制时基 |
| 设备参数与校准 | V2 版本、配置与校准状态读取，输出/周期/温控/CAN/RS485 参数、安装方向/量程、静止与六面校准/取消、航向归零和出厂恢复 |
| 固件升级 | 官方包元数据/版本检查、分页和 ACK 状态机、升级进度、取消、重连后版本核验；详细边界见升级说明 |
| 诊断与设置 | 协议校验、操作结果与日志、中/英文、浅/深色、蓝青绿橙紫红六色、10/20/30/60Hz波形重绘、自动连接、局域网密码和 Agent 接入 |

“体验演示”需要显式点击；live / demo / playback 始终分别标记，不会在无设备时伪造实测。“显示归零”仅改变画面，设备航向归零是独立操作。图表的 °/s 显示不会改变原始 rad/s 记录与导出。

FFT 与 Allan 使用用户填写或设备回读的标称输出频率，USB 接收频率不是设备采样时钟。FFT 是单边幅值频谱，不是功率谱密度。Allan 系数是静止噪声模型估计，不满足拟合条件会显示“未识别”。详见 [波形导出](docs/WAVEFORMS.md) 和 [Allan 分析](docs/ALLAN.md)。

**轨迹是六轴惯性积分估算，不能提供绝对位置或长期高精度定位。** 先静止建立参考，初始速度假设为零；误差会经两次积分累积。静止零速可能把匀速平移误判为静止，可手动关闭。最长 120 秒/12000 点；缺口停止，来源变化或重新建立参考清空旧轨迹。Service 是唯一持续估计来源，网页和 Agent 共用状态；关闭网页不停止已启动的追踪。Agent 通过 trajectory.reference/start/pause/reset/options 控制，并可直接下载当前轨迹。见 [轨迹说明](docs/TRAJECTORY.md)。

## 协议与实机验证边界

来源为 [达妙官方仓库](https://gitee.com/kit-miao/dm-imu)，固定提交 `ba758fd6106c2b210713bf1ef05800f1688f73c5`。新版控制协议从官方 2026-09-24 NativeAOT 上位机的封包、解析器与状态机独立还原；不是仅凭字符串猜命令。见 [协议说明](docs/PROTOCOL.md)、[V2 格式](docs/V2_PROTOCOL.md) 和 [固件协议](docs/FIRMWARE.md)。

V2 配置写入需要版本识别、ACK 与配置回读；保存 ACK 不证明断电持久保存。校准启动 ACK 只证明已接受开始，完成/失败以设备后续状态为准。升级进度 100% 只证明分页阶段，必须重连读回目标版本才报告成功。官方包格式校验与 SHA-256 标识不等于厂商数字签名认证。

新版版本、22字节参数和校准状态已通过真实串口只读核对，具体原始应答与实测保留本机。公开软件测试覆盖协议封包/解析、fake serial 事务、分析数学和浏览器行为。**本轮未对真实设备执行物理校准、出厂恢复或固件擦写，也未用外部位置真值验收轨迹精度。** Windows 脚本和 ACL 逻辑须结合实际 Windows/COM 设备验收；跨平台 CI 不能替代实机证据。详细记录由 [验证说明](docs/VALIDATION.md) 区分软件、浏览器与设备证据。

旧版 1.x 公开指令保留在 `legacy-v1` 模式；只有确认设备版本后才手动启用。旧版缺少可验证的校准完成回报时返回 `uncertain`，不冒充完成。

## Agent

服务启动后，同用户 Agent 可使用标准库 CLI 自动发现私有凭据，不直接占用串口：

```bash
python3 -S agent_cli.py capabilities
python3 -S agent_cli.py status
python3 -S agent_cli.py ports
python3 -S agent_cli.py action device.inspect --idempotency-key inspect-001
python3 -S agent_cli.py action record.start --idempotency-key record-001
python3 -S agent_cli.py action record.stop --idempotency-key stop-001
python3 -S agent_cli.py recordings
```

Windows 使用 `py -3`，复杂 JSON 使用 `--params-file`。设备写操作与校准需按已有用户授权显式调用，幂等键用于恢复未知结果；`uncertain`/超时后查询原操作，不换键重复执行。

所有 HTTP 功能共用 `/api/v1` 浏览器前缀和 `/api/agent/v1` Agent 前缀。波形/轨迹导出返回二进制附件；CLI `action` 控制队列操作，具体路径与 payload 见 [API](docs/API.md) 和 [Agent 指南](docs/AGENT_GUIDE.md)。

## 本机数据与网络

默认数据目录：Linux `${XDG_STATE_HOME:-~/.local/state}/dmimu-workbench`；Windows `%LOCALAPPDATA%\DM-IMU-Workbench`。保存设置、Token、操作、录制、分析、升级和探测资料。Linux 私有权限、Windows 当前用户 ACL；不进 Git。自定义 `--data-dir` 必须使用支持私有权限的本机目录，CLI 也用同目录；一个目录只运行一个实例。

默认监听本机 5050，本机浏览器免登录。在上位机设置创建密码并启用局域网，重启后监听 `0.0.0.0:5050`，其他浏览器需要登录。服务器接入设置只允许本机网页修改。

远程 Agent 需要 HTTPS 反向代理和显式私有 Token 文件。CLI 不接受远程明文 HTTP，不跳过证书校验。可信代理通过 `--trusted-proxy` 指定，未知代理不应加入。官方下载、照片、私有凭据、实测原始数据与本机固件均不发布。

```bash
python app.py --port 5050
python app.py --demo
python app.py --no-auto-connect
python app.py --dev  # 不自动重载，不创建第二个串口实例
```

## 排查与开发

没有 USB 串口时先确认数据线，检查 Linux `/dev/serial/by-id/` 或 Windows 设备管理器；Linux 权限错误按实际设备所属组配置，避免 root 运行整个工作台。设备被其他上位机占用时先关闭对应程序。5050 被占用会报错，不会停止别人的服务。

官方 `.imulog` 和本项目 `.dmimulog` 支持导入（最多1GiB）；容器读写已对照官方程序并做合成roundtrip，尚未实际在Windows官方EXE中打开导出文件。见 [录制格式](docs/RECORDINGS.md)。

回放使用流式解码与稀疏检查点，导入最多 1 GiB；不把整份记录或全部测量帧加载到内存。定位后沿用录制时基。拔出会结束录制，重新连接后需重新开始。`Agent Token` 找不到时核对同用户、服务是否运行和数据目录。

```bash
.venv/bin/python -m unittest discover -s tests -v
python3 -S agent_cli.py --help
node tests/pose_math.mjs
node tests/fft_math.mjs
node tests/trajectory_math.mjs
```

GitHub Actions 使用 Ubuntu/Windows × Python 3.11/3.14，安装 NumPy 与 Excel 导出依赖并运行后端测试、前端语法和数学验证。软件源码与第三方前端许可证可公开；官方 CAD/EXE/固件缓存、用户照片与真实测量不包含在发布内容中。
