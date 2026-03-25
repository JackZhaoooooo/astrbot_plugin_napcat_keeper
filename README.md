# NapCat 登录状态监控插件

一个从零重写的最小版本插件。

功能只有一件事：

- 定时检查 NapCat 当前 QQ 是否仍然登录
- 一旦检测到从“已登录”变成“未登录”，向配置好的 UMO 列表发送通知

## 功能说明

- 只做登录状态监控，不做保活、重启、自动登录
- 默认优先请求 `GET /api/get_login_info`
- 如果该接口不可用，会回退到 `GET /get_login_info`
- 只有在状态从“已登录”切到“未登录”时才发送一次通知，避免轮询刷屏
- 支持命令 `/napcat_status` 手动查看当前状态

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `napcat_url` | `http://localhost:6099` | NapCat 根地址，支持填写到根路径、`/webui` 或反代前缀 |
| `napcat_token` | `""` | NapCat 接口 Token，可选 |
| `check_interval` | `30` | 轮询间隔，单位秒 |
| `request_timeout_seconds` | `10` | 单次请求超时秒数 |
| `notify_umos` | `[]` | 退出登录通知目标 UMO 列表 |
| `debug` | `false` | 开启后输出每次轮询日志 |

## 使用方式

1. 安装插件
2. 在插件配置中填写 `napcat_url`
3. 配置一个或多个 `notify_umos`
4. 保存配置后启用插件

当 NapCat 当前账号退出登录时，插件会主动向这些 UMO 发送通知消息。

## 命令

- `/napcat_status`：立即检查一次当前登录状态

## 注意事项

- `notify_umos` 需要填写 AstrBot 能主动发送的有效会话
- 某些平台的私聊主动发送能力受平台限制，如果发送失败，插件会把失败原因写到日志
- 如果你的 NapCat 走了反向代理前缀，比如 `https://example.com/napcat`，直接把这个地址填到 `napcat_url` 即可
