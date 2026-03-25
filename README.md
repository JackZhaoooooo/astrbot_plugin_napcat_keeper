# NapCat 登录状态监控插件

一个从零重写的最小版本插件。

功能只有一件事：

- 定时检查 NapCat 当前 QQ 是否仍然登录
- 一旦检测到从“已登录”变成“未登录”，向配置好的 UMO 列表发送通知

## 功能说明

- 只做登录状态监控，不做保活、重启、自动登录
- 默认优先走 NapCat WebUI 鉴权后调用 `POST /api/QQLogin/CheckLoginStatus`
- 如果 WebUI 鉴权或接口检测失败，会再回退到 onebot `get_login_info` 系列接口
- 对 `Unauthorized`、`token is empty` 这类鉴权失败响应不再误判成“未登录”
- 只有在状态从“已登录”切到“未登录”时才发送一次通知，避免轮询刷屏
- 支持“启动时已离线通知一次”和“离线期间按间隔持续提醒”
- 支持命令 `/napcat_status` 手动查看当前状态

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `napcat_url` | `http://localhost:6099` | NapCat 根地址，支持填写到根路径、`/webui` 或反代前缀 |
| `napcat_token` | `""` | NapCat WebUI Token，可选；留空时会尝试读取本地 `webui.json` |
| `webui_config_path` | `/root/AstrBot/napcat/config/webui.json` | 本地 WebUI 配置路径，仅在未填写 `napcat_token` 时使用 |
| `check_interval` | `30` | 轮询间隔，单位秒 |
| `request_timeout_seconds` | `10` | 单次请求超时秒数 |
| `notify_on_initial_logged_out` | `true` | 插件启动后首次检测到已离线时也发送一次通知 |
| `notify_retry_cooldown_seconds` | `30` | 持续离线时的重复提醒间隔（秒） |
| `notify_umos` | `[]` | 退出登录通知目标 UMO 列表 |
| `debug` | `false` | 开启后输出每次轮询日志 |

## 使用方式

1. 安装插件
2. 在插件配置中填写 `napcat_url`
3. 配置一个或多个 `notify_umos`
4. 如插件和 NapCat 不在同一台机器，补充填写 `napcat_token`
5. 保存配置后启用插件

当 NapCat 当前账号退出登录时，插件会主动向这些 UMO 发送通知消息。

## 命令

- `/napcat_status`：立即检查一次当前登录状态

## 注意事项

- `notify_umos` 需要填写 AstrBot 能主动发送的有效会话
- 某些平台的私聊主动发送能力受平台限制，如果发送失败，插件会把失败原因写到日志
- 对 `qq_official` 平台，若会话没有可用入站 `msg_id`，AstrBot 无法主动发私聊；插件会在日志中明确提示该原因
- 如果你的 NapCat 走了反向代理前缀，比如 `https://example.com/napcat`，直接把这个地址填到 `napcat_url` 即可
- 如果 NapCat 与插件在同一台机器，且 `webui.json` 在默认位置，插件会自动读取本地 token
