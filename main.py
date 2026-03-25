"""
NapCat 登录状态监控插件
仅监控登录状态，并在退出登录时向配置的 UMO 列表发送通知。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

PLUGIN_TAG = "[NapcatLoginMonitor]"
DEFAULT_NAPCAT_URL = "http://localhost:6099"
DEFAULT_CHECK_INTERVAL = 30
DEFAULT_REQUEST_TIMEOUT = 10
DEFAULT_WEBUI_CONFIG_PATH = "/root/AstrBot/napcat/config/webui.json"
MIN_CHECK_INTERVAL = 5
MIN_REQUEST_TIMEOUT = 3

STATE_TEXT = {
    "logged_in": "🟢 已登录",
    "logged_out": "🟠 未登录",
    "error": "🔴 检测失败",
}


@dataclass(frozen=True)
class LoginState:
    state: str
    endpoint: str
    detail: str
    user_id: str | None = None
    nickname: str | None = None


class NapcatKeeperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self.napcat_url = self._normalize_root_url(
            str(config.get("napcat_url", DEFAULT_NAPCAT_URL))
        )
        self.napcat_token = str(config.get("napcat_token", "")).strip()
        self.webui_config_path = str(
            config.get("webui_config_path", DEFAULT_WEBUI_CONFIG_PATH)
        ).strip()
        self.check_interval = self._parse_int(
            config.get("check_interval", DEFAULT_CHECK_INTERVAL),
            default=DEFAULT_CHECK_INTERVAL,
            minimum=MIN_CHECK_INTERVAL,
        )
        self.request_timeout_seconds = self._parse_int(
            config.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT),
            default=DEFAULT_REQUEST_TIMEOUT,
            minimum=MIN_REQUEST_TIMEOUT,
        )
        self.notify_umos = self._normalize_umo_list(config.get("notify_umos", []))
        self.debug = self._parse_bool(config.get("debug", False))
        self._resolved_napcat_token = self._resolve_napcat_token()

        self._session: aiohttp.ClientSession | None = None
        self._monitor_task: asyncio.Task | None = None
        self._last_state: LoginState | None = None
        self._check_lock = asyncio.Lock()
        self._webui_credential: str | None = None

    async def initialize(self):
        await self._ensure_session()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._log(
            "插件已启动"
            f" | 监控地址: {self.napcat_url}"
            f" | 检查间隔: {self.check_interval} 秒"
            f" | 通知目标: {len(self.notify_umos)}"
            f" | WebUI Token: {'已就绪' if self._resolved_napcat_token else '未配置'}"
        )

    async def terminate(self):
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._session and not self._session.closed:
            await self._session.close()

        self._log("插件已停止")

    async def _monitor_loop(self):
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(f"监控循环异常: {exc}", level="ERROR", exc_info=True)
            await asyncio.sleep(self.check_interval)

    async def check_once(self) -> LoginState:
        async with self._check_lock:
            state = await self._fetch_login_state()
            await self._handle_state_update(state)
            self._last_state = state
            return state

    async def _handle_state_update(self, state: LoginState):
        previous = self._last_state

        if self._should_log_state(previous, state):
            self._log_state(state)

        if (
            previous
            and previous.state == "logged_in"
            and state.state == "logged_out"
        ):
            await self._send_logout_notifications(state)

        if previous and previous.state != "logged_in" and state.state == "logged_in":
            self._log(
                f"NapCat 登录状态已恢复 | 账号: {self._format_account(state)}",
                level="INFO",
            )

    async def _fetch_login_state(self) -> LoginState:
        errors: list[str] = []

        webui_state, webui_error = await self._fetch_login_state_via_webui()
        if webui_state is not None:
            return webui_state
        if webui_error:
            errors.append(webui_error)

        onebot_state, onebot_error = await self._fetch_login_state_via_onebot()
        if onebot_state is not None:
            return onebot_state
        if onebot_error:
            errors.append(onebot_error)

        detail = "；".join(errors) if errors else "未拿到任何可用响应。"
        return LoginState(
            state="error",
            endpoint=" / ".join(self._candidate_login_urls()),
            detail=detail,
        )

    async def _fetch_login_state_via_webui(
        self,
    ) -> tuple[LoginState | None, str | None]:
        credential, auth_error = await self._request_webui_credential()
        if not credential:
            return None, f"WebUI 鉴权失败: {auth_error}"

        status_endpoint = f"{self.napcat_url}/api/QQLogin/CheckLoginStatus"
        status_payload, status_error = await self._post_json(
            status_endpoint,
            {},
            headers=self._build_webui_headers(credential),
        )
        if status_payload is None:
            return None, f"WebUI 登录检测失败: {status_endpoint}: {status_error}"

        auth_failure = self._payload_indicates_auth_failure(status_payload)
        if auth_failure:
            return None, f"WebUI 登录检测失败: {status_endpoint}: {auth_failure}"

        code = status_payload.get("code")
        if code not in (0, "0", None):
            message = self._extract_message(status_payload) or "unknown"
            return None, (
                f"WebUI 登录检测失败: {status_endpoint}: "
                f"code={code}, message={message}"
            )

        data = status_payload.get("data")
        if not isinstance(data, dict):
            return None, f"WebUI 登录检测失败: {status_endpoint}: 返回结构异常"

        is_login = data.get("isLogin")
        login_error = self._normalize_text(data.get("loginError"))
        qr_url = self._normalize_text(data.get("qrcodeurl"))

        if is_login is False:
            detail = "WebUI 检测到当前未登录 QQ。"
            if login_error:
                detail = f"{detail} 原因: {login_error}"
            elif qr_url:
                detail = f"{detail} 可用二维码地址: {qr_url}"
            return (
                LoginState(
                    state="logged_out",
                    endpoint=status_endpoint,
                    detail=detail,
                ),
                None,
            )

        if is_login is not True:
            return None, f"WebUI 登录检测失败: {status_endpoint}: isLogin 字段缺失"

        info_endpoint = f"{self.napcat_url}/api/QQLogin/GetQQLoginInfo"
        info_payload, info_error = await self._post_json(
            info_endpoint,
            {},
            headers=self._build_webui_headers(credential),
        )
        if info_payload is None:
            return (
                LoginState(
                    state="logged_in",
                    endpoint=status_endpoint,
                    detail=(
                        "WebUI 检测到 QQ 已登录，但获取账号信息失败。"
                        f" {info_endpoint}: {info_error}"
                    ),
                ),
                None,
            )

        user_id, nickname = self._extract_login_identity(info_payload)
        detail = "WebUI 检测到 QQ 已登录。"
        info_message = self._extract_message(info_payload)
        if info_message and info_message.lower() != "success":
            detail = f"{detail} 附加信息: {info_message}"

        return (
            LoginState(
                state="logged_in",
                endpoint=f"{status_endpoint} + {info_endpoint}",
                detail=detail,
                user_id=user_id,
                nickname=nickname,
            ),
            None,
        )

    async def _fetch_login_state_via_onebot(
        self,
    ) -> tuple[LoginState | None, str | None]:
        errors: list[str] = []
        candidates = [
            ("GET", f"{self.napcat_url}/api/get_login_info"),
            ("GET", f"{self.napcat_url}/get_login_info"),
            ("POST", f"{self.napcat_url}/api/get_login_info"),
            ("POST", f"{self.napcat_url}/get_login_info"),
        ]

        for method, endpoint in candidates:
            payload, error_detail = await self._request_json(method, endpoint)
            if error_detail is not None:
                errors.append(f"{method} {endpoint}: {error_detail}")
                continue

            auth_failure = self._payload_indicates_auth_failure(payload)
            if auth_failure:
                errors.append(f"{method} {endpoint}: {auth_failure}")
                continue

            return (
                self._build_login_state_from_payload(
                    f"{method} {endpoint}",
                    payload,
                ),
                None,
            )

        return None, "；".join(errors) if errors else "onebot 登录态接口不可用"

    async def _request_json(
        self,
        method: str,
        url: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        session = await self._ensure_session()

        try:
            request = getattr(session, method.lower())
            kwargs: dict[str, Any] = {"headers": self._build_headers()}
            if method.upper() == "POST":
                kwargs["json"] = {}
            async with request(url, **kwargs) as response:
                response_text = await response.text()
        except asyncio.TimeoutError:
            return None, f"请求超时（{self.request_timeout_seconds}s）"
        except aiohttp.ClientError as exc:
            return None, f"请求失败: {exc}"
        except Exception as exc:
            return None, f"请求异常: {exc}"

        if response.status != 200:
            return None, f"HTTP {response.status}"

        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError:
            return None, "HTTP 200 非 JSON"

        if not isinstance(payload, dict):
            return None, "JSON 顶层不是对象"

        return payload, None

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        session = await self._ensure_session()

        try:
            async with session.post(url, json=payload, headers=headers) as response:
                response_text = await response.text()
        except asyncio.TimeoutError:
            return None, f"请求超时（{self.request_timeout_seconds}s）"
        except aiohttp.ClientError as exc:
            return None, f"请求失败: {exc}"
        except Exception as exc:
            return None, f"请求异常: {exc}"

        if response.status != 200:
            return None, f"HTTP {response.status}"

        try:
            body = json.loads(response_text)
        except json.JSONDecodeError:
            return None, "HTTP 200 非 JSON"

        if not isinstance(body, dict):
            return None, "JSON 顶层不是对象"

        return body, None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=float(self.request_timeout_seconds))
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _build_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._resolved_napcat_token:
            headers["Authorization"] = f"Bearer {self._resolved_napcat_token}"
            headers["token"] = self._resolved_napcat_token
        return headers

    @staticmethod
    def _build_webui_headers(credential: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }

    def _candidate_login_urls(self) -> list[str]:
        root = self.napcat_url.rstrip("/")
        candidates = [
            f"{root}/api/QQLogin/CheckLoginStatus",
            f"{root}/api/QQLogin/GetQQLoginInfo",
            f"{root}/api/get_login_info",
            f"{root}/get_login_info",
        ]

        deduplicated: list[str] = []
        for candidate in candidates:
            if candidate not in deduplicated:
                deduplicated.append(candidate)
        return deduplicated

    def _build_login_state_from_payload(
        self,
        endpoint: str,
        payload: dict[str, Any],
    ) -> LoginState:
        user_id, nickname = self._extract_login_identity(payload)
        message = self._extract_message(payload)

        if user_id:
            detail = f"GET {endpoint} 返回已登录账号信息。"
            if message:
                detail = f"{detail} 附加信息: {message}"
            return LoginState(
                state="logged_in",
                endpoint=endpoint,
                detail=detail,
                user_id=user_id,
                nickname=nickname,
            )

        detail = f"GET {endpoint} 已响应，但未返回有效账号信息。"
        if message:
            detail = f"{detail} 原因: {message}"
        return LoginState(
            state="logged_out",
            endpoint=endpoint,
            detail=detail,
        )

    async def _request_webui_credential(self) -> tuple[str | None, str | None]:
        if self._webui_credential:
            return self._webui_credential, None

        token = self._resolved_napcat_token
        if not token:
            return None, "未配置 napcat_token，且未从本地 webui.json 读取到 token"

        login_endpoint = f"{self.napcat_url}/api/auth/login"
        errors: list[str] = []
        for payload in (
            {"hash": self._hash_webui_token(token)},
            {"token": token},
        ):
            response, error_detail = await self._post_json(login_endpoint, payload)
            if response is None:
                if error_detail:
                    errors.append(error_detail)
                continue

            credential = self._extract_webui_credential(response)
            if credential:
                self._webui_credential = credential
                return credential, None

            auth_failure = self._payload_indicates_auth_failure(response)
            if auth_failure:
                errors.append(auth_failure)
                continue

            message = self._extract_message(response)
            if message:
                errors.append(message)

        return None, "；".join(errors) if errors else "未解析到 Credential"

    def _resolve_napcat_token(self) -> str:
        if self.napcat_token:
            return self.napcat_token

        path = Path(self.webui_config_path)
        if not path.exists():
            return ""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""

        token = payload.get("token")
        if isinstance(token, str):
            return token.strip()
        return ""

    @staticmethod
    def _hash_webui_token(token: str) -> str:
        return hashlib.sha256(f"{token}.napcat".encode("utf-8")).hexdigest()

    @classmethod
    def _extract_webui_credential(cls, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in ("Credential", "credential", "token", "access_token"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in payload.values():
                found = cls._extract_webui_credential(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = cls._extract_webui_credential(item)
                if found:
                    return found
        return None

    @classmethod
    def _payload_indicates_auth_failure(cls, payload: dict[str, Any]) -> str | None:
        message = cls._extract_message(payload)
        code = payload.get("code")
        text = " ".join(
            part for part in [str(code) if code is not None else "", message or ""] if part
        ).lower()
        keywords = [
            "unauthorized",
            "token is empty",
            "token为空",
            "credential",
            "鉴权",
            "认证",
            "forbidden",
            "未授权",
        ]
        if any(keyword in text for keyword in keywords):
            if message:
                return f"鉴权失败: {message}"
            return "鉴权失败"
        return None

    @classmethod
    def _extract_login_identity(
        cls,
        payload: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        for candidate in cls._collect_candidate_dicts(payload):
            user_id = cls._normalize_user_id(
                candidate.get("user_id")
                or candidate.get("uin")
                or candidate.get("qq")
                or candidate.get("self_id")
                or candidate.get("account")
            )
            if user_id:
                nickname = cls._normalize_text(
                    candidate.get("nickname")
                    or candidate.get("nick")
                    or candidate.get("name")
                )
                return user_id, nickname
        return None, None

    @classmethod
    def _collect_candidate_dicts(cls, payload: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        def append_candidate(value: Any):
            if isinstance(value, dict):
                candidates.append(value)

        append_candidate(payload)
        append_candidate(payload.get("data"))
        append_candidate(payload.get("result"))

        data = payload.get("data")
        if isinstance(data, dict):
            append_candidate(data.get("account"))
            append_candidate(data.get("login_info"))
            append_candidate(data.get("user"))

        return candidates

    @staticmethod
    def _extract_message(payload: dict[str, Any]) -> str | None:
        for candidate in (
            payload.get("message"),
            payload.get("msg"),
        ):
            normalized = NapcatKeeperPlugin._normalize_text(candidate)
            if normalized:
                return normalized
        return None

    async def _send_logout_notifications(self, state: LoginState):
        if not self.notify_umos:
            self._log("检测到退出登录，但未配置通知 UMO。", level="WARNING")
            return

        message = self._build_logout_message(state)

        for umo in self.notify_umos:
            chain = MessageChain().message(message)
            try:
                delivered = await self.context.send_message(umo, chain)
            except Exception as exc:
                self._log(
                    f"退出登录通知发送异常 | UMO: {umo} | 错误: {exc}",
                    level="ERROR",
                    exc_info=True,
                )
                continue

            if delivered:
                self._log(f"退出登录通知发送成功 | UMO: {umo}", level="INFO")
            else:
                self._log(
                    f"退出登录通知发送失败 | UMO: {umo} | 原因: 未找到匹配平台",
                    level="WARNING",
                )

    def _build_logout_message(self, state: LoginState) -> str:
        account_text = self._format_account(state)
        return (
            "NapCat 检测到 QQ 已退出登录\n"
            f"时间: {self._now_text()}\n"
            f"账号: {account_text}\n"
            f"接口: {state.endpoint}\n"
            f"详情: {state.detail}"
        )

    def _format_state_message(self, state: LoginState) -> str:
        return (
            "NapCat 登录状态\n"
            f"状态: {STATE_TEXT.get(state.state, state.state)}\n"
            f"账号: {self._format_account(state)}\n"
            f"接口: {state.endpoint}\n"
            f"详情: {state.detail}"
        )

    def _log_state(self, state: LoginState):
        level = {
            "logged_in": "INFO",
            "logged_out": "WARNING",
            "error": "ERROR",
        }.get(state.state, "INFO")
        self._log(
            "状态检查"
            f" | 登录: {STATE_TEXT.get(state.state, state.state)}"
            f" | 账号: {self._format_account(state)}"
            f" | 详情: {state.detail}",
            level=level,
        )

    def _should_log_state(
        self,
        previous: LoginState | None,
        current: LoginState,
    ) -> bool:
        if self.debug or previous is None:
            return True
        return (
            previous.state != current.state
            or previous.user_id != current.user_id
            or previous.nickname != current.nickname
            or (
                current.state != "logged_in"
                and previous.detail != current.detail
            )
        )

    def _format_account(self, state: LoginState) -> str:
        if not state.user_id:
            return "未识别到登录账号"
        if state.nickname:
            return f"{state.user_id} ({state.nickname})"
        return state.user_id

    def _log(self, message: str, level: str = "INFO", *, exc_info: bool = False):
        log_func = getattr(logger, level.lower(), logger.info)
        log_func(f"{PLUGIN_TAG} {message}", exc_info=exc_info)

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    @staticmethod
    def _parse_int(value: Any, *, default: int, minimum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(parsed, minimum)

    @staticmethod
    def _normalize_user_id(value: Any) -> str | None:
        if value in (None, "", 0, "0"):
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _normalize_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _normalize_umo_list(cls, raw_value: Any) -> list[str]:
        if raw_value is None:
            return []

        if isinstance(raw_value, str):
            parts = raw_value.replace(",", "\n").splitlines()
        elif isinstance(raw_value, (list, tuple, set)):
            parts = list(raw_value)
        else:
            parts = [raw_value]

        normalized: list[str] = []
        for part in parts:
            text = cls._normalize_text(part)
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    @staticmethod
    def _normalize_root_url(raw_url: str) -> str:
        text = raw_url.strip() or DEFAULT_NAPCAT_URL
        if "://" not in text:
            text = f"http://{text}"

        parsed = urlsplit(text)
        path = parsed.path.rstrip("/")
        for suffix in ("/webui", "/api"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break

        normalized = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        return normalized.rstrip("/")

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @filter.command("napcat_status")
    async def napcat_status(self, event: AstrMessageEvent):
        state = await self.check_once()
        return event.plain_result(self._format_state_message(state))
