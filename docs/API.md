# HTTP API v1

浏览器前缀 `/api/v1`，Agent 前缀 `/api/agent/v1`。共享单一 Host Service、串口接收线程和操作队列；不能通过接口创建第二个串口实例。Agent 使用 `Authorization: Bearer TOKEN`，远程必须 HTTPS。网页写请求使用会话 `X-CSRF-Token`，局域网网页先密码登录。

## 查询与制品

| Method | 路径（相对于上述前缀） | 说明 |
| --- | --- | --- |
| GET | `/capabilities` | 动作清单、数据类型、控制协议、设备能力与限制 |
| GET | `/status` | source/generation/connection、通道新鲜度、device版本/配置/校准、录制/回放/统计 |
| GET | `/ports` | 当前 USB 串口及设备身份 |
| GET | `/samples?after=SEQ` | 增量原始测量点、generation、seq、truncated |
| GET | `/recordings` | 私有录制清单 |
| GET | `/recordings/ID/raw`、`/recordings/ID/csv`、`/recordings/ID/imulog` | 已关闭记录，CSV先record.export；imulog为官方容器导出 |
| POST | `/recordings/import` | octet-stream导入官方imulog或工作台dmimulog，最多1GiB |
| GET | `/firmware` | 私有已上传固件清单 |
| POST | `/firmware/upload` | octet-stream解析/保存包；不触发擦写 |
| GET | `/logs` | 有界服务日志 |
| POST | `/actions` | 通过幂等队列提交动作，需要 `Idempotency-Key` |
| GET | `/operations/ID` | 原操作状态/结果/错误/分析进度 |
| GET | `/protocol-probes/ID` | 私有协议调查记录 |
| GET | `/analyses/ID`、`/analyses/ID/json` | Allan 结构化结果 |
| GET | `/analyses/ID/csv` | Allan 偏差数据 CSV |
| POST | `/waveforms/export` | 当前窗口原始点导出 MAT/XLSX/CSV，二进制附件 |
| GET | `/trajectory?after=INDEX&epoch=EPOCH` | Service唯一轨迹状态和增量点，epoch可选 |
| GET | `/trajectory/csv`、`/trajectory/mat`、`/trajectory/xlsx` | 当前Service估算轨迹只读导出 |
| POST | `/trajectories/export` | 离线估算点导出 MAT/XLSX/CSV，必须 estimate=true |
| POST | `/spectra/export` | 频谱数据导出 MAT/XLSX/CSV，单边幅值及分析设置，最多4MiB |

`/api/model` 与 `/api/model/mesh` 提供已验证本地 CAD 缓存状态和网格，缺少缓存不会下载未知替代模型。浏览器额外入口 `/api/session`、`/api/login`、`/api/logout`、`/api/v1/settings` 和 SSE `/api/v1/events`；服务器设置只允许本机浏览器，Agent 无对应入口。

普通 JSON 响应为 `{"ok":true,"data":...}`，动作响应为 `{"ok":true,"operation":...}`；排队/运行时 HTTP202。错误为 `{"ok":false,"error":{"code":"...","message":"..."}}`。导出成功直接返回文件字节和 Content-Disposition，不封装 JSON。

## 队列与结果

```json
{"action":"device.inspect","params":{}}
```

`operation` 包含 id/key/action/params、时间、state、result/error。`queued`/`running` 是处理中，`succeeded`/`failed`/`uncertain` 是终态。HTTP 提交成功不表示设备已经执行。

| 动作 | 参数 |
| --- | --- |
| connect | `{"port":"/dev/ttyACM0"}` 或实际 COM 号 |
| disconnect / demo / record.start / record.stop | `{}` |
| record.export / playback.open | `{"id":"RECORDING_ID"}` |
| playback.control | playing、speed、position 的受校验 patch |
| device.inspect | `{}`，只读版本/配置/校准状态，确认控制能力 |
| device.read-settings / device.calibration-status | `{}` |
| device.configure | 完整校验的配置 patch，见 Agent 指南 |
| device.calibrate | `{"kind":"gyro"或"six-face","acknowledged":true}` |
| device.calibration-abort / device.yaw-zero / device.factory-reset | `{"acknowledged":true}`，仅已识别 V2 |
| device.angle-zero | `{"acknowledged":true}`，旧版协议；V2 使用 yaw-zero |
| protocol.probe | `{"acknowledged":true}`，固定旧查询，不枚举未知命令 |
| allan.analyze | recording_id、channel（acceleration/angular_velocity）、sample_rate |
| allan.cancel | `{"operation_id":"ANALYSIS_OPERATION_ID"}` |
| trajectory.reference / trajectory.start / trajectory.pause / trajectory.reset | `{}`，上位机估计，不发送设备指令 |
| trajectory.options | `zupt` 与 `referenceGyroMax`、`referenceAccelerationStd`、`referenceOutlierFraction`、`referenceSeconds` 的非空子集；范围见 [轨迹说明](TRAJECTORY.md)，参考项变更清空参考/轨迹并递增 epoch |
| firmware.inspect | `{"id":"FIRMWARE_ID"}` |
| firmware.upgrade | id、acknowledged=true、expected_version、expected_identity |
| firmware.cancel | `{"operation_id":"UPGRADE_OPERATION_ID"}` |

运行中 `/capabilities` 返回当前支持动作与约束，不为未知协议开放任意串口写入。

同键相同参数返回原操作，同键不同参数 HTTP409。幂等键1–128字符，待处理队列最多32项，累计持久操作最多1万条。服务重启将未结束操作恢复为 uncertain，不重放。设备写/校准结果未知时保存原操作 ID，查询原操作，不换键重发。

校准的 `succeeded` 可只代表启动 ACK，此时 `completed=false`；需要随后查询 `device.static_calibration` / `device.six_face_calibration` 状态确认完成。配置回读匹配证明当前参数，`persistent_storage_verified=false`；保存 ACK 不证明断电持久化。

## 数据时基与代次

每个 sample 为 `seq/time/measurement_time/channel/values/slave_id`。单位保留设备原始值：加速度 m/s²、角速度 rad/s、Euler deg、四元数 wxyz。没有报告的通道不会补默认数据。

`time` 是服务本次接收时间。`measurement_time` 在 live 中相同，在回放中是录制时原始接收时间；分析消费者在回放中使用后者，倍速不改变物理时间。两者都不是设备同步时钟，同一 USB 数据块内多帧可以共用时间。

status通道 `updated_at/age_ms/stale` 用于当前显示新鲜度。重连/切换 source/回放跳转更新 generation，消费者必须清空上一代缓存。seq 在单服务生命周期递增。truncated 表示增量消费者读取落后，不能假定获取完整数据。

## 导出 payload

波形导出接受原始通道数组，不插值、不使用图表抽点：

```json
{"format":"mat","source":"demo","channels":{"acceleration":[[1000.0,0,0,9.80665],[1000.01,1,0,9.80665]]}}
```

每行第一列为原始接收时间，其后是通道值；各通道独立时间。最多40万个原始点，请求体上限48 MiB。MAT为Level5文件，每通道独立double矩阵与单位/列名/来源元数据；XLSX每通道工作表与metadata；CSV带通道、单位和来源。见 [波形格式](WAVEFORMS.md)。

轨迹导出最多12000点，请求体上限8 MiB，包含 source/generation/gravity_reference/timing/estimate 与位移、速度、去重力加速度、分段、姿态。`estimate=true` 必填，不能当绝对位置。见 [轨迹 payload](TRAJECTORY.md)。

其他普通 JSON 请求默认上限64 KiB。上传二进制制品接口与分析都有独立的校验、大小和取消规则；不能把制品路径当成任意本机读写权限。

频谱导出字段为 format、channel、source、sample_rate_hz、window（hann/hamming/rectangular）、samples（2的幂）、start_time_unix_s、end_time_unix_s、frequency_hz（0..Fs/2原始频点）、amplitude（按通道各轴排列的数组）。输出保留标称频率、窗函数、幅值定义与单位；不将FFT幅值当PSD。

官方录制容器以IMULOG01开头，格式及证据见 [录制格式](RECORDINGS.md)。导入成功不代表当前USB设备已连接；回放来源始终playback。文件上限与回放100MiB/50万帧限制分别检查。固件上传上限4096×255+18字节；文件名只是标签，不能指定任意磁盘路径，包必须通过尾部元数据解析与哈希校验。

Service持续轨迹与网页共用同一个估计器；关闭网页不会终止已启动任务。`/status`包含trajectory统计；`/trajectory`返回 `{status,epoch,points,offset,next,total,generation}`，points从after索引开始。提供epoch且与当前不匹配时从0返回；reference/reset增加独立epoch，数据源generation变化也清空参考与点集，消费者必须检查两者并丢弃旧缓存。暂停保持位置但速度归零，恢复建立新段；backendmonotonic watchdog在1.2秒无有效加速度时停止，不跨缺口积分。
