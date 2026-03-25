# NapCat Keeper 重构技术方案

## 1. 目标

本插件用于在 AstrBot 运行期持续检测 NapCat QQ 登录状态，并在掉线后自动恢复登录：

1. 服务在线 + QQ 已登录：维持正常。
2. 服务在线 + QQ 未登录：触发自动重登流程。
3. 密码登录失败且要求验证：切换二维码登录，并将二维码图片推送到配置的多个 UMO。
4. 二维码有效期按 2 分钟处理，在可配置间隔到期后自动刷新并再次通知。

## 2. 架构

- **NapcatKeeperPlugin**：插件主类，继承 `Star`。
- **Monitor Loop**：后台异步轮询任务，负责检查状态和触发恢复流程。
- **NapCatClient(内聚在主类方法中)**：统一封装 HTTP 调用、鉴权、状态解析。
- **QrLoginState**：二维码登录等待态，记录二维码 URL、图片路径、刷新时间。

## 3. 配置项

- `napcat_url`: NapCat 根地址，默认 `http://127.0.0.1:6099`
- `napcat_token`: WebUI Token（用于 `/api/auth/login`）
- `qq_account`: 目标 QQ 号
- `qq_password`: 目标 QQ 密码（明文配置，提交时转 md5）
- `check_interval_seconds`: 轮询间隔
- `request_timeout_seconds`: HTTP 超时
- `enable_auto_login`: 是否自动登录
- `umo_ids_for_qr`: 二维码通知目标 UMO 列表（支持多值）
- `qr_refresh_interval_seconds`: 二维码刷新间隔（默认 120 秒）
- `cache_dir`: 缓存目录（二维码图片等）
- `debug`: 详细日志

## 4. 关键流程

### 4.1 状态检测

1. 检查 NapCat 服务是否可达。
2. 可达后优先用 WebUI 登录态接口检查：
   - `/api/auth/login`
   - `/api/QQLogin/CheckLoginStatus`
   - `/api/QQLogin/GetQQLoginInfo`
3. WebUI 检查失败时降级 `get_login_info`。

### 4.2 重登流程

- 未登录时触发 `_attempt_relogin`：
  1. 若配置账号+密码：调用 `/api/QQLogin/PasswordLogin`。
  2. 若返回需要验证（验证码/风控/扫码）：切换二维码流程。
  3. 若未配置密码：直接二维码流程。

### 4.3 二维码流程

1. 调用候选二维码接口（按顺序尝试）获取二维码链接。
2. 用 `qrcode` 库将链接生成本地 PNG。
3. 使用 `context.send_message(umo, MessageChain().message(...).file_image(...))` 推送至多个 UMO。
4. 进入等待态，达到刷新间隔后自动刷新并再次推送。

## 5. 错误处理策略

- 单次轮询失败不阻断主循环。
- HTTP/JSON 解析异常统一日志化，继续下一轮。
- 二维码发送对单个 UMO 失败不影响其他 UMO。
- 所有自动动作带可读日志，便于线上排查。

## 6. 指令

- `/napcat_keeper_status`：查看当前服务状态、登录态、二维码等待态。
- `/napcat_keeper_refresh_qr`：手动触发二维码刷新（管理员）。

## 7. 实现约束

- 使用 `aiohttp` 进行异步请求。
- 不依赖 AstrBot 私有 API，仅使用公开插件接口。
- 二维码图片落地到插件缓存目录，避免内存传输风险。
