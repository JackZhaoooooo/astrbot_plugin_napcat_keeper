# NapCat Keeper - AstrBot 保活插件

🛡️ 自动检测并恢复 NapCat QQ 机器人掉线状态

## 功能特点

- ✅ **自动检测** - 定时检查 NapCat 登录状态（使用 `get_login_info` API）
- ✅ **自动恢复** - 掉线时自动重启 NapCat
- ✅ **自动重登** - 支持配置 QQ 账号密码实现自动重新登录
- ✅ **Webhook 通知** - 支持通过 Webhook 接收退出登录/重新登录通知
- ✅ **手动控制** - 提供指令手动查看状态/恢复
- ✅ **配置灵活** - 支持自定义检查间隔和重试阈值
- ✅ **日志记录** - 完整的日志输出便于排查问题

## 指令

| 指令 | 说明 |
|------|------|
| `/napcat_status` | 查看当前 NapCat 状态 |
| `/napcat_recover` | 手动恢复 NapCat |
| `/napcat_keeper_help` | 查看帮助信息 |

## 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `napcat_url` | `http://localhost:6099` | NapCat WebUI 地址 |
| `napcat_token` | `""` | WebUI Token（可选） |
| `check_interval` | `60` | 检查间隔（秒） |
| `max_retries` | `3` | 连续失败次数阈值 |
| `enable_auto_restart` | `true` | 是否启用自动恢复 |
| `enable_auto_login` | `true` | 是否启用自动登录 |
| `qq_account` | `""` | QQ 账号（用于自动登录） |
| `qq_password` | `""` | QQ 密码（用于自动登录） |
| `logout_notify_umos` | `[]` | QQ 退出登录时主动通知的 UMO 列表 |
| `relogin_notify_umos` | `[]` | QQ 重新登录时主动通知的 UMO 列表 |
| `logout_notify_webhooks` | `[]` | QQ 退出登录时 JSON POST 通知的 Webhook 列表 |
| `relogin_notify_webhooks` | `[]` | QQ 重新登录时 JSON POST 通知的 Webhook 列表 |
| `notification_webhook_timeout_seconds` | `5` | 通知 Webhook 请求超时秒数 |

## 安装

### 方法一：通过 AstrBot WebUI 安装（推荐）

1. 打开 AstrBot WebUI → 插件管理
2. 点击安装插件，输入本仓库地址
3. 安装完成后在插件配置中设置参数

### 方法二：手动安装

```bash
cd /root/AstrBot/data/plugins
git clone https://github.com/JackZhaoooooo/astrbot_plugin_napcat_keeper.git
```

## 工作原理

```
┌─────────────────┐     每 N 秒      ┌─────────────────┐
│  NapcatKeeper   │ ─────────────→ │   NapCat API     │
│    插件         │ ←────────────── │  get_login_info  │
└─────────────────┘    状态检查     └─────────────────┘
        │
        │ 检测到掉线
        ↓
┌─────────────────┐
│  1. 终止进程    │
│  2. 清理状态    │
│  3. 重启 NapCat │
│  4. 自动登录    │
└─────────────────┘
```

1. 插件启动后创建异步监控任务
2. 每隔 `check_interval` 秒调用 `get_login_info` API 检查状态
3. 连续失败达到 `max_retries` 次后触发自动恢复
4. 恢复流程：终止进程 → 清理状态 → 重启 → 等待 → 验证

## 注意事项

- 确保 NapCat 和 AstrBot 运行在同一台服务器上
- 启动脚本需要有执行权限
- 建议将 `check_interval` 设置为 30-120 秒
- 如果使用 Docker 部署 NapCat，需要修改 `launcher_script` 为重启容器的命令
- `qq_official` / `qq_official_webhook` 类型的 UMO 依赖最近一条有效 `msg_id`，不适合作为保活通知的唯一通道
- 如果你需要稳定接收登录态通知，推荐同时配置 `logout_notify_webhooks` / `relogin_notify_webhooks`

## Webhook 负载

插件会向配置的 Webhook 地址发送 `POST` JSON，请求体示例：

```json
{
  "plugin": "NapcatKeeper",
  "event": "logout",
  "time": "2026-03-24 22:30:00",
  "napcat_url": "http://localhost:6099",
  "account": {
    "user_id": "123456789",
    "nickname": "NapCatBot",
    "display": "NapCatBot (123456789)"
  },
  "status": {
    "previous_login_state": "logged_in",
    "current_login_state": "not_logged_in",
    "current_overall_status": "offline"
  },
  "message": "NapCat Keeper 检测到 QQ 已退出登录\n账号: NapCatBot (123456789)\n时间: 2026-03-24 22:30:00\n地址: http://localhost:6099\n说明: WebUI 检测到当前未登录 QQ。"
}
```

## 自带 Flask 接收示例

仓库内已附带一个最小可用的 Flask 接收器：

`examples/webhook_receiver_flask.py`

### 1. 安装 Flask

```bash
pip install flask
```

### 2. 启动接收器

在插件仓库目录下运行：

```bash
python examples/webhook_receiver_flask.py
```

默认监听地址：

```text
http://0.0.0.0:8787/napcat-webhook
```

默认日志文件：

```text
napcat_webhook_receiver.log
```

也可以通过环境变量修改：

```bash
NAPCAT_WEBHOOK_HOST=0.0.0.0 \
NAPCAT_WEBHOOK_PORT=8787 \
NAPCAT_WEBHOOK_LOG_FILE=/root/napcat_webhook_receiver.log \
python examples/webhook_receiver_flask.py
```

### 3. 在插件配置中填写 Webhook

如果 Flask 接收器和 AstrBot 在同一台机器上，可以这样填：

```json
{
  "logout_notify_webhooks": [
    "http://127.0.0.1:8787/napcat-webhook"
  ],
  "relogin_notify_webhooks": [
    "http://127.0.0.1:8787/napcat-webhook"
  ]
}
```

如果接收器部署在其他机器上，把 `127.0.0.1` 换成对应服务器 IP 或域名。

### 4. 手动测试

先确认接收器健康检查正常：

```bash
curl http://127.0.0.1:8787/healthz
```

再手动模拟一条退出登录通知：

```bash
curl -X POST http://127.0.0.1:8787/napcat-webhook \
  -H 'Content-Type: application/json' \
  -d '{
    "plugin": "NapcatKeeper",
    "event": "logout",
    "time": "2026-03-24 23:00:00",
    "account": {
      "user_id": "123456789",
      "nickname": "NapCatBot",
      "display": "NapCatBot (123456789)"
    },
    "status": {
      "previous_login_state": "logged_in",
      "current_login_state": "not_logged_in",
      "current_overall_status": "offline"
    },
    "message": "NapCat Keeper 检测到 QQ 已退出登录"
  }'
```

成功后你会看到：

- 终端打印一行接收日志
- `napcat_webhook_receiver.log` 追加一条 JSON 记录

## 兼容性

- AstrBot 版本: >= 4.16
- 支持平台: QQ (aiocqhttp / NapCat)

## License

AGPL-3.0
