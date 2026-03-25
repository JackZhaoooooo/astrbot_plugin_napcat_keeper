# NapCat 登录状态监控插件

插件会监控 NapCat 的 QQ 登录状态，并在未登录时推送通知和二维码登录入口。

## 功能说明

- 定时检查 NapCat 登录状态
- 检测到退出登录时，向配置 UMO 列表发送离线通知
- 支持自动获取 NapCat 二维码链接，生成二维码图片并推送到 UMO 列表
- 支持手动命令 `/qr` 强制刷新并推送最新二维码
- 推送二维码前会先调用 NapCat 刷新二维码会话接口，尽量避免发送过期二维码
- 优先走 WebUI 接口（`/api/auth/login` + `/api/QQLogin/CheckLoginStatus`），失败时回退 onebot `get_login_info`

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `napcat_url` | `http://localhost:6099` | NapCat 根地址，支持填写到根路径、`/webui` 或反代前缀 |
| `napcat_token` | `""` | NapCat WebUI Token，可选；留空时会尝试读取本地 `webui.json` |
| `webui_config_path` | `/root/AstrBot/napcat/config/webui.json` | 本地 WebUI 配置路径，仅在未填写 `napcat_token` 时使用 |
| `check_interval` | `30` | 登录状态轮询间隔（秒） |
| `request_timeout_seconds` | `10` | 单次请求超时秒数 |
| `notify_on_initial_logged_out` | `true` | 插件启动后首次检测到未登录时是否推送 |
| `notify_retry_cooldown_seconds` | `30` | 持续未登录时，离线通知重复提醒间隔（秒） |
| `notify_umos` | `[]` | 退出登录通知目标 UMO 列表 |
| `qr_notify_umos` | `[]` | 二维码通知目标 UMO 列表，留空则复用 `notify_umos` |
| `auto_send_qr_on_logged_out` | `true` | 检测到未登录时自动推送二维码 |
| `qr_notify_retry_cooldown_seconds` | `120` | 持续未登录时，二维码重复推送间隔（秒） |
| `qr_image_dir` | `/tmp/astrbot_plugin_napcat_keeper_qr` | 二维码图片缓存目录 |
| `debug` | `false` | 开启后输出每次轮询日志 |

## 使用方式

1. 安装插件并启用
2. 配置 `napcat_url`、`notify_umos`（或 `qr_notify_umos`）
3. 如跨机器部署，填写 `napcat_token`
4. 保存后观察日志

当 NapCat 未登录时，插件会自动推送二维码图片。扫码成功后，后续状态会恢复为“已登录”。

## 命令

- `/napcat_status`：立即检查一次当前状态
- `/qr`：强制刷新二维码，并发送最新二维码图片到二维码通知 UMO 列表

## 注意事项

- `notify_umos` / `qr_notify_umos` 需要填写 AstrBot 可主动发送的有效 UMO
- 某些平台私聊主动发送受限，发送失败时会写入日志
- 对 `qq_official` 平台，若会话没有可用入站 `msg_id`，AstrBot 无法主动发私聊
