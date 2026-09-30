# HTTP API v1

网页使用 `/api/v1`，Agent 使用 `/api/agent/v1`。共享后端服务和操作队列，不能通过接口创建第二个串口实例。

| Method | 路径（相对于上述前缀） | 说明 |
| --- | --- | --- |
| GET | `/capabilities` | 数据类型、动作及设备控制兼容范围 |
| GET | `/status` | 来源、连接、通道值、更新时间、频率、统计、录制与回放状态 |
| GET | `/ports` | USB 串口清单和设备身份 |
| GET | `/samples?after=SEQ` | 有界历史中的增量帧；返回 `generation` 和 `truncated` |
| GET | `/recordings` | 录制清单 |
| GET | `/recordings/ID/raw`、`/recordings/ID/csv` | 下载已关闭的记录；CSV 需先导出 |
| GET | `/logs` | 最近服务日志 |
| POST | `/actions` | 提交动作，需要 `Idempotency-Key` |
| GET | `/operations/ID` | 查询原操作 |
| GET | `/protocol-probes/ID` | 下载协议探测原始证据 |

正常读响应为 `{"ok":true,"data":...}`；动作响应为 `{"ok":true,"operation":...}`，排队或运行时 HTTP 202。错误为 `{"ok":false,"error":{"code":"...","message":"..."}}`。

动作请求：

```json
{"action":"record.start","params":{}}
```

`operation` 包含 `id`、`key`、`action`、`params`、时间和状态，结束后有 `result` 或 `error`。HTTP 提交成功不代表设备操作成功，调用方必须检查终态。

通道值保留原始单位；没有数据时不出现该通道。`updated_at` 是主机接收时间，`age_ms` 和 `stale` 用于当前有效性检查。来源切换、重连和回放拖动会更新 `generation`，消费方应清空上一代缓存。`seq` 是单服务生命周期内递增帧序号。

网页额外提供 `/api/session`、`/api/login`、`/api/logout`，以及 `/api/v1/settings` 和 SSE `/api/v1/events`。浏览器写请求需要会话 CSRF Token；局域网浏览器需要密码登录。设置接口只允许本机浏览器，Agent 没有对应入口。

Agent 使用 `Authorization: Bearer TOKEN`；远程需 HTTPS。请求体最多 64 KiB，操作队列最多 32 个待处理项，累计持久操作最多 1 万条。幂等键长度 1–128，同键参数冲突返回 HTTP 409。
