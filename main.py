import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import qrcode
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star


@dataclass
class LoginSnapshot:
    service_online: bool
    login_state: str
    detail: str
    account: str | None = None
    nickname: str | None = None


@dataclass
class QrLoginState:
    url: str
    image_path: str
    expires_at: float
    next_refresh_at: float
    reason: str


class NapcatKeeperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.napcat_url = str(config.get("napcat_url", "http://127.0.0.1:6099")).rstrip("/")
        self.napcat_token = str(config.get("napcat_token", "")).strip()
        self.qq_account = str(config.get("qq_account", "")).strip()
        self.qq_password = str(config.get("qq_password", "")).strip()
        self.enable_auto_login = bool(config.get("enable_auto_login", True))
        self.check_interval_seconds = max(5, int(config.get("check_interval_seconds", 20)))
        self.request_timeout_seconds = max(3, int(config.get("request_timeout_seconds", 10)))
        self.qr_refresh_interval_seconds = max(
            30, int(config.get("qr_refresh_interval_seconds", 120))
        )
        self.debug = bool(config.get("debug", False))

        cache_dir = str(config.get("cache_dir", "")).strip()
        if cache_dir:
            self.cache_dir = cache_dir
        else:
            self.cache_dir = os.path.join(os.path.dirname(__file__), "cache")
        os.makedirs(self.cache_dir, exist_ok=True)

        self.umo_ids_for_qr = self._normalize_umo_list(config.get("umo_ids_for_qr", ""))

        self._monitor_task: asyncio.Task | None = None
        self._is_monitoring = False
        self._last_snapshot: LoginSnapshot | None = None
        self._webui_credential: str | None = None
        self._qr_state: QrLoginState | None = None

    async def initialize(self):
        await self._start_monitor_if_needed("initialize")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        await self._start_monitor_if_needed("on_astrbot_loaded")

    async def terminate(self):
        self._is_monitoring = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _start_monitor_if_needed(self, source: str):
        if self._monitor_task and not self._monitor_task.done():
            if self.debug:
                self._log(f"[{source}] 监控任务已存在，跳过重复启动", "DEBUG")
            return
        self._is_monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._log(f"[{source}] NapCat Keeper 监控任务已启动。")

    async def _monitor_loop(self):
        while self._is_monitoring:
            try:
                snapshot = await self._collect_snapshot()
                self._emit_snapshot_log(snapshot)

                if (
                    self.enable_auto_login
                    and snapshot.service_online
                    and snapshot.login_state != "logged_in"
                ):
                    await self._attempt_relogin(snapshot)
                elif self.enable_auto_login and self._qr_state:
                    await self._refresh_qr_if_needed()

                self._last_snapshot = snapshot
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"监控循环异常: {e}", "ERROR")

            await asyncio.sleep(self.check_interval_seconds)

    async def _collect_snapshot(self) -> LoginSnapshot:
        service_online = await self._check_service_online()
        if not service_online:
            return LoginSnapshot(
                service_online=False,
                login_state="service_offline",
                detail="NapCat 服务不可达。",
            )

        webui_snapshot = await self._check_login_via_webui()
        if webui_snapshot.login_state in {"logged_in", "not_logged_in"}:
            return webui_snapshot

        fallback_snapshot = await self._check_login_via_onebot()
        if fallback_snapshot.login_state in {"logged_in", "not_logged_in"}:
            return fallback_snapshot

        return LoginSnapshot(
            service_online=True,
            login_state="error",
            detail=(
                f"WebUI 检测失败: {webui_snapshot.detail}；"
                f"备用 get_login_info 失败: {fallback_snapshot.detail}"
            ),
        )

    async def _check_service_online(self) -> bool:
        endpoint = self.napcat_url
        try:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint) as resp:
                    return resp.status < 500
        except Exception:
            return False

    async def _check_login_via_webui(self) -> LoginSnapshot:
        if not self.napcat_token:
            return LoginSnapshot(True, "error", "未配置 napcat_token，无法执行 WebUI 检测。")

        credential, auth_err = await self._request_webui_credential()
        if not credential:
            return LoginSnapshot(True, "error", f"WebUI 鉴权失败: {auth_err}")

        status_endpoint = f"{self.napcat_url}/api/QQLogin/CheckLoginStatus"
        status_payload, status_err = await self._post_json(
            status_endpoint,
            {},
            headers=self._auth_headers(credential),
        )
        if status_payload is None:
            return LoginSnapshot(True, "error", f"CheckLoginStatus 请求失败: {status_err}")

        data = status_payload.get("data") if isinstance(status_payload, dict) else None
        if not isinstance(data, dict):
            return LoginSnapshot(True, "error", "CheckLoginStatus 返回结构异常。")

        is_login = bool(data.get("isLogin"))
        login_error = str(data.get("loginError") or "").strip()
        if not is_login:
            detail = "WebUI 检测到当前未登录 QQ。"
            if login_error:
                detail += f" 原因: {login_error}"
            return LoginSnapshot(True, "not_logged_in", detail)

        info_endpoint = f"{self.napcat_url}/api/QQLogin/GetQQLoginInfo"
        info_payload, info_err = await self._post_json(
            info_endpoint,
            {},
            headers=self._auth_headers(credential),
        )
        if info_payload is None:
            return LoginSnapshot(True, "logged_in", f"已登录，但获取账号信息失败: {info_err}")

        account, nickname = self._extract_identity(info_payload)
        return LoginSnapshot(
            service_online=True,
            login_state="logged_in",
            detail=f"WebUI 检测到 QQ 已登录: {self._format_identity(account, nickname)}",
            account=account,
            nickname=nickname,
        )

    async def _check_login_via_onebot(self) -> LoginSnapshot:
        endpoint = f"{self.napcat_url}/get_login_info"
        payload, err = await self._get_json(endpoint)
        if payload is None:
            return LoginSnapshot(True, "error", err or "请求失败")

        account, nickname = self._extract_identity(payload)
        if account:
            return LoginSnapshot(
                service_online=True,
                login_state="logged_in",
                detail=f"get_login_info 检测到 QQ 已登录: {self._format_identity(account, nickname)}",
                account=account,
                nickname=nickname,
            )

        return LoginSnapshot(True, "not_logged_in", "get_login_info 未返回有效账号信息。")

    async def _attempt_relogin(self, snapshot: LoginSnapshot):
        if self.qq_account and self.qq_password and self.napcat_token:
            ok, detail = await self._try_password_login()
            if ok:
                self._clear_qr_state()
                self._log(f"已提交 QQ 密码登录请求: {detail}")
                return

            verify_required = self._is_verify_required(detail)
            if verify_required:
                self._log(f"密码登录需要验证，切换二维码登录。原因: {detail}", "WARNING")
                await self._enter_qr_login_flow(detail)
                return

            self._log(f"密码登录失败，回退二维码登录。原因: {detail}", "WARNING")
            await self._enter_qr_login_flow(detail)
            return

        await self._enter_qr_login_flow(snapshot.detail)

    async def _try_password_login(self) -> tuple[bool, str]:
        credential, auth_err = await self._request_webui_credential()
        if not credential:
            return False, f"WebUI 鉴权失败: {auth_err}"

        endpoint = f"{self.napcat_url}/api/QQLogin/PasswordLogin"
        payload = {
            "uin": self.qq_account,
            "passwordMd5": hashlib.md5(self.qq_password.encode("utf-8")).hexdigest(),
        }
        resp, err = await self._post_json(
            endpoint,
            payload,
            headers=self._auth_headers(credential),
        )
        if resp is None:
            return False, f"PasswordLogin 请求失败: {err}"

        code = resp.get("code") if isinstance(resp, dict) else None
        message = str(resp.get("message") or "") if isinstance(resp, dict) else ""
        if code == 0:
            return True, "success"

        return False, message or "unknown"

    async def _enter_qr_login_flow(self, reason: str):
        credential, auth_err = await self._request_webui_credential()
        if not credential:
            self._log(f"无法获取二维码（鉴权失败）: {auth_err}", "ERROR")
            return

        qr_url, qr_reason = await self._fetch_qr_url(credential)
        if not qr_url:
            self._log(f"生成二维码失败: {qr_reason}", "ERROR")
            return

        image_path = self._write_qr_image(qr_url)
        now = time.monotonic()
        self._qr_state = QrLoginState(
            url=qr_url,
            image_path=image_path,
            expires_at=now + 120,
            next_refresh_at=now + self.qr_refresh_interval_seconds,
            reason=reason,
        )
        await self._notify_qr_to_umos(self._qr_state)

    async def _refresh_qr_if_needed(self):
        if not self._qr_state:
            return
        now = time.monotonic()
        if now < self._qr_state.next_refresh_at:
            return
        self._log("二维码等待超时，准备刷新并重新通知。", "WARNING")
        await self._enter_qr_login_flow("二维码过期，自动刷新")

    async def _notify_qr_to_umos(self, qr_state: QrLoginState):
        if not self.umo_ids_for_qr:
            self._log(
                "未配置 umo_ids_for_qr，无法主动推送二维码。请手动查看日志中的二维码链接。",
                "WARNING",
            )
            self._log(f"二维码链接: {qr_state.url}", "WARNING")
            return

        message = (
            "NapCat Keeper 检测到 QQ 需要验证，请在 2 分钟内扫码登录。\n"
            f"二维码链接: {qr_state.url}\n"
            f"触发原因: {qr_state.reason}"
        )

        success = 0
        for umo in self.umo_ids_for_qr:
            try:
                chain = MessageChain().message(message).file_image(qr_state.image_path)
                await self.context.send_message(umo, chain)
                success += 1
            except Exception as e:
                self._log(f"向 UMO 发送二维码失败: {umo} | {e}", "WARNING")

        if success > 0:
            self._log(f"二维码通知已发送，成功 {success}/{len(self.umo_ids_for_qr)}")
        else:
            self._log("二维码通知全部失败，请检查 UMO 与平台主动消息能力。", "ERROR")

    async def _request_webui_credential(self) -> tuple[str | None, str | None]:
        if self._webui_credential:
            return self._webui_credential, None
        if not self.napcat_token:
            return None, "未配置 napcat_token"

        endpoint = f"{self.napcat_url}/api/auth/login"
        payload = {"token": self.napcat_token}
        resp, err = await self._post_json(endpoint, payload)
        if resp is None:
            return None, err

        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            return None, "鉴权响应格式异常"

        credential = str(data.get("Credential") or data.get("credential") or "").strip()
        if not credential:
            message = str(resp.get("message") or "") if isinstance(resp, dict) else ""
            return None, message or "未返回 Credential"

        self._webui_credential = credential
        return credential, None

    async def _fetch_qr_url(self, credential: str) -> tuple[str | None, str]:
        candidates = [
            ("/api/QQLogin/GetQRcode", {}),
            ("/api/QQLogin/GetQrCode", {}),
            ("/api/QQLogin/FetchQrCode", {}),
            ("/api/QQLogin/GetLoginQrCode", {}),
            ("/api/QQLogin/GetQRcode", {"uin": self.qq_account}),
            ("/api/QQLogin/GetQrCode", {"uin": self.qq_account}),
        ]

        last_error = ""
        for path, payload in candidates:
            endpoint = f"{self.napcat_url}{path}"
            resp, err = await self._post_json(
                endpoint,
                payload,
                headers=self._auth_headers(credential),
            )
            if resp is None:
                last_error = f"{path}: {err}"
                continue

            url = self._deep_find_qr_url(resp)
            if url:
                return url, "ok"

            last_error = f"{path}: 未解析到二维码链接"

        return None, last_error or "未知错误"

    def _deep_find_qr_url(self, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in ["qrCodeUrl", "qrcodeUrl", "qr_url", "url", "qrCode"]:
                value = payload.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value
            for value in payload.values():
                found = self._deep_find_qr_url(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = self._deep_find_qr_url(item)
                if found:
                    return found
        return None

    def _write_qr_image(self, qr_url: str) -> str:
        filename = f"napcat_qr_{int(time.time())}.png"
        path = os.path.join(self.cache_dir, filename)
        img = qrcode.make(qr_url)
        img.save(path)
        return path

    def _extract_identity(self, payload: dict[str, Any]) -> tuple[str | None, str | None]:
        candidates = [payload]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)

        for item in candidates:
            uid = (
                item.get("uin")
                or item.get("uid")
                or item.get("user_id")
                or item.get("self_id")
            )
            name = item.get("nickname") or item.get("nick") or item.get("name")
            if uid:
                return str(uid), str(name) if name else None
        return None, None

    def _format_identity(self, account: str | None, nickname: str | None) -> str:
        if nickname and account:
            return f"{nickname} ({account})"
        if account:
            return account
        return "未知账号"

    def _is_verify_required(self, detail: str) -> bool:
        text = detail.lower()
        keywords = [
            "verify",
            "验证码",
            "扫码",
            "qr",
            "risk",
            "风控",
            "安全验证",
        ]
        return any(key in text for key in keywords)

    def _normalize_umo_list(self, raw_value: Any) -> list[str]:
        items: list[str] = []
        if isinstance(raw_value, str):
            for part in raw_value.replace("，", ",").replace("\n", ",").split(","):
                value = part.strip()
                if value:
                    items.append(value)
        elif isinstance(raw_value, list):
            for node in raw_value:
                if node is None:
                    continue
                value = str(node).strip()
                if value:
                    items.append(value)

        dedup: list[str] = []
        seen = set()
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            dedup.append(item)
        return dedup

    def _clear_qr_state(self):
        self._qr_state = None

    def _auth_headers(self, credential: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }

    async def _get_json(self, endpoint: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint) as resp:
                    text = await resp.text()
                    try:
                        payload = json.loads(text)
                    except Exception:
                        return None, f"HTTP {resp.status} 非 JSON"
                    return payload, None
        except Exception as e:
            return None, str(e)

    async def _post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    try:
                        body = json.loads(text)
                    except Exception:
                        return None, f"HTTP {resp.status} 非 JSON"
                    return body, None
        except Exception as e:
            return None, str(e)

    def _emit_snapshot_log(self, snapshot: LoginSnapshot):
        service_label = "🟢 在线" if snapshot.service_online else "🔴 离线"
        if snapshot.login_state == "logged_in":
            login_label = "🟢 已登录"
        elif snapshot.login_state == "not_logged_in":
            login_label = "🟠 未登录"
        elif snapshot.login_state == "service_offline":
            login_label = "⚪ 未检测"
        else:
            login_label = "🔴 检测失败"

        level = "INFO" if snapshot.login_state == "logged_in" else "WARNING"
        self._log(
            f"状态检查 | 服务: {service_label} | 登录: {login_label} | 详情: {snapshot.detail}",
            level,
        )

    def _log(self, message: str, level: str = "INFO"):
        text = f"[NapcatKeeper] {message}"
        if level == "ERROR":
            logger.error(text)
        elif level == "WARNING":
            logger.warning(text)
        elif level == "DEBUG":
            logger.debug(text)
        else:
            logger.info(text)

    @filter.command("napcat_keeper_status")
    async def cmd_status(self, event: AstrMessageEvent):
        snapshot = self._last_snapshot or await self._collect_snapshot()
        qr_status = "无"
        if self._qr_state:
            remain = max(0, int(self._qr_state.expires_at - time.monotonic()))
            qr_status = f"等待扫码，约 {remain}s 过期，链接: {self._qr_state.url}"

        msg = (
            "NapCat Keeper 状态\n"
            f"- 服务在线: {snapshot.service_online}\n"
            f"- 登录状态: {snapshot.login_state}\n"
            f"- 账号: {self._format_identity(snapshot.account, snapshot.nickname)}\n"
            f"- 详情: {snapshot.detail}\n"
            f"- 二维码状态: {qr_status}"
        )
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("napcat_keeper_refresh_qr")
    async def cmd_refresh_qr(self, event: AstrMessageEvent):
        if not self.enable_auto_login:
            yield event.plain_result("当前未启用自动登录。")
            return

        await self._enter_qr_login_flow("管理员手动刷新")
        if self._qr_state:
            yield event.plain_result(
                f"二维码已刷新并尝试推送。链接: {self._qr_state.url}"
            )
        else:
            yield event.plain_result("二维码刷新失败，请查看日志。")
