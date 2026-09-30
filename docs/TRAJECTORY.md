# 三维轨迹追踪

这是六轴 DM-IMU 的**短时惯性积分估计**，没有 GNSS、视觉或其他绝对位置观测。它不能替代位置传感器，也不能保证长时间、长距离的定位精度。轨迹和导出文件都明确标注估算属性、数据来源与时间基准。

## 使用

1. 打开“三维轨迹”，确保加速度、角速度和姿态通道持续输出。优先使用四元数；缺少四元数时支持欧拉角 ZYX。
2. 固定模块，点击“建立静止参考”，保持静止至少 2 秒。模块可倾斜摆放，但姿态变换后的重力参考应沿世界 +Z，模长接近 9.80665 m/s²。
3. 静止参考通过后，点击“开始追踪”。初始位移和速度均为零。先做短时、可用尺子或外部位置源核对的小范围实验。
4. “暂停”停止积分；“继续追踪”以当前位置开始新段，速度重新从零起算。不同段之间不画连接线。暂停期间的真实移动不会被补算。
5. “清空轨迹”清除轨迹和静止参考。断开、切换 live/demo/playback、重新连接或回放跳转导致数据 generation 变化时，也会清空并要求重新建立参考。
6. 数据缺口、姿态过期或服务加速度样本停止，都会暂停；需要手动恢复。恢复时速度从零开始。浏览器转入后台或关闭页面不会停止常驻Service估计任务。
7. 导出 CSV、Excel `.xlsx` 或 MATLAB `.mat`。导出的是估算轨迹，不是原始加速度波形或绝对位置真值；原始波形从“波形分析”另行导出。

“建立静止参考”在Service建立本次估计参考，不发送校准指令，也不修改设备。重新建立参考会清空旧轨迹，保证同一导出点集对应同一个参考；请先导出已有结果。显示归零只影响三维姿态画面，不影响轨迹数学。真实设备航向或安装方向变化后必须重新建立参考。

## 算法与坐标

- 输入加速度单位 m/s²，角速度 rad/s；四元数按 `[w,x,y,z]` 规范化。欧拉角输入度，使用 `Rz(yaw) Ry(pitch) Rx(roll)`。
- 世界加速度 `a_world = R(q) a_body`。世界 +Z 朝上，静止比力默认约定为 +Z。可视化通过 proper rotation `X→X、Y→−Z、Z→Y` 变换到 Three.js 的 Y-up；积分保持原始世界 XYZ。
- 静止参考取稳健窗口内点的 `a_world` 平均向量。默认角速度容差 0.15 rad/s、内点加速度 RMS 容差 0.35 m/s²、允许最多 10% 离群样本；平均重力模长与标准值相差不超过 0.6 m/s²，方向偏离世界 +Z 小于 15°。默认至少 2 秒、50 个有效时间样本；容差可调，详见下表。
- 这个单姿态参考不能区分重力、安装误差和体坐标加速度零偏，也不是三轴偏置校准。它只补偿本次窗口里的静止世界参考；改变温度、姿态、量程或设备校准后，参考可能不再适用。
- 去重力加速度 `a_linear = a_world - gravity_reference`；速度与位移使用梯形平均加速度积分。位置初值与速度初值由用户声明为零，没有从其他传感器估计真实初始速度。
- 静止零速更新默认打开：去重力加速度模长小于 0.12 m/s²、角速度小于 0.035 rad/s，持续至少 0.35 秒时把速度置零。**六轴 IMU 无法区分静止和无转动的匀速平移**，所以这个假设会误判匀速运动；需要追踪该类运动时关闭自动零速。
- 姿态必须来自同一从机 ID，接收时间不晚于加速度，差值最多 50 ms。超过 100 ms 的加速度积分间隔会暂停，不跨缺口推算速度和位移。
- 输出按约 100 Hz 保存，点数最多 12000，单次跨度最多 120 秒。达到上限会停止，保留当前数据供导出。积分本身处理每个有效不同时间的加速度组。

短时误差也可能很大：恒定 0.01 m/s² 残余误差积分 10 秒会产生约 0.5 m 位移误差；约 1° 的倾角误差可把约 0.17 m/s² 重力泄漏到水平轴，10 秒约 8.5 m。静止零速更新可以降低某些漂移，但无法恢复绝对位置。

## 时间基准

真实 USB 使用主机原始接收时间，**不是设备时钟**。一个 USB 数据块可能包含多个加速度和姿态帧，它们会共享接收时间。程序把同时间加速度帧平均，只积分一次，不把零间隔帧伪造为固定 1 kHz 时钟。

回放使用服务提供的 `measurement_time`，也就是录制时的原始主机接收时间；`time`/`host_time` 仍代表当前页面收到回放帧时的服务时间。因此播放速度不会改变物理积分时长或轨迹。数据缺少 `measurement_time` 时，轨迹会暂停并显示原因，不用回放发送时间代替采样时间。录制本身仍带主机时间抖动，程序没有宣称恢复设备级采样时间。

## 导出接口

Service是运行中唯一估计器，网页和Agent读取同一状态。`trajectory.reference/start/pause/reset`接受空参数。`trajectory.options` 接受 `zupt` 和下述参考容差的非空子集；修改参考容差会清空参考和轨迹并增加 epoch，修改 `zupt` 不清空点集。操作不写硬件，沿用已有队列/幂等恢复。关闭网页不停止已启动追踪，服务持续接收和估计。

参考建立改用滚动窗口，每 100 ms 评估一次，不因单个噪声尖峰清零。用世界加速度分量中位数估计中心，剔除距离过大或角速度超过容差的样本；内点 RMS、前后半窗均值变化和平均重力约定须满足条件。持续移动与姿态/时间缺口仍会拒绝参考。少量被剔除的尖峰不等于采集丢帧。

| 选项 | 默认 | 范围 / 单位 |
| --- | --- | --- |
| `referenceGyroMax` | 0.15 | 0.01–1 rad/s，超出值计入离群样本 |
| `referenceAccelerationStd` | 0.35 | 0.02–3 m/s²，内点向量 RMS 和前后均值变化上限 |
| `referenceOutlierFraction` | 0.10 | 0–0.20，最大离群比例 |
| `referenceSeconds` | 2 | 1–10 s，至少 50 个有效样本 |

网页提供普通、低噪声、振动较大预设和自定义字段；角速度字段显示 °/s，API 使用 rad/s。`referenceOptions` 回报当前生效设置，`referenceQuality` 回报样本数、内点 RMS、内点角速度 P95、离群比例、前后均值变化和具体拒绝原因。参考阈值与追踪时的零速更新阈值独立，放宽参考不放宽 ZUPT。窗口最多保留 30000 个参考样本；高频且选择长窗口时应降低输出频率或缩短窗口。

```sh
python agent_cli.py action trajectory.options --params '{"referenceGyroMax":0.15,"referenceAccelerationStd":0.35,"referenceOutlierFraction":0.1,"referenceSeconds":2}' --idempotency-key reference-tolerance-001
```

`GET /api/v1/trajectory?after=INDEX&epoch=EPOCH`返回status、epoch、points、offset、next、total、generation。epoch与当前不匹配时从0返回；reference/reset增加独立epoch，source/generation变化清空点集，消费者必须丢弃旧缓存。status.trajectory也包含当前统计。

`GET /api/v1/trajectory/csv|mat|xlsx`只读导出当前服务点集。Agent把前缀换为 `/api/agent/v1`。离线点集仍可POST `/api/v1/trajectories/export`，受既有browser CSRF / Agent Bearer保护，必须estimate=true。它不打开串口，也不修改设备。

Agent 可导出服务当前点集：

```sh
python agent_cli.py action trajectory.reference
python agent_cli.py action trajectory.start
python agent_cli.py action trajectory.pause
python agent_cli.py trajectory-download --format mat --output trajectory-estimate.mat
```

参考建立需要持续收到约 2 秒静止数据；开始前先确认状态里的 `trajectory.reference` 已建立。暂停回放时先恢复播放，再建立参考或开始追踪。

```json
{
  "format": "mat",
  "source": "live",
  "generation": 1,
  "gravity_reference": [0, 0, 9.80665],
  "timing": "host_receive_time",
  "estimate": true,
  "points": [{
    "time": 1000.01,
    "host_time": 1000.01,
    "elapsed": 0.01,
    "segment": 1,
    "position": [0, 0, 0],
    "velocity": [0, 0, 0],
    "acceleration": [0, 0, 0],
    "distance": 0,
    "stationary": true,
    "orientation_kind": "quaternion",
    "quaternion": [1, 0, 0, 0]
  }]
}
```

`source` 明确区分 `live`、`demo`、`playback`；`timing` 为 `host_receive_time` 或 `recorded_time`；`orientation_kind` 为 `quaternion` 或 `euler_zyx`。MAT 的数值枚举由导出文件内的字段说明定义。位置/距离 m，速度 m/s，去重力加速度/参考 m/s²，时间 s，四元数无量纲。

前端保留纯数学/隔离测试实现，作为合成验证，不与产品运行中的Service重复积分。网页导出和Agent直接下载使用服务当前估计点；离线导出接口供已明确标记来源的独立估算结果使用。

## 验证边界

`node tests/trajectory_math.mjs` 与 `python -m unittest discover -s tests -p test_trajectory.py` 验证纯合成数据：静止、倾斜静止重力补偿、已知恒加速度的速度与位移、旋转后同一世界加速度、同块重复时间去重、姿态缺失、采样缺口、数据源变化、Euler fallback、回放 4× 使用原始时间以及有界存储。隔离浏览器使用合成数据检查三维渲染、坐标映射、导出控件和响应式布局。

这些验证证明软件数学与界面行为，不能证明实机绝对定位精度。尚未用外部位置真值完成真实 DM-IMU 的运动距离验收；用户使用前应先静止建立参考，再做短程真值对照。官方演示的合成加速度、欧拉角和角速度并不一定符合物理一致性，它可能无法通过静止参考条件；可以用真实设备、物理一致的录制或隔离合成验证。
