# NapCat Keeper - AstrBot 保活插件

🛡️ 自动检测并恢复 NapCat QQ 机器人掉线状态

## 功能特点

- ✅ **自动检测** - 定时检查 NapCat 登录状态（使用 `get_login_info` API）
- ✅ **自动恢复** - 掉线时自动重启 NapCat
- ✅ **自动重登** - 支持配置 QQ 账号密码实现自动重新登录
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

## 兼容性

- AstrBot 版本: >= 4.16
- 支持平台: QQ (aiocqhttp / NapCat)

## License

AGPL-3.0
