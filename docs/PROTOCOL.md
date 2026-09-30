# DM-IMU USB 协议与证据

## 官方来源

- [官方总资料仓库](https://gitee.com/kit-miao/damiao)的 `模块/传感器模块/DM-IMU` 子模块指向 [kit-miao/dm-imu](https://gitee.com/kit-miao/dm-imu)。
- 已核对 IMU 仓库完整目录与最新提交 `ba758fd6106c2b210713bf1ef05800f1688f73c5`，2026-09-24 更新。
- `说明书/DM-IMU-L1 六轴惯性测量单元使用说明书 V1.3 2026-09-22.pdf` 针对固件 2.0.3.0，附录 E 定义 USB 数据帧，但明确不覆盖控制协议，并指出旧版控制响应失效。
- 官方 ROS1 C++ 例程包含旧版控制指令；ROS2 Python 例程只解码三种 19 字节测量帧。新工作台独立实现解析，补上四元数、温度、状态及逐通道时间。
- 历史 V1.2 手册位于上一个提交 `39a0c4e3ebeae719310e1e3d717ab9bddeb69e3b`，第 6 页列出 USB 快捷指令。
- 最新目录只发布 `上位机/dm-imu-upper.exe` 和 `固件/dm_imu_app_v2.0.3.0.bin`，未找到新版控制源码。上位机为 Windows 原生 .NET AOT 包。本项目已经核对 NativeAOT 编码器、解析器和校准/升级调用路径，独立还原新版控制协议，详见 [V2协议](V2_PROTOCOL.md)。
- 固件包大小 106514 字节，起始数据不呈现 ARM 向量表，字节熵约 7.9984 bit/byte。正文按官方升级流程原样传输；尾部18字节为版本/发行元数据，已经从官方程序恢复布局。不将其直接作为裸 Cortex-M 程序分析，详见 [升级协议](FIRMWARE.md)。

## 测量帧

```
55 AA | slave_id:u8 | type:u8 | payload | CRC16:u16 little-endian | 0A
```

CRC-16/CCITT-FALSE：poly `0x1021`，init `0xFFFF`，RefIn/RefOut false，xorout 0，覆盖帧头至数据区结束。兼容选项允许旧版不含帧头的 CRC，默认关闭。CRC 校验成功后才解析值；非有限数值和无效四元数不进入遥测。

| type | 长度 | payload |
| --- | --- | --- |
| 1 | 19 | 三个 little-endian float32，加速度 m/s² |
| 2 | 19 | 三个 float32，角速度 rad/s |
| 3 | 19 | roll/pitch/yaw 三个 float32，deg，ZYX |
| 4 | 23 | w/x/y/z 四个 float32 |
| 5 | 19 或 23 | 目标/当前温度 float32、间隔 uint16，其余保留 |
| 7 | 19 | slave/master uint16，八字节设备状态 |

V1.3 对温度帧的表格写 19 字节，布局却写 16 字节数据区（总长 23）。解码器严格校验两种长度并暴露实际 `frame_length`；没有实机证据前不选择猜测长度。

三维积分采用世界 Z-up，显示以一次 proper rotation 转为 Three.js Y-up；优先使用设备四元数，否则从欧拉角 ZYX 生成四元数。姿态缺失时不通过陀螺积分伪造。

## 已公开旧指令

| 功能 | Hex |
| --- | --- |
| 设置模式 / 正常模式 | `AA 06 01 0D` / `AA 06 00 0D` |
| 加速度开 / 关 | `AA 01 14 0D` / `AA 01 04 0D` |
| 角速度开 / 关 | `AA 01 15 0D` / `AA 01 05 0D` |
| 欧拉角开 / 关 | `AA 01 16 0D` / `AA 01 06 0D` |
| 四元数开 / 关 | `AA 01 17 0D` / `AA 01 07 0D` |
| 上报周期 | `AA 02 interval:u16-LE 0D`，官方 ROS1 示例设置 1 ms |
| 保存参数 | `AA 03 01 0D` |
| 陀螺静态 / 六面校准 | `AA 03 02 0D` / `AA 03 03 0D` |
| 温控关 / 开 | `AA 04 00 0D` / `AA 04 01 0D` |
| 温控目标 | `AA 05 temperature:u8 0D` |
| 角度置零 | `AA 0C 01 0D`；不等同于新版独立航向归零 |

除了探测的固定查询，控制仅在用户确认 `legacy-v1` 后开放。当前值回读匹配不证明 Flash 保存成功，校准指令发送不证明校准完成。

## 新协议与验证范围

新版请求为 `A5 command length:u16LE payload CRC:u16LE 5A`；应答增加 command 后的 ACK_CODE 字节。CRC 为 CCITT-FALSE，排除 A5 和 5A，只覆盖 command 至 payload。完整命令、字段及 Native 地址见 [V2协议](V2_PROTOCOL.md)。55AA测量与A5应答在同一接收线程解析，不创建第二个串口实例。

默认连接不发送指令。显式 `device.inspect` 只读版本、配置与校准状态，确认 APP>=2 后才开放新版写能力。真实串口已完成这些只读结构核对；具体设备原始应答和测量不随源码发布。设备写参数、物理校准/恢复和升级擦写本轮未执行，软件测试不证明实际物理效果。

`protocol.probe` 保留固定旧版查询作为诊断工具，最多3次/2MiB原始记录，不扫描命令ID，也不自动由旧响应推断新版能力。新版日常读取使用 `device.inspect`，不是旧协议探测。

## 时间与数据来源

测量只记录主机 USB 接收时间，没有设备同步时钟。一个数据块内多个帧可以共享时间，通道不同更新时间也不证明硬件同时采样。`samples` 的 `time` 是本次服务接收时间，`measurement_time` 在 live 中与其相同，在 playback 中保留录制时的原始接收时间；倍速播放不改变原始分析时基。

来源和 `generation` 必须随缓存消费；切换来源/重连/回放跳转后清空旧缓存。FFT与Allan采用已知标称输出频率，轨迹避免跨缺口积分并明确漂移。演示和回放不能证明当前实机状态。
