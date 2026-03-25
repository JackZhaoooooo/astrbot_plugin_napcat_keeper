# NapCat Keeper - AstrBot 保活插件

🛡️ 自动检测并恢复 NapCat QQ 机器人掉线状态

## 功能特点

- ✅ **自动检测** - 定时检查 NapCat 登录状态（使用 `get_login_info` API）
- ✅ **自动恢复** - 掉线时自动重启 NapCat
- ✅ **自动重登** - 支持配置 QQ 账号密码实现自动重新登录
- ✅ **登录辅助** - 遇到验证码/新设备验证时自动切换为二维码辅助登录，避免反复重启
- ✅ **登录态通知** - 支持通过 UMO 接收退出登录/重新登录通知
- ✅ **调试开关** - 可按需输出全部巡检日志，避免平台日志被正常轮询刷屏
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
| `manual_login_notify_umos` | `[]` | QQ 登录需要人工验证/扫码时主动通知的 UMO 列表；留空则默认复用登录态通知目标 |
| `auto_qr_login_fallback` | `true` | 当密码登录触发验证码、快速登录提示重新登录时，自动尝试获取 NapCat 二维码登录链接 |
| `manual_login_cooldown_seconds` | `900` | 登录需要人工处理时的冷却时长；冷却期内只轮询状态，不重复重启 NapCat |
| `debug` | `false` | 开启后输出全部巡检日志；关闭后隐藏正常在线的轮询日志，但保留恢复和重新登录过程日志 |

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
4. 如果 NapCat 服务仍在线，仅执行 QQ 重新登录流程，不再整套重启
5. 如果密码登录触发验证码/新设备验证，插件会尝试获取验证链接或二维码，并进入人工登录等待模式
6. 冷却期内插件只轮询登录状态，不重复重启 NapCat，等待你完成扫码或验证

## 注意事项

- 确保 NapCat 和 AstrBot 运行在同一台服务器上
- 启动脚本需要有执行权限
- 建议将 `check_interval` 设置为 30-120 秒
- 如果使用 Docker 部署 NapCat，需要修改 `launcher_script` 为重启容器的命令
- `qq_official` / `qq_official_webhook` 类型的 UMO 依赖最近一条有效 `msg_id`
- 如果你要把它们用于登录态通知，建议优先使用最近和机器人有过交互的会话
- 更稳定的做法是使用群聊 UMO，或其他支持主动发送的平台 UMO
- 遇到验证码、设备锁、新设备验证时，插件不会尝试绕过腾讯校验；它会改为输出验证链接或二维码，等待你手动完成

## 兼容性

- AstrBot 版本: >= 4.16
- 支持平台: QQ (aiocqhttp / NapCat)

## License

AGPL-3.0
