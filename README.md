# NapCat Keeper - AstrBot 保活插件

🛡️ 自动检测并恢复 NapCat QQ 机器人掉线状态

## 功能特点

- ✅ **自动检测** - 定时检查 NapCat 登录状态
- ✅ **自动恢复** - 掉线时自动重启 NapCat
- ✅ **手动控制** - 提供指令手动查看状态/重启
- ✅ **配置灵活** - 支持自定义检查间隔和重试阈值
- ✅ **日志记录** - 完整的日志输出便于排查问题

## 指令

| 指令 | 说明 |
|------|------|
| `/napcat_status` | 查看当前 NapCat 状态 |
| `/napcat_restart` | 手动重启 NapCat |
| `/napcat_keeper_help` | 查看帮助信息 |

## 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `napcat_url` | `http://localhost:6099` | NapCat WebUI 地址 |
| `check_interval` | `60` | 检查间隔（秒） |
| `max_retries` | `3` | 连续失败次数阈值 |
| `napcat_dir` | `/root/AstrBot/napcat` | NapCat 安装目录 |
| `launcher_script` | `/root/AstrBot/napcat/launcher.sh` | 启动脚本路径 |
| `enable_auto_restart` | `true` | 是否启用自动重启 |
| `notify_on_restart` | `true` | 重启后是否通知 |

## 安装

### 方法一：通过 AstrBot WebUI 安装（推荐）

1. 打开 AstrBot WebUI → 插件管理
2. 点击安装插件，输入本仓库地址
3. 安装完成后在插件配置中设置参数

### 方法二：手动安装

```bash
# 克隆到插件目录
cd /root/AstrBot/data/plugins
git clone https://github.com/your-repo/astrbot_plugin_napcat_keeper.git

# 重启 AstrBot 或在 WebUI 中重载插件
```

## 原理

```
┌─────────────────┐     每 N 秒      ┌─────────────────┐
│  NapcatKeeper   │ ─────────────→ │    NapCat       │
│    插件         │ ←────────────── │   WebUI API     │
└─────────────────┘    状态检查     └─────────────────┘
        │
        │ 检测到掉线
        ↓
┌─────────────────┐
│  自动重启       │
│  NapCat         │
└─────────────────┘
```

1. 插件启动后创建异步监控任务
2. 每隔 `check_interval` 秒检查 NapCat API 状态
3. 连续失败达到 `max_retries` 次后触发自动重启
4. 重启流程：终止进程 → 等待 → 重新启动 → 验证

## 注意事项

- 确保 NapCat 和 AstrBot 运行在同一台服务器上
- 启动脚本需要有执行权限
- 建议将 `check_interval` 设置为 30-120 秒
- 如果 NapCat 使用 Docker 部署，请修改 `launcher_script` 为重启容器的命令

## 兼容性

- AstrBot 版本: >= 4.16
- 支持平台: QQ (aiocqhttp / NapCat)

## License

AGPL-3.0
