"""
NapCat QQ 保活插件
自动检测 NapCat 登录状态，掉线时自动重启/重登恢复
"""

import asyncio
import hashlib
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from astrbot.api import AstrBotConfig, logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

PLUGIN_TAG = "[NapcatKeeper]"
LOG_FILE = "/root/AstrBot/logs/napcat_keeper.log"
FILE_LOGGER_NAME = "astrbot_plugin_napcat_keeper.file"
SUCCESS_HTTP_CODES = {200, 301, 302, 303, 307, 308}
WEBUI_SUCCESS_CODES = {0, "0"}
WEBUI_CREDENTIAL_CACHE_TTL_SECONDS = 300
RECOVERY_SERVICE_READY_TIMEOUT_SECONDS = 15
RECOVERY_SERVICE_READY_POLL_SECONDS = 1
RECOVERY_VERIFY_INTERVAL_SECONDS = 2
RECOVERY_VERIFY_ATTEMPTS_WITH_AUTO_LOGIN = 8
RECOVERY_VERIFY_ATTEMPTS_DEFAULT = 4
QR_LOGIN_VALID_SECONDS = 120
MANUAL_LOGIN_COOLDOWN_SECONDS_DEFAULT = 900
STATUS_TEXT = {
    "online": "🟢 在线",
    "offline": "🟡 未登录",
    "error": "🔴 异常",
}
LOGIN_STATE_TEXT = {
    "logged_in": "🟢 已登录",
    "not_logged_in": "🟡 未登录",
    "error": "🔴 检测失败",
}
_file_logger: logging.Logger | None = None


@dataclass(frozen=True)
class ServiceCheckResult:
    state: str
    checked_url: str
    status_code: int | None
    detail: str


@dataclass(frozen=True)
class LoginCheckResult:
    state: str
    endpoint: str
    detail: str
    user_id: str | None = None
    nickname: str | None = None


@dataclass(frozen=True)
class StatusSnapshot:
    overall_status: str
    service: ServiceCheckResult
    login: LoginCheckResult | None


@dataclass(frozen=True)
class AutoLoginAttemptResult:
    submitted: bool
    detail: str
    level: str = "INFO"
    manual_action_required: bool = False


def _build_file_logger() -> logging.Logger:
    """保留一份独立文件日志，平台日志仍以 AstrBot logger 为主。"""
    logger = logging.getLogger(FILE_LOGGER_NAME)
    if getattr(logger, "_napcat_keeper_ready", False):
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(file_handler)
    logger._napcat_keeper_ready = True  # type: ignore[attr-defined]
    return logger


def _get_file_logger() -> logging.Logger:
    global _file_logger
    if _file_logger is None:
        _file_logger = _build_file_logger()
    return _file_logger


class NapcatKeeperPlugin(Star):
    """NapCat QQ 保活插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.context = context

        self.napcat_url = config.get("napcat_url", "http://localhost:6099")
        self.napcat_token = config.get("napcat_token", "")
        self.check_interval = config.get("check_interval", 60)
        self.max_retries = config.get("max_retries", 3)
        self.napcat_dir = config.get("napcat_dir", "/root/AstrBot/napcat")
        self.launcher_script = config.get(
            "launcher_script", "/root/AstrBot/napcat/launcher.sh"
        )
        self.enable_auto_restart = config.get("enable_auto_restart", True)
        self.enable_auto_login = config.get("enable_auto_login", True)
        self.notify_on_restart = config.get("notify_on_restart", True)
        self.debug_mode = self._parse_bool(config.get("debug", False), default=False)

        self.qq_account = config.get("qq_account", "")
        self.qq_password = config.get("qq_password", "")
        self.logout_notify_umos = self._normalize_umo_list(
            config.get("logout_notify_umos", [])
        )
        self.relogin_notify_umos = self._normalize_umo_list(
            config.get("relogin_notify_umos", [])
        )
        self.manual_login_notify_umos = self._normalize_umo_list(
            config.get("manual_login_notify_umos", [])
        )
        self.manual_login_cooldown_seconds = self._parse_int(
            config.get(
                "manual_login_cooldown_seconds",
                MANUAL_LOGIN_COOLDOWN_SECONDS_DEFAULT,
            ),
            default=MANUAL_LOGIN_COOLDOWN_SECONDS_DEFAULT,
            minimum=0,
        )
        self.qr_login_valid_seconds = QR_LOGIN_VALID_SECONDS

        self._consecutive_failures = 0
        self._is_monitoring = False
        self._monitor_task = None
        self._last_restart_time = None
        self._login_info = None
        self._check_count = 0
        self._last_snapshot = None
        self._webui_credential = None
        self._webui_credential_cached_at = 0.0
        self._manual_login_pending_until = 0.0
        self._manual_login_pending_reason = ""
        self._manual_login_pending_context: dict[str, Any] = {}
        self._manual_login_last_retry_at = 0.0
        self._log("=" * 50)
        self._log("NapcatKeeper 插件初始化")
        self._log(f"NapCat URL: {self.napcat_url}")
        self._log(f"检查间隔: {self.check_interval}秒")
        self._log(f"自动恢复: {'启用' if self.enable_auto_restart else '禁用'}")
        self._log(f"二维码登录恢复: {'启用' if self.enable_auto_login else '禁用'}")
        self._log(f"调试日志: {'启用' if self.debug_mode else '禁用'}")
        self._log(f"展示账号: {'已配置' if self.qq_account else '未配置'}")
        logout_notify_text = (
            f"退出登录通知: {len(self.logout_notify_umos)} 个 UMO"
            if self.logout_notify_umos
            else "退出登录通知: 未配置"
        )
        relogin_notify_text = (
            f"重新登录通知: {len(self.relogin_notify_umos)} 个 UMO"
            if self.relogin_notify_umos
            else "重新登录通知: 未配置"
        )
        manual_notify_text = (
            f"登录辅助通知: {len(self.manual_login_notify_umos)} 个 UMO"
            if self.manual_login_notify_umos
            else "登录辅助通知: 未单独配置（默认复用登录态通知目标）"
        )
        self._log(logout_notify_text)
        self._log(relogin_notify_text)
        self._log(manual_notify_text)
        self._log(f"人工登录冷却: {self.manual_login_cooldown_seconds} 秒")
        self._log(
            f"二维码登录有效期: {self.qr_login_valid_seconds} 秒（超时自动刷新）"
        )
        self._log("=" * 50)

    def _log(self, msg: str, level: str = "INFO", *, exc_info=False):
        """统一输出到 AstrBot 平台日志，并保留文件日志。"""
        normalized_level = level.upper()
        clean_msg = msg.strip()
        file_logger = _get_file_logger()

        platform_log = getattr(
            astrbot_logger, normalized_level.lower(), astrbot_logger.info
        )
        file_log = getattr(file_logger, normalized_level.lower(), file_logger.info)

        file_log(clean_msg, exc_info=exc_info)
        platform_log(f"{PLUGIN_TAG} {clean_msg}", exc_info=exc_info)

    def _build_auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = (self.napcat_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _build_webui_auth_headers(credential: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        return headers

    def _normalize_napcat_root_url(self) -> str:
        raw_url = (self.napcat_url or "http://localhost:6099").strip()
        parsed = urlsplit(raw_url)
        path = (parsed.path or "").rstrip("/")
        for suffix in ("/webui", "/api"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")

    def _build_webui_api_url(self, path: str) -> str:
        api_path = f"api/{path.lstrip('/')}"
        return urljoin(self._normalize_napcat_root_url().rstrip("/") + "/", api_path)

    @staticmethod
    def _hash_webui_token(token: str) -> str:
        return hashlib.sha256(f"{token}.napcat".encode("utf-8")).hexdigest()

    @staticmethod
    def _password_md5(password: str) -> str:
        return hashlib.md5(password.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_user_id(raw_user_id: Any) -> str | None:
        if raw_user_id in (None, "", 0, "0"):
            return None
        user_id = str(raw_user_id).strip()
        return user_id or None

    @staticmethod
    def _normalize_string_list(raw_value: Any) -> list[str]:
        if raw_value in (None, ""):
            return []

        items = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
        normalized: list[str] = []
        seen: set[str] = set()

        for item in items:
            if item in (None, ""):
                continue
            text = str(item).replace("，", ",").replace("\r", "\n")
            for part in text.replace(",", "\n").split("\n"):
                umo = part.strip()
                if umo and umo not in seen:
                    seen.add(umo)
                    normalized.append(umo)

        return normalized

    @classmethod
    def _normalize_umo_list(cls, raw_value: Any) -> list[str]:
        return cls._normalize_string_list(raw_value)

    @staticmethod
    def _parse_bool(raw_value: Any, *, default: bool) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        if raw_value in (None, ""):
            return default
        if isinstance(raw_value, (int, float)):
            return raw_value != 0

        text = str(raw_value).strip().lower()
        if text in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
        return default

    @staticmethod
    def _parse_int(
        raw_value: Any,
        *,
        default: int,
        minimum: int | None = None,
    ) -> int:
        if raw_value in (None, ""):
            value = default
        else:
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    @classmethod
    def _extract_login_identity(
        cls, payload: dict[str, Any] | None
    ) -> tuple[str | None, str | None]:
        if not isinstance(payload, dict):
            return None, None

        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return None, None

        user_id = cls._normalize_user_id(
            data.get("user_id") or data.get("uin") or data.get("qq")
        )
        nickname = data.get("nickname") or data.get("nick") or data.get("name")
        if nickname is not None:
            nickname = str(nickname).strip() or None
        return user_id, nickname

    @staticmethod
    def _extract_payload_message(payload: dict[str, Any] | None) -> str | None:
        if not isinstance(payload, dict):
            return None

        candidates = [payload]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)

        for item in candidates:
            for key in ("message", "msg", "retmsg", "wording", "error"):
                value = item.get(key)
                if value not in (None, ""):
                    text = str(value).strip()
                    if text:
                        return text
        return None

    @staticmethod
    def _extract_webui_message(payload: dict[str, Any] | None) -> str | None:
        if not isinstance(payload, dict):
            return None

        message = payload.get("message")
        if message in (None, ""):
            return None

        text = str(message).strip()
        if not text or text.lower() == "success":
            return None
        return text

    @staticmethod
    def _looks_like_not_logged_in(message: str | None) -> bool:
        if not message:
            return False
        lowered = message.lower()
        keywords = [
            "未登录",
            "未扫码",
            "扫码",
            "二维码",
            "not login",
            "not logged",
            "login required",
        ]
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _looks_like_already_logged_in(message: str | None) -> bool:
        if not message:
            return False
        lowered = message.lower()
        keywords = [
            "qq is logined",
            "already login",
            "already logged",
            "已登录",
            "already online",
        ]
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _looks_like_rate_limited(message: str | None) -> bool:
        if not message:
            return False
        lowered = message.lower()
        keywords = [
            "rate limit",
            "too many",
            "too frequent",
            "频率",
            "限流",
            "过于频繁",
        ]
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _extract_user_id_from_text(message: str | None) -> str | None:
        if not message:
            return None

        patterns = [
            r"账号[\(\（]?(\d{5,})[\)\）]?",
            r"uin[\s:=]+(\d{5,})",
            r"qq[\s:=]+(\d{5,})",
            r"\b(\d{5,})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _is_webui_success(payload: dict[str, Any] | None) -> bool:
        return isinstance(payload, dict) and payload.get("code") in WEBUI_SUCCESS_CODES

    @staticmethod
    def _extract_webui_response_data(payload: dict[str, Any] | None) -> Any:
        if not isinstance(payload, dict):
            return None
        return payload.get("data")

    @staticmethod
    def _summarize_response_text(text: str | None, *, limit: int = 160) -> str | None:
        if not text:
            return None
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    @classmethod
    def _build_webui_error_detail(
        cls,
        action: str,
        payload: dict[str, Any] | None = None,
        raw_text: str | None = None,
    ) -> str:
        message = cls._extract_webui_message(payload) or cls._summarize_response_text(
            raw_text
        )
        if message:
            return f"{action}: {message}"
        return action

    @staticmethod
    def _append_detail(base: str, extra: str | None) -> str:
        if not extra:
            return base
        extra = extra.strip()
        if not extra or extra in base:
            return base
        return f"{base} | {extra}"

    @staticmethod
    def _format_wait_seconds(seconds: int) -> str:
        seconds = max(0, int(seconds))
        minutes, remain_seconds = divmod(seconds, 60)
        hours, remain_minutes = divmod(minutes, 60)
        parts: list[str] = []
        if hours:
            parts.append(f"{hours} 小时")
        if remain_minutes:
            parts.append(f"{remain_minutes} 分")
        if remain_seconds or not parts:
            parts.append(f"{remain_seconds} 秒")
        return " ".join(parts)

    def _manual_login_notification_targets(self) -> list[str]:
        if self.manual_login_notify_umos:
            return list(self.manual_login_notify_umos)
        return self._normalize_string_list(
            [
                *self.logout_notify_umos,
                *self.relogin_notify_umos,
            ]
        )

    def _get_reusable_manual_login_context(self, *, account: str) -> dict[str, Any]:
        if not self._is_manual_login_pending():
            return {}

        context = dict(self._manual_login_pending_context)
        if str(context.get("account") or "").strip() != str(account or "").strip():
            return {}
        return context

    @staticmethod
    def _normalize_timestamp(raw_value: Any) -> float:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return 0.0
        return value if value > 0 else 0.0

    def _manual_login_qrcode_remaining_seconds(
        self,
        context: dict[str, Any] | None = None,
    ) -> int:
        current_context = context or self._manual_login_pending_context
        expires_at = self._normalize_timestamp(current_context.get("qrcode_expires_at"))
        if expires_at <= 0:
            return 0
        return max(0, int(expires_at - time.monotonic()))

    def _has_valid_manual_login_qrcode(
        self,
        context: dict[str, Any] | None = None,
    ) -> bool:
        current_context = context or self._manual_login_pending_context
        qrcode_url = str(current_context.get("qrcode_url") or "").strip()
        return bool(qrcode_url) and self._manual_login_qrcode_remaining_seconds(
            current_context
        ) > 0

    def _store_manual_login_qrcode(
        self,
        context: dict[str, Any],
        qrcode_url: str,
    ):
        now = time.monotonic()
        context["qrcode_url"] = qrcode_url
        context["qrcode_generated_at"] = now
        context["qrcode_expires_at"] = now + self.qr_login_valid_seconds

    def _update_manual_login_pending(
        self,
        detail: str,
        *,
        context: dict[str, Any] | None = None,
        preserve_window: bool = False,
    ):
        merged_context = dict(self._manual_login_pending_context)
        if context:
            merged_context.update(context)

        if preserve_window and self._is_manual_login_pending():
            self._manual_login_pending_reason = detail
            self._manual_login_pending_context = merged_context
            return

        self._enter_manual_login_pending(detail, context=merged_context)

    def _clear_manual_login_pending(self):
        self._manual_login_pending_until = 0.0
        self._manual_login_pending_reason = ""
        self._manual_login_pending_context = {}
        self._manual_login_last_retry_at = 0.0

    def _is_manual_login_pending(self) -> bool:
        if self._manual_login_pending_until <= 0:
            return False
        if time.monotonic() >= self._manual_login_pending_until:
            self._clear_manual_login_pending()
            return False
        return True

    def _manual_login_pending_remaining_seconds(self) -> int:
        if not self._is_manual_login_pending():
            return 0
        return max(0, int(self._manual_login_pending_until - time.monotonic()))

    def _enter_manual_login_pending(
        self,
        detail: str,
        *,
        context: dict[str, Any] | None = None,
    ):
        cooldown = max(0, int(self.manual_login_cooldown_seconds))
        if cooldown <= 0:
            self._clear_manual_login_pending()
            return

        self._manual_login_pending_until = time.monotonic() + cooldown
        self._manual_login_pending_reason = detail
        self._manual_login_pending_context = dict(context or {})
        self._manual_login_last_retry_at = time.monotonic()
        self._consecutive_failures = 0
        self._log(
            "已进入二维码登录等待模式，后续仅轮询登录状态，暂不重复重启 NapCat。"
            f" | 冷却时长: {self._format_wait_seconds(cooldown)}"
            f" | 原因: {detail}",
            "WARNING",
        )

    def _should_hold_manual_login_pending(self, snapshot: StatusSnapshot) -> bool:
        if not self._is_manual_login_pending():
            return False
        return snapshot.service.state == "online" and snapshot.overall_status != "online"

    def _should_retry_manual_login_pending(self) -> bool:
        if not self._is_manual_login_pending():
            return False
        if not str(self._manual_login_pending_context.get("qrcode_url") or "").strip():
            return True
        return self._manual_login_qrcode_remaining_seconds() <= 0

    def _mark_manual_login_retry(self):
        self._manual_login_last_retry_at = time.monotonic()

    async def _notify_manual_login_required(
        self,
        detail: str,
        *,
        account: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> bool:
        targets = self._manual_login_notification_targets()
        if not targets:
            self._log(
                "检测到需要人工完成 QQ 登录，但未配置登录辅助通知目标，仅保留日志输出。",
                "WARNING",
            )
            return False

        context = context or {}
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "NapCat Keeper 检测到 QQ 登录需要人工处理",
            f"时间: {current_time}",
            f"地址: {self.napcat_url}",
        ]
        if account:
            lines.append(f"账号: {account}")
        qrcode_url = context.get("qrcode_url")
        if qrcode_url:
            lines.append(f"二维码地址: {qrcode_url}")
            qrcode_remaining_seconds = self._manual_login_qrcode_remaining_seconds(
                context
            )
            lines.append(
                "二维码有效期: "
                f"{self._format_wait_seconds(qrcode_remaining_seconds or self.qr_login_valid_seconds)}"
                "（超时后插件会自动重新生成并再次通知）"
            )
        lines.append(f"说明: {detail}")
        lines.append(
            "插件已切换为二维码登录等待模式，后续仅轮询登录状态，不再重复重启 NapCat。"
        )
        delivered = await self._send_notification_to_umos(
            targets,
            "\n".join(lines),
            "登录辅助通知",
        )
        if not delivered:
            self._log("登录辅助通知未通过任何 UMO 发送成功。", "WARNING")
        return delivered

    async def _handle_manual_login_pending_snapshot(
        self,
        snapshot: StatusSnapshot,
        current_time: str,
    ) -> bool:
        if not self._should_hold_manual_login_pending(snapshot):
            return False

        remaining_seconds = self._manual_login_pending_remaining_seconds()
        hold_level = "WARNING" if snapshot.overall_status == "offline" else "ERROR"
        qrcode_remaining_seconds = self._manual_login_qrcode_remaining_seconds()

        if self.enable_auto_login and self._should_retry_manual_login_pending():
            self._mark_manual_login_retry()
            self._log(
                f"[{current_time}] 当前处于二维码登录等待期，"
                "当前二维码已超时，开始重新生成二维码并复查登录状态，不重启 NapCat。"
                f" | 剩余等待: {self._format_wait_seconds(remaining_seconds)}"
                f" | 原因: {self._manual_login_pending_reason}",
                hold_level,
            )
            await self._recover_login_only(snapshot, keep_manual_pending=True)
            return True

        self._consecutive_failures = 0
        self._log(
            f"[{current_time}] 当前处于二维码登录等待期，"
            f"剩余 {self._format_wait_seconds(remaining_seconds)}，"
            f"当前二维码剩余有效期约 {self._format_wait_seconds(qrcode_remaining_seconds)}，"
            "本轮继续等待扫码登录。"
            f" | 原因: {self._manual_login_pending_reason}",
            hold_level,
        )
        return True

    async def _fetch_login_qrcode(
        self,
        session: aiohttp.ClientSession,
        credential: str,
        *,
        refresh: bool = True,
    ) -> tuple[str | None, str]:
        refresh_endpoint = self._build_webui_api_url("/QQLogin/RefreshQRcode")
        refresh_note = ""
        if refresh:
            try:
                (
                    refresh_endpoint,
                    refresh_code,
                    refresh_payload,
                    refresh_raw_text,
                ) = await self._call_webui_api(
                    session,
                    "/QQLogin/RefreshQRcode",
                    credential=credential,
                )
                if refresh_payload is not None and not self._is_webui_success(refresh_payload):
                    refresh_message = self._extract_webui_message(refresh_payload)
                    if not self._looks_like_already_logged_in(refresh_message):
                        refresh_note = (
                            f"刷新二维码未成功: "
                            f"{self._build_webui_error_detail('RefreshQRcode 失败', refresh_payload, refresh_raw_text)}"
                            f" | 接口: {refresh_endpoint} | HTTP {refresh_code}"
                        )
            except Exception as e:
                refresh_note = f"刷新二维码异常: {e} | 接口: {refresh_endpoint}"

        (
            endpoint,
            status_code,
            payload,
            raw_text,
        ) = await self._call_webui_api(
            session,
            "/QQLogin/GetQQLoginQrcode",
            credential=credential,
        )
        if payload is None:
            response_text = raw_text or f"HTTP {status_code}"
            detail = (
                "获取 QQ 登录二维码失败，接口返回了非 JSON 响应。"
                f" | 接口: {endpoint} | 响应: {response_text}"
            )
            return None, self._append_detail(detail, refresh_note)

        if not self._is_webui_success(payload):
            detail = (
                f"{self._build_webui_error_detail('获取 QQ 登录二维码失败', payload, raw_text)}"
                f" | 接口: {endpoint} | HTTP {status_code}"
            )
            return None, self._append_detail(detail, refresh_note)

        data = self._extract_webui_response_data(payload)
        qrcode_url = None
        if isinstance(data, dict):
            qrcode_url = data.get("qrcode") or data.get("qrCode")
        elif isinstance(data, str):
            qrcode_url = data

        if qrcode_url:
            detail = f"已获取 QQ 登录二维码。 | 接口: {endpoint}"
            return str(qrcode_url).strip(), self._append_detail(detail, refresh_note)

        detail = f"获取 QQ 登录二维码失败，接口未返回二维码地址。 | 接口: {endpoint}"
        return None, self._append_detail(detail, refresh_note)

    async def _prepare_manual_login_assistance(
        self,
        session: aiohttp.ClientSession,
        credential: str,
        account: str,
        detail: str,
        *,
        notify: bool = True,
    ) -> str:
        assisted_detail = detail
        context: dict[str, Any] = self._get_reusable_manual_login_context(
            account=account
        )
        context["account"] = account
        previous_qrcode_url = str(context.get("qrcode_url") or "").strip()
        qrcode_refreshed = False

        if self._has_valid_manual_login_qrcode(context):
            remaining_seconds = self._manual_login_qrcode_remaining_seconds(context)
            assisted_detail = self._append_detail(
                assisted_detail,
                "沿用当前 QQ 登录二维码"
                f"（剩余约 {self._format_wait_seconds(remaining_seconds)}）: "
                f"{previous_qrcode_url}",
            )
        else:
            try:
                qrcode_url, qrcode_detail = await self._fetch_login_qrcode(
                    session,
                    credential,
                    refresh=True,
                )
                if qrcode_url:
                    self._store_manual_login_qrcode(context, qrcode_url)
                    qrcode_refreshed = True
                    qrcode_valid_text = self._format_wait_seconds(
                        self.qr_login_valid_seconds
                    )
                    if previous_qrcode_url:
                        assisted_detail = self._append_detail(
                            assisted_detail,
                            "上一张二维码已超时，已自动生成新的 QQ 登录二维码"
                            f"（{qrcode_valid_text}内有效）: {qrcode_url}",
                        )
                    else:
                        assisted_detail = self._append_detail(
                            assisted_detail,
                            f"已生成 QQ 登录二维码（{qrcode_valid_text}内有效）: {qrcode_url}",
                        )
                    self._log(
                        "已生成 QQ 登录二维码，等待扫码登录。"
                        f" | 账号: {account or '未指定'} | 二维码: {qrcode_url}"
                    )
                else:
                    context.pop("qrcode_url", None)
                    context.pop("qrcode_generated_at", None)
                    context.pop("qrcode_expires_at", None)
                    assisted_detail = self._append_detail(
                        assisted_detail,
                        f"生成 QQ 登录二维码失败: {qrcode_detail}",
                    )
            except Exception as e:
                context.pop("qrcode_url", None)
                context.pop("qrcode_generated_at", None)
                context.pop("qrcode_expires_at", None)
                assisted_detail = self._append_detail(
                    assisted_detail,
                    f"生成 QQ 登录二维码异常: {e}",
                )

        if not self._has_valid_manual_login_qrcode(context):
            self._clear_manual_login_pending()
            return self._append_detail(
                assisted_detail,
                "当前未拿到可用二维码，本轮保持常规恢复策略，下次将继续尝试生成二维码。",
            )

        assisted_detail = self._append_detail(
            assisted_detail,
            "插件将进入二维码登录等待模式，"
            f"在接下来的 {self._format_wait_seconds(self.manual_login_cooldown_seconds)} 内仅轮询登录状态，"
            f"二维码每 {self._format_wait_seconds(self.qr_login_valid_seconds)} 自动刷新一次。",
        )
        self._update_manual_login_pending(
            assisted_detail,
            context=context,
            preserve_window=not qrcode_refreshed,
        )
        should_notify = notify or qrcode_refreshed
        if should_notify:
            await self._notify_manual_login_required(
                assisted_detail,
                account=account or None,
                context=context,
            )
        else:
            self._log(
                "二维码登录等待期内仍未扫码成功，当前二维码仍有效，跳过重复通知。"
                f" | 账号: {account or '未指定'} | 原因: {assisted_detail}",
                "WARNING",
            )
        return assisted_detail

    async def _post_json_request(
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | None, str | None]:
        async with session.post(
            endpoint,
            json=payload or {},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            try:
                response_payload = await resp.json(content_type=None)
                raw_text = None
            except (aiohttp.ContentTypeError, ValueError):
                response_payload = None
                raw_text = await resp.text()
            return (
                resp.status,
                response_payload,
                self._summarize_response_text(raw_text),
            )

    async def _request_webui_credential(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        force_refresh: bool = False,
    ) -> str:
        token = (self.napcat_token or "").strip()
        if not token:
            raise ValueError("未配置 napcat_token，无法向 NapCat WebUI 申请凭证。")

        cached_credential = self._webui_credential
        if (
            not force_refresh
            and cached_credential
            and time.monotonic() - self._webui_credential_cached_at
            < WEBUI_CREDENTIAL_CACHE_TTL_SECONDS
        ):
            return cached_credential

        endpoint = self._build_webui_api_url("/auth/login")
        created_session = session is None
        if created_session:
            session = aiohttp.ClientSession()

        try:
            status_code, payload, raw_text = await self._post_json_request(
                session,
                endpoint,
                payload={"hash": self._hash_webui_token(token)},
                headers={"Content-Type": "application/json"},
            )
        finally:
            if created_session and session is not None:
                await session.close()

        if payload is None:
            response_text = raw_text or f"HTTP {status_code}"
            if cached_credential and self._looks_like_rate_limited(response_text):
                self._log(
                    "NapCat WebUI 鉴权触发限流，继续复用已缓存 Credential。",
                    "WARNING",
                )
                return cached_credential
            raise RuntimeError(
                "NapCat WebUI 鉴权接口返回了非 JSON 响应。"
                f" | 接口: {endpoint} | 响应: {response_text}"
            )

        if not self._is_webui_success(payload):
            rate_limit_detail = self._build_webui_error_detail(
                "NapCat WebUI 鉴权失败",
                payload,
                raw_text,
            )
            if cached_credential and self._looks_like_rate_limited(rate_limit_detail):
                self._log(
                    "NapCat WebUI 鉴权触发限流，继续复用已缓存 Credential。",
                    "WARNING",
                )
                return cached_credential
            raise RuntimeError(
                f"{rate_limit_detail}"
                f" | 接口: {endpoint} | HTTP {status_code}"
            )

        data = self._extract_webui_response_data(payload)
        credential = data.get("Credential") if isinstance(data, dict) else None
        if not credential:
            raise RuntimeError(
                f"NapCat WebUI 鉴权成功，但未返回 Credential。 | 接口: {endpoint}"
            )
        self._webui_credential = credential
        self._webui_credential_cached_at = time.monotonic()
        return credential

    async def _call_webui_api(
        self,
        session: aiohttp.ClientSession,
        path: str,
        *,
        credential: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, int, dict[str, Any] | None, str | None]:
        endpoint = self._build_webui_api_url(path)
        status_code, response_payload, raw_text = await self._post_json_request(
            session,
            endpoint,
            payload=payload,
            headers=self._build_webui_auth_headers(credential),
        )
        return endpoint, status_code, response_payload, raw_text

    @classmethod
    def _build_service_result(
        cls,
        checked_url: str,
        *,
        status_code: int | None = None,
        detail: str,
    ) -> ServiceCheckResult:
        state = "online" if status_code in SUCCESS_HTTP_CODES else "error"
        return ServiceCheckResult(
            state=state,
            checked_url=checked_url,
            status_code=status_code,
            detail=detail,
        )

    @classmethod
    def _build_login_result(
        cls,
        endpoint: str,
        *,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
        detail: str | None = None,
        force_state: str | None = None,
    ) -> LoginCheckResult:
        if force_state is not None:
            return LoginCheckResult(
                state=force_state,
                endpoint=endpoint,
                detail=detail or "登录态检测失败。",
            )

        if status_code in (401, 403):
            return LoginCheckResult(
                state="error",
                endpoint=endpoint,
                detail=detail or "鉴权失败，请检查 napcat_token 是否正确。",
            )

        if status_code == 404:
            return LoginCheckResult(
                state="error",
                endpoint=endpoint,
                detail=detail or "未找到 get_login_info 接口。",
            )

        if status_code is not None and status_code >= 400:
            return LoginCheckResult(
                state="error",
                endpoint=endpoint,
                detail=detail or f"接口返回 HTTP {status_code}。",
            )

        user_id, nickname = cls._extract_login_identity(payload)
        if user_id:
            login_detail = detail or f"已获取登录账号信息: {nickname or '未知昵称'} ({user_id})。"
            return LoginCheckResult(
                state="logged_in",
                endpoint=endpoint,
                detail=login_detail,
                user_id=user_id,
                nickname=nickname,
            )

        payload_message = cls._extract_payload_message(payload)
        if cls._looks_like_not_logged_in(payload_message):
            login_detail = payload_message
        else:
            login_detail = (
                detail
                or payload_message
                or "接口已响应，但未返回已登录 QQ 账号信息。"
            )

        return LoginCheckResult(
            state="not_logged_in",
            endpoint=endpoint,
            detail=login_detail,
        )

    @staticmethod
    def _service_state_text(state: str) -> str:
        return "🟢 服务在线" if state == "online" else "🔴 服务异常"

    @staticmethod
    def _login_state_text(state: str) -> str:
        return LOGIN_STATE_TEXT.get(state, "⚪ 未知")

    def _format_service_log(
        self, current_time: str, service: ServiceCheckResult
    ) -> str:
        http_text = (
            f"HTTP {service.status_code}"
            if service.status_code is not None
            else "无 HTTP 响应"
        )
        return (
            f"[{current_time}] NapCat 服务检测: "
            f"{self._service_state_text(service.state)} | 地址: {service.checked_url} | "
            f"响应: {http_text} | 说明: {service.detail}"
        )

    def _format_login_log(self, current_time: str, login: LoginCheckResult | None) -> str:
        if login is None:
            return (
                f"[{current_time}] QQ 登录检测: "
                "⚪ 已跳过 | 原因: NapCat 服务不可达，未执行登录态检查。"
            )

        account_text = (
            f"{login.nickname or '未知昵称'} ({login.user_id})"
            if login.user_id
            else "未识别到登录账号"
        )
        return (
            f"[{current_time}] QQ 登录检测: "
            f"{self._login_state_text(login.state)} | 接口: {login.endpoint} | "
            f"账号: {account_text} | 说明: {login.detail}"
        )

    def _format_summary_log(
        self, current_time: str, snapshot: StatusSnapshot
    ) -> str:
        login_text = (
            self._login_state_text(snapshot.login.state)
            if snapshot.login
            else "⚪ 已跳过"
        )
        return (
            f"[{current_time}] 综合判定: "
            f"{STATUS_TEXT.get(snapshot.overall_status, '❓ 未知')} | "
            f"服务状态: {self._service_state_text(snapshot.service.state)} | "
            f"登录状态: {login_text}"
        )

    def _should_emit_snapshot_logs(
        self,
        snapshot: StatusSnapshot,
        *,
        force: bool = False,
    ) -> bool:
        if force or self.debug_mode:
            return True
        return snapshot.overall_status != "online"

    def _emit_snapshot_logs(
        self,
        current_time: str,
        snapshot: StatusSnapshot,
        *,
        force: bool = False,
    ):
        if not self._should_emit_snapshot_logs(snapshot, force=force):
            return

        service_level = "INFO" if snapshot.service.state == "online" else "ERROR"
        self._log(self._format_service_log(current_time, snapshot.service), service_level)

        login_level = "WARNING"
        if snapshot.login is None:
            login_level = "WARNING"
        elif snapshot.login.state == "logged_in":
            login_level = "INFO"
        elif snapshot.login.state == "error":
            login_level = "ERROR"
        self._log(self._format_login_log(current_time, snapshot.login), login_level)

        summary_level = {
            "online": "INFO",
            "offline": "WARNING",
            "error": "ERROR",
        }.get(snapshot.overall_status, "WARNING")
        self._log(self._format_summary_log(current_time, snapshot), summary_level)

    def _snapshot_failure_reason(self, snapshot: StatusSnapshot) -> str:
        if snapshot.overall_status == "offline" and snapshot.login:
            return f"QQ 登录态异常: {snapshot.login.detail}"
        if snapshot.login and snapshot.login.state == "error":
            return f"QQ 登录检测失败: {snapshot.login.detail}"
        return f"NapCat 服务异常: {snapshot.service.detail}"

    @staticmethod
    def _looks_like_duplicate_login_conflict(message: str | None) -> bool:
        if not message:
            return False
        lowered = message.lower()
        keywords = [
            "无法重复登录",
            "cannot login again",
            "duplicate login",
        ]
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _notification_login_state(snapshot: StatusSnapshot | None) -> str | None:
        if snapshot is None:
            return None
        if snapshot.login is not None:
            return snapshot.login.state
        if snapshot.overall_status == "error":
            return "error"
        return None

    @staticmethod
    def _format_account_identity(login: LoginCheckResult | None) -> str:
        if login and login.user_id:
            return f"{login.nickname or '未知昵称'} ({login.user_id})"
        return "未识别到登录账号"

    @staticmethod
    def _split_umo(umo: str) -> tuple[str | None, str | None, str | None]:
        parts = str(umo or "").split(":", 2)
        if len(parts) != 3:
            return None, None, None
        platform_id, message_type, session_id = (part.strip() for part in parts)
        return (
            platform_id or None,
            message_type or None,
            session_id or None,
        )

    def _resolve_platform_for_umo(self, umo: str) -> tuple[Any | None, Any | None]:
        platform_id, _message_type, _session_id = self._split_umo(umo)
        if not platform_id:
            return None, None

        platform_manager = getattr(self.context, "platform_manager", None)
        platform_insts = getattr(platform_manager, "platform_insts", None)
        if not platform_insts:
            return None, None

        for platform in platform_insts:
            meta_getter = getattr(platform, "meta", None)
            if not callable(meta_getter):
                continue
            try:
                meta = meta_getter()
            except Exception:
                continue
            if getattr(meta, "id", None) == platform_id:
                return platform, meta
        return None, None

    @staticmethod
    def _is_qq_official_platform(meta: Any | None) -> bool:
        if meta is None:
            return False
        platform_name = str(getattr(meta, "name", "") or "").strip().lower()
        return platform_name in {"qq_official", "qq_official_webhook"}

    @staticmethod
    def _read_platform_session_marker(
        platform: Any | None,
        session_id: str | None,
        *attr_names: str,
    ) -> str | None:
        if platform is None or not session_id:
            return None
        for attr_name in attr_names:
            mapping = getattr(platform, attr_name, None)
            if not isinstance(mapping, dict):
                continue
            value = mapping.get(session_id)
            if value not in (None, ""):
                text = str(value).strip()
                if text:
                    return text
        return None

    def _build_qq_official_send_failure_reason(
        self,
        *,
        session_id: str | None,
        inbound_marker: str | None,
        outbound_before: str | None,
        outbound_after: str | None,
    ) -> str:
        if not inbound_marker:
            return (
                "当前会话没有可用的入站 msg_id，QQ 官方平台无法完成主动发送。"
                "请先在该会话中重新与机器人交互后再重试。"
            )
        if outbound_after and outbound_after != outbound_before:
            return ""
        return (
            "已调用 QQ 官方平台发送链路，但未观察到新的出站消息锚点，"
            "本次通知大概率未实际发出。"
        )

    @staticmethod
    def _get_notification_block_reason_from_exception(error: Exception) -> str | None:
        detail = str(error or "").strip()
        if not detail:
            return None

        lowered = detail.lower()
        if "msg_id" in lowered and (
            "无效" in detail
            or "越权" in detail
            or "invalid" in lowered
            or "unauthorized" in lowered
        ):
            return (
                "当前会话的 msg_id 已失效或无权限，AstrBot 无法继续通过该 UMO 主动发送通知。"
                "请先在该会话中重新与机器人交互，或改用其他平台/群聊 UMO。"
            )

        return None

    def _build_transition_notification_message(
        self,
        kind: str,
        previous_snapshot: StatusSnapshot,
        current_snapshot: StatusSnapshot,
        current_time: str,
    ) -> str:
        if kind == "logout":
            identity_source = current_snapshot.login
            if not identity_source or not identity_source.user_id:
                identity_source = previous_snapshot.login
            detail = (
                current_snapshot.login.detail
                if current_snapshot.login
                else current_snapshot.service.detail
            )
            title = "NapCat Keeper 检测到 QQ 已退出登录"
        else:
            identity_source = current_snapshot.login or previous_snapshot.login
            detail = (
                current_snapshot.login.detail
                if current_snapshot.login
                else current_snapshot.service.detail
            )
            title = "NapCat Keeper 检测到 QQ 已重新登录"

        return (
            f"{title}\n"
            f"账号: {self._format_account_identity(identity_source)}\n"
            f"时间: {current_time}\n"
            f"地址: {self.napcat_url}\n"
            f"说明: {detail}"
        )

    async def _send_notification_to_umos(
        self,
        targets: list[str],
        message: str,
        label: str,
    ) -> bool:
        if not targets:
            self._log(f"检测到{label}事件，但未配置通知 UMO，跳过发送。", "DEBUG")
            return False

        any_success = False

        for umo in targets:
            platform, meta = self._resolve_platform_for_umo(umo)
            _platform_id, _message_type, session_id = self._split_umo(umo)
            platform_name = str(getattr(meta, "name", "") or "unknown").strip() or "unknown"
            is_qq_official = self._is_qq_official_platform(meta)
            inbound_marker = self._read_platform_session_marker(
                platform,
                session_id,
                "_session_last_inbound_message_id",
                "_session_last_message_id",
            )
            outbound_before = self._read_platform_session_marker(
                platform,
                session_id,
                "_session_last_outbound_message_id",
            )
            self._log(
                f"{label}开始尝试发送 | UMO: {umo} | 平台: {platform_name}"
                + (
                    f" | 入站锚点: {'已存在' if inbound_marker else '缺失'}"
                    if is_qq_official
                    else ""
                )
            )

            try:
                delivered = await self.context.send_message(
                    umo,
                    MessageChain().message(message),
                )
                if not delivered:
                    self._log(
                        f"{label}发送失败，未找到对应会话 | UMO: {umo}"
                        " | 将继续尝试后续通知目标。",
                        "WARNING",
                    )
                    continue

                if is_qq_official:
                    outbound_after = self._read_platform_session_marker(
                        platform,
                        session_id,
                        "_session_last_outbound_message_id",
                    )
                    if outbound_after and outbound_after != outbound_before:
                        any_success = True
                        self._log(
                            f"{label}已发送 | UMO: {umo} | 平台: {platform_name}"
                            " | QQ 官方平台已生成新的出站消息锚点。"
                        )
                    else:
                        failure_reason = self._build_qq_official_send_failure_reason(
                            session_id=session_id,
                            inbound_marker=inbound_marker,
                            outbound_before=outbound_before,
                            outbound_after=outbound_after,
                        )
                        self._log(
                            f"{label}发送未确认成功 | UMO: {umo} | 平台: {platform_name}"
                            f" | 原因: {failure_reason}"
                            " | 将继续尝试后续通知目标。",
                            "WARNING",
                        )
                    continue

                any_success = True
                self._log(f"{label}已发送 | UMO: {umo}")
            except Exception as e:
                failure_reason = self._get_notification_block_reason_from_exception(e)
                if failure_reason:
                    self._log(
                        f"{label}发送失败 | UMO: {umo} | 原因: {failure_reason}"
                        " | 将继续尝试后续通知目标。",
                        "WARNING",
                    )
                    continue

                self._log(
                    f"{label}发送异常 | UMO: {umo} | 错误: {e}"
                    " | 将继续尝试后续通知目标。",
                    "ERROR",
                    exc_info=True,
                )

        return any_success

    async def _handle_login_transition_notifications(
        self,
        previous_snapshot: StatusSnapshot | None,
        current_snapshot: StatusSnapshot,
        current_time: str,
    ):
        previous_state = self._notification_login_state(previous_snapshot)
        current_state = self._notification_login_state(current_snapshot)

        if previous_state is None or current_state is None:
            return

        if previous_state == "logged_in" and current_state == "not_logged_in":
            message = self._build_transition_notification_message(
                "logout",
                previous_snapshot,
                current_snapshot,
                current_time,
            )
            delivered = await self._send_notification_to_umos(
                self.logout_notify_umos,
                message,
                "退出登录通知",
            )
            if not delivered:
                self._log(
                    "退出登录通知未通过任何 UMO 发送成功。",
                    "WARNING",
                )
            return

        if previous_state in {"not_logged_in", "error"} and current_state == "logged_in":
            message = self._build_transition_notification_message(
                "relogin",
                previous_snapshot,
                current_snapshot,
                current_time,
            )
            delivered = await self._send_notification_to_umos(
                self.relogin_notify_umos,
                message,
                "重新登录通知",
            )
            if not delivered:
                self._log(
                    "重新登录通知未通过任何 UMO 发送成功。",
                    "WARNING",
                )

    async def _ensure_monitor_started(self, trigger: str) -> bool:
        """幂等地启动监控任务，兼容安装后热加载与常规启动。"""
        if self._monitor_task and not self._monitor_task.done():
            self._log(
                f"监控任务已在运行，跳过来自 {trigger} 的重复启动。",
                "DEBUG",
            )
            return False

        self._is_monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._log(f"已通过 {trigger} 启动 NapCat 保活监控。")
        return True

    async def initialize(self):
        """插件加载或热重载后立即启动监控。"""
        await self._ensure_monitor_started("initialize()")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后的兼容兜底，避免旧流程失效。"""
        await self._ensure_monitor_started("on_astrbot_loaded()")

    async def _monitor_loop(self):
        """主监控循环。"""
        self._log(f"监控循环已启动，每 {self.check_interval} 秒检查一次")

        while self._is_monitoring:
            try:
                self._check_count += 1
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if self.debug_mode:
                    self._log(f"[{current_time}] 开始巡检...")

                previous_snapshot = self._last_snapshot
                snapshot = await self._collect_status_snapshot()
                self._emit_snapshot_logs(current_time, snapshot)
                await self._handle_login_transition_notifications(
                    previous_snapshot,
                    snapshot,
                    current_time,
                )
                self._last_snapshot = snapshot

                if snapshot.overall_status == "online":
                    if self._is_manual_login_pending():
                        self._clear_manual_login_pending()
                        self._log(
                            f"[{current_time}] QQ 已重新登录，退出二维码登录等待模式。"
                        )
                    if self._consecutive_failures > 0:
                        self._log(
                            f"[{current_time}] 状态已恢复正常 "
                            f"(连续失败 {self._consecutive_failures} 次后恢复)"
                        )
                    self._consecutive_failures = 0
                else:
                    if await self._handle_manual_login_pending_snapshot(
                        snapshot,
                        current_time,
                    ):
                        await asyncio.sleep(self.check_interval)
                        continue

                    self._consecutive_failures += 1
                    failure_reason = self._snapshot_failure_reason(snapshot)
                    self._log(
                        f"[{current_time}] 当前连续失败: "
                        f"{self._consecutive_failures}/{self.max_retries} | 原因: {failure_reason}",
                        "WARNING" if snapshot.overall_status == "offline" else "ERROR",
                    )

                    if (
                        self.enable_auto_restart
                        and self._consecutive_failures >= self.max_retries
                    ):
                        self._log(
                            f"[{current_time}] 连续失败达到阈值，开始执行 NapCat 全量恢复... "
                            f"| 触发原因: {failure_reason}",
                            "ERROR",
                        )
                        await self._recover_for_snapshot(snapshot)
                        self._consecutive_failures = 0

            except Exception as e:
                self._log(f"监控循环异常: {e}", "ERROR", exc_info=True)

            await asyncio.sleep(self.check_interval)

    async def _check_napcat_service_status(
        self,
        *,
        timeout_seconds: int = 5,
    ) -> ServiceCheckResult:
        """检查 NapCat WebUI/服务端口是否可达。"""
        checked_url = self.napcat_url
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    checked_url,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as resp:
                    if resp.status in SUCCESS_HTTP_CODES:
                        return self._build_service_result(
                            checked_url,
                            status_code=resp.status,
                            detail=f"HTTP {resp.status}，NapCat 服务可达。",
                        )
                    return self._build_service_result(
                        checked_url,
                        status_code=resp.status,
                        detail=f"HTTP {resp.status}，NapCat 服务返回异常状态。",
                    )
        except asyncio.TimeoutError:
            return self._build_service_result(
                checked_url,
                detail=f"连接超时（{timeout_seconds} 秒内未收到响应）。",
            )
        except aiohttp.ClientError as e:
            return self._build_service_result(
                checked_url,
                detail=f"连接失败: {e}",
            )
        except Exception as e:
            return self._build_service_result(
                checked_url,
                detail=f"检查服务状态时发生异常: {e}",
            )

    async def _check_qq_login_status_via_onebot(self) -> LoginCheckResult:
        """调用 get_login_info 检查 QQ 登录态。"""
        endpoint = urljoin(self.napcat_url.rstrip("/") + "/", "get_login_info")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json={},
                    headers=self._build_auth_headers(),
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    try:
                        payload = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        return self._build_login_result(
                            endpoint,
                            status_code=resp.status,
                            detail="get_login_info 返回了非 JSON 响应，无法确认 QQ 登录态。",
                            force_state="error",
                        )

                    return self._build_login_result(
                        endpoint,
                        status_code=resp.status,
                        payload=payload,
                    )
        except asyncio.TimeoutError:
            return self._build_login_result(
                endpoint,
                detail="调用 get_login_info 超时（5 秒内未收到响应）。",
                force_state="error",
            )
        except aiohttp.ClientError as e:
            return self._build_login_result(
                endpoint,
                detail=f"调用 get_login_info 失败: {e}",
                force_state="error",
            )
        except Exception as e:
            return self._build_login_result(
                endpoint,
                detail=f"检查 QQ 登录态时发生异常: {e}",
                force_state="error",
            )

    async def _check_qq_login_status_via_webui(self) -> LoginCheckResult:
        """优先通过 NapCat WebUI 接口检查 QQ 登录态。"""
        status_endpoint = self._build_webui_api_url("/QQLogin/CheckLoginStatus")
        info_endpoint = self._build_webui_api_url("/QQLogin/GetQQLoginInfo")
        endpoint_label = f"{status_endpoint} + {info_endpoint}"

        try:
            async with aiohttp.ClientSession() as session:
                credential = await self._request_webui_credential(session=session)
                (
                    status_endpoint,
                    status_code,
                    status_payload,
                    status_raw_text,
                ) = await self._call_webui_api(
                    session,
                    "/QQLogin/CheckLoginStatus",
                    credential=credential,
                )

                if status_payload is None:
                    response_text = status_raw_text or f"HTTP {status_code}"
                    return LoginCheckResult(
                        state="error",
                        endpoint=status_endpoint,
                        detail=(
                            "CheckLoginStatus 返回了非 JSON 响应。"
                            f" | 响应: {response_text}"
                        ),
                    )

                if not self._is_webui_success(status_payload):
                    return LoginCheckResult(
                        state="error",
                        endpoint=status_endpoint,
                        detail=(
                            f"{self._build_webui_error_detail('调用 CheckLoginStatus 失败', status_payload, status_raw_text)}"
                            f" | HTTP {status_code}"
                        ),
                    )

                status_data = self._extract_webui_response_data(status_payload)
                if not isinstance(status_data, dict):
                    return LoginCheckResult(
                        state="error",
                        endpoint=status_endpoint,
                        detail="CheckLoginStatus 返回格式异常，未包含 data 对象。",
                    )

                is_login = status_data.get("isLogin") is True
                is_offline = status_data.get("isOffline") is True
                login_error = status_data.get("loginError")
                login_error_text = (
                    str(login_error).strip()
                    if login_error not in (None, "")
                    else None
                )

                if not is_login and not is_offline:
                    detail = "WebUI 检测到当前未登录 QQ。"
                    inferred_user_id = None
                    if login_error_text:
                        detail = f"WebUI 检测到当前未登录 QQ: {login_error_text}"
                        if self._looks_like_duplicate_login_conflict(login_error_text):
                            inferred_user_id = (
                                self._extract_user_id_from_text(login_error_text)
                                or self._normalize_user_id(self.qq_account)
                            )
                    return LoginCheckResult(
                        state="not_logged_in",
                        endpoint=status_endpoint,
                        detail=detail,
                        user_id=inferred_user_id,
                    )

                (
                    info_endpoint,
                    info_code,
                    info_payload,
                    info_raw_text,
                ) = await self._call_webui_api(
                    session,
                    "/QQLogin/GetQQLoginInfo",
                    credential=credential,
                )

                user_id = None
                nickname = None
                info_failure = None
                if info_payload is None:
                    response_text = info_raw_text or f"HTTP {info_code}"
                    info_failure = (
                        "GetQQLoginInfo 返回了非 JSON 响应。"
                        f" | 响应: {response_text}"
                    )
                elif not self._is_webui_success(info_payload):
                    info_failure = (
                        f"{self._build_webui_error_detail('调用 GetQQLoginInfo 失败', info_payload, info_raw_text)}"
                        f" | HTTP {info_code}"
                    )
                else:
                    user_id, nickname = self._extract_login_identity(info_payload)

                account_text = (
                    f"{nickname or '未知昵称'} ({user_id})"
                    if user_id
                    else "未识别到登录账号"
                )

                if is_login:
                    detail = f"WebUI 检测到 QQ 已登录: {account_text}。"
                    if info_failure:
                        detail = f"WebUI 检测到 QQ 已登录，但读取账号信息失败: {info_failure}"
                    return LoginCheckResult(
                        state="logged_in",
                        endpoint=f"{status_endpoint} + {info_endpoint}",
                        detail=detail,
                        user_id=user_id,
                        nickname=nickname,
                    )

                detail = f"WebUI 检测到 QQ 账号当前处于离线状态: {account_text}。"
                if info_failure:
                    detail = (
                        "WebUI 检测到 QQ 账号当前处于离线状态，"
                        f"但读取账号信息失败: {info_failure}"
                    )
                return LoginCheckResult(
                    state="not_logged_in",
                    endpoint=f"{status_endpoint} + {info_endpoint}",
                    detail=detail,
                    user_id=user_id,
                    nickname=nickname,
                )
        except (ValueError, RuntimeError) as e:
            return LoginCheckResult(
                state="error",
                endpoint=endpoint_label,
                detail=str(e),
            )
        except asyncio.TimeoutError:
            return LoginCheckResult(
                state="error",
                endpoint=endpoint_label,
                detail="调用 NapCat WebUI QQ 登录接口超时（5 秒内未收到响应）。",
            )
        except aiohttp.ClientError as e:
            return LoginCheckResult(
                state="error",
                endpoint=endpoint_label,
                detail=f"调用 NapCat WebUI QQ 登录接口失败: {e}",
            )
        except Exception as e:
            return LoginCheckResult(
                state="error",
                endpoint=endpoint_label,
                detail=f"NapCat WebUI QQ 登录检测异常: {e}",
            )

    async def _check_qq_login_status(self) -> LoginCheckResult:
        """优先使用 WebUI QQLogin 接口，必要时回退到 get_login_info。"""
        webui_result = await self._check_qq_login_status_via_webui()
        if webui_result.state != "error":
            return webui_result

        onebot_result = await self._check_qq_login_status_via_onebot()
        if onebot_result.state != "error":
            return onebot_result

        return LoginCheckResult(
            state="error",
            endpoint=f"{webui_result.endpoint} | {onebot_result.endpoint}",
            detail=(
                f"NapCat WebUI 检测失败: {webui_result.detail}；"
                f"备用 get_login_info 检测也失败: {onebot_result.detail}"
            ),
        )

    async def _quick_login_by_account(
        self,
        session: aiohttp.ClientSession,
        credential: str,
        account: str,
    ) -> tuple[bool, str]:
        (
            config_endpoint,
            config_code,
            config_payload,
            config_raw_text,
        ) = await self._call_webui_api(
            session,
            "/QQLogin/SetQuickLoginQQ",
            credential=credential,
            payload={"uin": account},
        )
        if config_payload is None:
            response_text = config_raw_text or f"HTTP {config_code}"
            return (
                False,
                "写入快速登录账号失败。"
                f" | 接口: {config_endpoint} | 响应: {response_text}",
            )

        if not self._is_webui_success(config_payload):
            return (
                False,
                f"{self._build_webui_error_detail('写入快速登录账号失败', config_payload, config_raw_text)}"
                f" | 接口: {config_endpoint} | HTTP {config_code}",
            )

        (
            login_endpoint,
            login_code,
            login_payload,
            login_raw_text,
        ) = await self._call_webui_api(
            session,
            "/QQLogin/SetQuickLogin",
            credential=credential,
            payload={"uin": account},
        )
        if login_payload is None:
            response_text = login_raw_text or f"HTTP {login_code}"
            return (
                False,
                "触发快速登录失败，接口返回了非 JSON 响应。"
                f" | 接口: {login_endpoint} | 响应: {response_text}",
            )

        login_message = self._extract_webui_message(login_payload)
        if not self._is_webui_success(login_payload):
            if self._looks_like_duplicate_login_conflict(login_message):
                return (
                    False,
                    "快速登录未成功，当前 QQ 账号已在其他位置登录，"
                    "NapCat 端尚未完成登录。"
                    f" | 原始响应: {login_message} | 接口: {login_endpoint}",
                )
            if self._looks_like_already_logged_in(login_message):
                return (
                    True,
                    f"QQ {account} 当前已经处于登录状态，无需重复执行快速登录。"
                    f" | 接口: {login_endpoint}",
                )
            return (
                False,
                f"{self._build_webui_error_detail('触发快速登录失败', login_payload, login_raw_text)}"
                f" | 接口: {login_endpoint} | HTTP {login_code}",
            )

        return (
            True,
            f"已为 QQ {account} 提交快速登录请求。 | 接口: {login_endpoint}",
        )

    async def _password_login_by_account(
        self,
        session: aiohttp.ClientSession,
        credential: str,
        account: str,
        password: str,
    ) -> tuple[bool, str]:
        (
            endpoint,
            status_code,
            payload,
            raw_text,
        ) = await self._call_webui_api(
            session,
            "/QQLogin/PasswordLogin",
            credential=credential,
            payload={
                "uin": account,
                "passwordMd5": self._password_md5(password),
            },
        )

        if payload is None:
            response_text = raw_text or f"HTTP {status_code}"
            return (
                False,
                "QQ 密码登录接口返回了非 JSON 响应。"
                f" | 接口: {endpoint} | 响应: {response_text}",
            )

        message = self._extract_webui_message(payload)
        if not self._is_webui_success(payload):
            if self._looks_like_duplicate_login_conflict(message):
                return (
                    False,
                    "QQ 密码登录未成功，当前 QQ 账号已在其他位置登录，"
                    "NapCat 端尚未完成登录。"
                    f" | 原始响应: {message} | 接口: {endpoint}",
                )
            if self._looks_like_already_logged_in(message):
                return (
                    True,
                    f"QQ {account} 当前已经处于登录状态，无需重复执行密码登录。"
                    f" | 接口: {endpoint}",
                )
            return (
                False,
                f"{self._build_webui_error_detail('QQ 密码登录失败', payload, raw_text)}"
                f" | 接口: {endpoint} | HTTP {status_code}",
            )

        data = self._extract_webui_response_data(payload)
        if isinstance(data, dict) and data.get("needCaptcha"):
            return False, "QQ 密码登录需要验证码，当前二维码登录模式不会继续处理该流程。"

        if isinstance(data, dict) and data.get("needNewDevice"):
            return False, "QQ 密码登录触发新设备验证，当前二维码登录模式不会继续处理该流程。"

        return (
            True,
            f"已为 QQ {account} 提交密码登录请求。 | 接口: {endpoint}",
        )

    async def _auto_login_qq(
        self,
        *,
        reset_manual_pending: bool = True,
        notify_manual_action: bool = True,
    ) -> AutoLoginAttemptResult:
        account = str(self.qq_account or "").strip()
        if reset_manual_pending:
            self._clear_manual_login_pending()
        if not self.enable_auto_login:
            result = AutoLoginAttemptResult(
                submitted=False,
                detail="已禁用 QQ 二维码登录恢复，跳过二维码登录流程。",
            )
            self._log(result.detail, result.level)
            return result

        self._log(
            "准备生成 QQ 登录二维码"
            f" | 账号: {account or '未配置（扫码后以实际登录账号为准）'}"
        )

        manual_action_required = False
        try:
            async with aiohttp.ClientSession() as session:
                credential = ""
                if str(self.napcat_token or "").strip():
                    credential = await self._request_webui_credential(session=session)
                    self._log("NapCat WebUI 鉴权成功，已获取临时 Credential。")
                else:
                    self._log(
                        "未配置 napcat_token，将直接调用 NapCat WebUI 二维码接口。",
                        "WARNING",
                    )

                detail = await self._prepare_manual_login_assistance(
                    session,
                    credential,
                    account,
                    "NapCat 当前未登录，需要扫码完成 QQ 登录。",
                    notify=notify_manual_action,
                )
                success = False
                manual_action_required = self._has_valid_manual_login_qrcode(
                    self._manual_login_pending_context
                )
        except (ValueError, RuntimeError) as e:
            result = AutoLoginAttemptResult(
                submitted=False,
                detail=str(e),
                level="ERROR",
            )
            self._log(result.detail, result.level)
            return result
        except asyncio.TimeoutError:
            result = AutoLoginAttemptResult(
                submitted=False,
                detail="QQ 二维码登录流程超时（5 秒内未收到响应）。",
                level="ERROR",
            )
            self._log(result.detail, result.level)
            return result
        except aiohttp.ClientError as e:
            result = AutoLoginAttemptResult(
                submitted=False,
                detail=f"QQ 二维码登录请求失败: {e}",
                level="ERROR",
            )
            self._log(result.detail, result.level)
            return result
        except Exception as e:
            result = AutoLoginAttemptResult(
                submitted=False,
                detail=f"QQ 二维码登录流程异常: {e}",
                level="ERROR",
            )
            self._log(result.detail, result.level, exc_info=True)
            return result

        result = AutoLoginAttemptResult(
            submitted=success,
            detail=detail,
            level="INFO" if success else ("WARNING" if manual_action_required else "ERROR"),
            manual_action_required=manual_action_required,
        )
        self._log(result.detail, result.level)
        return result

    async def _wait_for_napcat_service_ready(
        self,
        *,
        timeout_seconds: int = RECOVERY_SERVICE_READY_TIMEOUT_SECONDS,
        poll_interval_seconds: int = RECOVERY_SERVICE_READY_POLL_SECONDS,
    ) -> ServiceCheckResult:
        attempts = max(
            1,
            (timeout_seconds + poll_interval_seconds - 1) // poll_interval_seconds,
        )
        last_result: ServiceCheckResult | None = None

        for attempt in range(attempts):
            last_result = await self._check_napcat_service_status(
                timeout_seconds=max(1, min(2, poll_interval_seconds)),
            )
            if last_result.state == "online":
                return last_result
            if attempt < attempts - 1:
                await asyncio.sleep(poll_interval_seconds)

        if last_result is not None:
            return last_result

        return ServiceCheckResult(
            state="error",
            checked_url=self.napcat_url,
            status_code=None,
            detail=f"等待 NapCat 服务就绪超时（{timeout_seconds} 秒）。",
        )

    async def _verify_recovery_status(
        self,
        *,
        max_attempts: int,
        interval_seconds: int,
    ) -> StatusSnapshot:
        last_snapshot: StatusSnapshot | None = None

        for attempt in range(max_attempts):
            if attempt > 0:
                await asyncio.sleep(interval_seconds)

            snapshot = await self._collect_status_snapshot()
            last_snapshot = snapshot
            verify_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._emit_snapshot_logs(verify_time, snapshot, force=True)

            if snapshot.overall_status == "online":
                return snapshot

            if attempt < max_attempts - 1:
                wait_level = "WARNING" if snapshot.overall_status == "offline" else "ERROR"
                self._log(
                    f"[6/6] 快速验证未通过 ({attempt + 1}/{max_attempts})，"
                    f"{interval_seconds} 秒后重试。"
                    f" | 原因: {self._snapshot_failure_reason(snapshot)}",
                    wait_level,
                )

        if last_snapshot is None:
            raise RuntimeError("恢复验证未生成状态快照。")

        return last_snapshot

    @staticmethod
    def _resolve_verify_attempts(
        auto_login_result: AutoLoginAttemptResult,
        *,
        default_attempts: int,
    ) -> int:
        if auto_login_result.manual_action_required:
            return 1
        if auto_login_result.submitted:
            return RECOVERY_VERIFY_ATTEMPTS_WITH_AUTO_LOGIN
        return default_attempts

    async def _collect_status_snapshot(self) -> StatusSnapshot:
        service = await self._check_napcat_service_status()
        if service.state != "online":
            self._login_info = None
            return StatusSnapshot(
                overall_status="error",
                service=service,
                login=None,
            )

        login = await self._check_qq_login_status()
        if login.state == "logged_in":
            self._login_info = {
                "user_id": login.user_id,
                "nickname": login.nickname or "未知昵称",
            }
            overall_status = "online"
        elif login.state == "not_logged_in":
            self._login_info = None
            overall_status = "offline"
        else:
            self._login_info = None
            overall_status = "error"

        snapshot = StatusSnapshot(
            overall_status=overall_status,
            service=service,
            login=login,
        )
        return snapshot

    async def _check_napcat_status(self) -> str:
        snapshot = await self._collect_status_snapshot()
        return snapshot.overall_status

    async def _recover_for_snapshot(self, snapshot: StatusSnapshot | None = None):
        if snapshot and snapshot.service.state == "online" and snapshot.overall_status != "online":
            self._log(
                "NapCat 服务在线但 QQ 登录状态异常，改为仅执行 QQ 重新登录流程。"
            )
            await self._recover_login_only(snapshot)
            return
        await self._recover_napcat()

    async def _recover_login_only(
        self,
        snapshot: StatusSnapshot | None = None,
        *,
        keep_manual_pending: bool = False,
    ):
        """NapCat 服务仍在线时，仅执行 QQ 重新登录流程。"""
        self._webui_credential = None
        self._webui_credential_cached_at = 0.0
        self._log("=" * 50)
        self._log("NapCat 服务在线，开始仅执行 QQ 二维码登录恢复流程")
        self._log("=" * 50)

        try:
            if snapshot is None:
                snapshot = await self._collect_status_snapshot()
            if snapshot.service.state != "online":
                self._log(
                    "执行 QQ 重新登录前发现 NapCat 服务已不可用，回退到全量恢复流程。",
                    "WARNING",
                )
                await self._recover_napcat()
                return

            auto_login_result = AutoLoginAttemptResult(
                submitted=False,
                detail="未执行 QQ 登录处理。",
            )
            if self.enable_auto_login:
                self._log("[登录恢复] 开始处理 QQ 登录二维码...")
                auto_login_result = await self._auto_login_qq(
                    reset_manual_pending=not keep_manual_pending,
                    notify_manual_action=not keep_manual_pending,
                )
                if auto_login_result.manual_action_required:
                    self._log(
                        "[登录恢复] 已生成 QQ 登录二维码，等待扫码登录。"
                        f" | 结果: {auto_login_result.detail}",
                        "WARNING",
                    )
                elif auto_login_result.submitted:
                    self._log(
                        "[登录恢复] ✓ QQ 登录请求已提交，进入快速验证阶段。"
                        f" | 结果: {auto_login_result.detail}"
                    )
                else:
                    self._log(
                        "[登录恢复] QQ 登录处理未成功。"
                        f" | 原因: {auto_login_result.detail}",
                        auto_login_result.level,
                    )
            else:
                self._log("[登录恢复] 已跳过 QQ 登录处理: 配置已禁用。")

            verify_attempts = self._resolve_verify_attempts(
                auto_login_result,
                default_attempts=RECOVERY_VERIFY_ATTEMPTS_DEFAULT,
            )
            if auto_login_result.manual_action_required:
                verify_attempts = 1
                self._log(
                    "[登录恢复] 检测到当前需要扫码登录，"
                    "本轮只做 1 次状态校验，若二维码超时后续会自动重新生成。",
                    "WARNING",
                )
            self._log(
                "[登录恢复] 开始快速验证恢复结果 "
                f"(最多 {verify_attempts} 次，每 {RECOVERY_VERIFY_INTERVAL_SECONDS} 秒一次)..."
            )
            verified_snapshot = await self._verify_recovery_status(
                max_attempts=verify_attempts,
                interval_seconds=RECOVERY_VERIFY_INTERVAL_SECONDS,
            )

            if verified_snapshot.overall_status == "online":
                if self._is_manual_login_pending():
                    self._clear_manual_login_pending()
                self._log("=" * 50)
                self._log("✓ QQ 重新登录成功!")
                self._log("=" * 50)
                return

            auto_login_reason = ""
            if self.enable_auto_login and not auto_login_result.submitted:
                if auto_login_result.manual_action_required:
                    auto_login_reason = f" | 当前仍在等待二维码扫码登录: {auto_login_result.detail}"
                else:
                    auto_login_reason = f" | QQ 登录处理失败原因: {auto_login_result.detail}"

            final_level = (
                "WARNING"
                if verified_snapshot.overall_status == "offline"
                else "ERROR"
            )
            self._log(
                "QQ 重新登录后综合状态: "
                f"{STATUS_TEXT.get(verified_snapshot.overall_status, verified_snapshot.overall_status)}"
                f"{auto_login_reason}",
                final_level,
            )
        except Exception as e:
            self._log(f"QQ 重新登录流程失败: {e}", "ERROR", exc_info=True)

    async def _recover_napcat(self):
        """恢复 NapCat。"""
        self._last_restart_time = datetime.now()
        self._webui_credential = None
        self._webui_credential_cached_at = 0.0
        self._clear_manual_login_pending()
        self._log("=" * 50)
        self._log("开始恢复 NapCat")
        self._log("=" * 50)

        try:
            self._log("[1/6] 终止 QQ 进程...")
            subprocess.run(["pkill", "-f", "qq"], check=False, stderr=subprocess.DEVNULL)
            await asyncio.sleep(2)
            subprocess.run(
                ["pkill", "-9", "-f", "QQ"],
                check=False,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(1)
            self._log("[1/6] ✓ 进程已终止")

            self._log("[2/6] 清理残留状态...")
            await self._clear_login_state()
            self._log("[2/6] ✓ 状态已清理")

            self._log("[3/6] 启动 NapCat...")
            await self._start_napcat()
            self._log("[3/6] ✓ 启动命令已执行")

            self._log(
                "[4/6] 等待 NapCat 服务就绪 "
                f"(最长 {RECOVERY_SERVICE_READY_TIMEOUT_SECONDS} 秒，"
                f"每 {RECOVERY_SERVICE_READY_POLL_SECONDS} 秒检查一次)..."
            )
            service_ready_result = await self._wait_for_napcat_service_ready()
            if service_ready_result.state == "online":
                self._log(
                    "[4/6] ✓ NapCat 服务已就绪 "
                    f"| 地址: {service_ready_result.checked_url} "
                    f"| 说明: {service_ready_result.detail}"
                )
            else:
                self._log(
                    "[4/6] NapCat 服务尚未完全就绪，继续执行后续恢复流程。"
                    f" | 说明: {service_ready_result.detail}",
                    "WARNING",
                )

            self._log("[5/6] 处理 QQ 登录二维码...")
            auto_login_result = AutoLoginAttemptResult(
                submitted=False,
                detail="未执行 QQ 登录处理。",
            )
            if self.enable_auto_login:
                auto_login_result = await self._auto_login_qq()
                if auto_login_result.manual_action_required:
                    self._log(
                        "[5/6] 已生成 QQ 登录二维码，等待扫码登录。"
                        f" | 结果: {auto_login_result.detail}",
                        "WARNING",
                    )
                elif auto_login_result.submitted:
                    self._log(
                        "[5/6] ✓ QQ 登录请求已提交，进入快速验证阶段。"
                        f" | 结果: {auto_login_result.detail}"
                    )
                else:
                    self._log(
                        "[5/6] QQ 登录处理未成功。"
                        f" | 原因: {auto_login_result.detail}",
                        auto_login_result.level,
                    )
            else:
                self._log("[5/6] 已跳过 QQ 登录处理: 配置已禁用。")

            verify_attempts = self._resolve_verify_attempts(
                auto_login_result,
                default_attempts=RECOVERY_VERIFY_ATTEMPTS_DEFAULT,
            )
            if auto_login_result.manual_action_required:
                verify_attempts = 1
                self._log(
                    "[6/6] 检测到当前需要扫码登录，"
                    "本轮只做 1 次状态校验，若二维码超时后续会自动重新生成。",
                    "WARNING",
                )
            self._log(
                "[6/6] 开始快速验证恢复结果 "
                f"(最多 {verify_attempts} 次，每 {RECOVERY_VERIFY_INTERVAL_SECONDS} 秒一次)..."
            )
            snapshot = await self._verify_recovery_status(
                max_attempts=verify_attempts,
                interval_seconds=RECOVERY_VERIFY_INTERVAL_SECONDS,
            )

            if snapshot.overall_status == "online":
                self._log("=" * 50)
                self._log("✓ NapCat 恢复成功!")
                self._log("=" * 50)
                return

            auto_login_reason = ""
            if self.enable_auto_login and not auto_login_result.submitted:
                if auto_login_result.manual_action_required:
                    auto_login_reason = f" | 当前仍在等待二维码扫码登录: {auto_login_result.detail}"
                else:
                    auto_login_reason = f" | QQ 登录处理失败原因: {auto_login_result.detail}"

            final_level = "WARNING" if snapshot.overall_status == "offline" else "ERROR"
            self._log(
                f"NapCat 恢复后综合状态: "
                f"{STATUS_TEXT.get(snapshot.overall_status, snapshot.overall_status)}"
                f"{auto_login_reason}",
                final_level,
            )

        except Exception as e:
            self._log(f"恢复失败: {e}", "ERROR", exc_info=True)

    async def _clear_login_state(self):
        """清理登录状态。"""
        try:
            napcat_data_dir = os.path.join(self.napcat_dir, "app", ".config", "QQ")
            if os.path.exists(napcat_data_dir):
                self._log(f"清理目录: {napcat_data_dir}")
        except Exception as e:
            self._log(f"清理状态失败: {e}", "WARNING", exc_info=True)

    async def _start_napcat(self):
        """启动 NapCat。"""
        log_path = "/root/AstrBot/napcat_restart.log"
        try:
            with open(log_path, "a", encoding="utf-8") as output:
                if os.path.exists(self.launcher_script):
                    subprocess.Popen(
                        [self.launcher_script],
                        cwd=self.napcat_dir,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    self._log(f"使用启动脚本: {self.launcher_script}")
                else:
                    subprocess.Popen(
                        ["bash", "-c", f"cd {self.napcat_dir} && ./launcher.sh"],
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    self._log(f"使用备选启动: {self.napcat_dir}/launcher.sh")
        except Exception as e:
            self._log(f"启动 NapCat 失败: {e}", "ERROR", exc_info=True)

    async def terminate(self):
        """插件卸载。"""
        self._log("插件卸载，停止监控...")
        self._is_monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            finally:
                self._monitor_task = None

    @filter.command("napcat_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 NapCat 当前状态。"""
        try:
            snapshot = await self._collect_status_snapshot()
            status_text = STATUS_TEXT.get(snapshot.overall_status, "❓ 未知状态")

            login_status_text = (
                self._login_state_text(snapshot.login.state)
                if snapshot.login
                else "⚪ 已跳过"
            )
            login_detail = (
                snapshot.login.detail
                if snapshot.login
                else "NapCat 服务不可达，未执行登录态检查。"
            )

            account_text = ""
            if snapshot.login and snapshot.login.user_id:
                account_text = (
                    f"\n📱 当前账号: {snapshot.login.nickname or '未知昵称'} "
                    f"({snapshot.login.user_id})"
                )

            restart_info = ""
            if self._last_restart_time:
                restart_info = (
                    f"\n🔄 上次恢复: {self._last_restart_time.strftime('%Y-%m-%d %H:%M:%S')}"
                )

            manual_login_info = ""
            if self._is_manual_login_pending():
                qrcode_url = str(
                    self._manual_login_pending_context.get("qrcode_url") or ""
                ).strip()
                qrcode_info = ""
                if qrcode_url:
                    qrcode_info = (
                        f"\n🔳 当前二维码: {qrcode_url}"
                        "\n⌛ 二维码剩余有效期: "
                        f"{self._format_wait_seconds(self._manual_login_qrcode_remaining_seconds())}"
                    )
                manual_login_info = (
                    "\n🔐 二维码登录等待: 是"
                    f"\n⏳ 剩余冷却: {self._format_wait_seconds(self._manual_login_pending_remaining_seconds())}"
                    f"\n🧭 等待原因: {self._manual_login_pending_reason}"
                    f"{qrcode_info}"
                )

            message = (
                f"📊 NapCat 状态监控\n"
                f"─────────────────────────────────\n"
                f"🔗 地址: {self.napcat_url}\n"
                f"📋 综合状态: {status_text}\n"
                f"🌐 NapCat 服务: {self._service_state_text(snapshot.service.state)}\n"
                f"📝 服务详情: {snapshot.service.detail}\n"
                f"👤 QQ 登录: {login_status_text}\n"
                f"📝 登录详情: {login_detail}"
                f"{account_text}\n"
                f"⏱️ 检查间隔: {self.check_interval}秒\n"
                f"📈 已检查: {self._check_count} 次\n"
                f"⚠️ 连续失败: {self._consecutive_failures}/{self.max_retries}\n"
                f"🔧 自动恢复: {'启用' if self.enable_auto_restart else '禁用'}"
                f"{manual_login_info}"
                f"{restart_info}\n"
                f"📁 日志文件: {LOG_FILE}"
            )
            yield event.plain_result(message)

        except Exception as e:
            yield event.plain_result(f"检查状态失败: {e}")

    @filter.command("napcat_recover")
    async def cmd_recover(self, event: AstrMessageEvent):
        """手动恢复 NapCat。"""
        try:
            yield event.plain_result("🔄 正在恢复 NapCat，请稍候...")
            snapshot = await self._collect_status_snapshot()
            await self._recover_for_snapshot(snapshot)
            status = await self._check_napcat_status()

            if status == "online":
                yield event.plain_result("✅ NapCat 恢复成功！")
            else:
                yield event.plain_result(
                    f"⚠️ NapCat 正在恢复，当前综合状态: {STATUS_TEXT.get(status, status)}"
                )

        except Exception as e:
            yield event.plain_result(f"❌ 恢复失败: {e}")

    @filter.command("napcat_keeper_help")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看插件帮助。"""
        help_text = (
            "🔧 NapCat Keeper 保活插件\n"
            "─────────────────────────────────\n"
            "/napcat_status - 查看 NapCat 当前状态\n"
            "/napcat_recover - 手动恢复 NapCat\n"
            "/napcat_keeper_help - 查看帮助\n"
            "─────────────────────────────────\n"
            "💡 功能:\n"
            "• 同时检测 NapCat 服务状态与 QQ 登录态\n"
            "• 掉线时自动重启恢复\n"
            f"• 日志文件: {LOG_FILE}"
        )
        yield event.plain_result(help_text)


NapcatKeeper = NapcatKeeperPlugin

__all__ = [
    "LoginCheckResult",
    "NapcatKeeper",
    "NapcatKeeperPlugin",
    "ServiceCheckResult",
    "StatusSnapshot",
]
