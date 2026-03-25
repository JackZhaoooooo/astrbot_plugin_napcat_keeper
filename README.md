# astrbot_plugin_napcat_keeper

NapCat 保活插件（AstrBot Star 插件）。

## 功能

- 周期检测 NapCat 服务与 QQ 登录状态。
- 检测到掉线后自动尝试重登。
- 支持账号密码登录（`qq_account` + `qq_password`）。
- 当触发验证/风控时自动切换二维码登录。
- 将二维码链接生成图片后通过多个 UMO 主动推送。
- 二维码按 2 分钟逻辑处理，并可按配置间隔自动刷新。

## 配置

见 `_conf_schema.json`。

关键项：

- `napcat_url`
- `napcat_token`
- `qq_account`
- `qq_password`
- `enable_auto_login`
- `check_interval_seconds`
- `qr_refresh_interval_seconds`
- `umo_ids_for_qr`

## 指令

- `/napcat_keeper_status` 查看状态
- `/napcat_keeper_refresh_qr` 管理员手动刷新二维码

## 开发说明

设计文档见 `TECHNICAL_DESIGN.md`。
