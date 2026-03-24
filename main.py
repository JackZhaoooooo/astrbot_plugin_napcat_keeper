"""
NapCat QQ 保活插件
自动检测 NapCat 登录状态，掉线时自动重启/重登恢复
"""

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import aiohttp

from astrbot.api import AstrBotConfig, logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_TAG = "[NapcatKeeper]"
LOG_FILE = "/root/AstrBot/logs/napcat_keeper.log"
FILE_LOGGER_NAME = "astrbot_plugin_napcat_keeper.file"
SUCCESS_HTTP_CODES = {200, 301, 302, 303, 307, 308}
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

        self.qq_account = config.get("qq_account", "")
        self.qq_password = config.get("qq_password", "")

        self._consecutive_failures = 0
        self._is_monitoring = False
        self._monitor_task = None
        self._last_restart_time = None
        self._login_info = None
        self._check_count = 0
        self._last_snapshot = None

        self._log("=" * 50)
        self._log("NapcatKeeper 插件初始化")
        self._log(f"NapCat URL: {self.napcat_url}")
        self._log(f"检查间隔: {self.check_interval}秒")
        self._log(f"自动恢复: {'启用' if self.enable_auto_restart else '禁用'}")
        self._log(f"自动登录: {'启用' if self.enable_auto_login else '禁用'}")
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
    def _normalize_user_id(raw_user_id: Any) -> str | None:
        if raw_user_id in (None, "", 0, "0"):
            return None
        user_id = str(raw_user_id).strip()
        return user_id or None

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
            f"[{current_time}] 第 {self._check_count} 次检查 - NapCat 服务检测: "
            f"{self._service_state_text(service.state)} | 地址: {service.checked_url} | "
            f"响应: {http_text} | 说明: {service.detail}"
        )

    def _format_login_log(self, current_time: str, login: LoginCheckResult | None) -> str:
        if login is None:
            return (
                f"[{current_time}] 第 {self._check_count} 次检查 - QQ 登录检测: "
                "⚪ 已跳过 | 原因: NapCat 服务不可达，未执行登录态检查。"
            )

        account_text = (
            f"{login.nickname or '未知昵称'} ({login.user_id})"
            if login.user_id
            else "未识别到登录账号"
        )
        return (
            f"[{current_time}] 第 {self._check_count} 次检查 - QQ 登录检测: "
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
            f"[{current_time}] 第 {self._check_count} 次检查 - 综合判定: "
            f"{STATUS_TEXT.get(snapshot.overall_status, '❓ 未知')} | "
            f"服务状态: {self._service_state_text(snapshot.service.state)} | "
            f"登录状态: {login_text}"
        )

    def _emit_snapshot_logs(self, current_time: str, snapshot: StatusSnapshot):
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

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后启动监控。"""
        if self._monitor_task and not self._monitor_task.done():
            self._log("监控任务已在运行，跳过重复启动。", "WARNING")
            return

        self._log("AstrBot 加载完成，启动 NapCat 保活监控...")
        self._is_monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self):
        """主监控循环。"""
        self._log(f"监控循环已启动，每 {self.check_interval} 秒检查一次")

        while self._is_monitoring:
            try:
                self._check_count += 1
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._log(f"[{current_time}] 第 {self._check_count} 次检查 - 开始巡检...")

                snapshot = await self._collect_status_snapshot()
                self._emit_snapshot_logs(current_time, snapshot)

                if snapshot.overall_status == "online":
                    if self._consecutive_failures > 0:
                        self._log(
                            f"[{current_time}] 状态已恢复正常 "
                            f"(连续失败 {self._consecutive_failures} 次后恢复)"
                        )
                    self._consecutive_failures = 0
                else:
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
                            f"[{current_time}] 连续失败达到阈值，开始执行恢复... "
                            f"| 触发原因: {failure_reason}",
                            "ERROR",
                        )
                        await self._recover_napcat()
                        self._consecutive_failures = 0

            except Exception as e:
                self._log(f"监控循环异常: {e}", "ERROR", exc_info=True)

            await asyncio.sleep(self.check_interval)

    async def _check_napcat_service_status(self) -> ServiceCheckResult:
        """检查 NapCat WebUI/服务端口是否可达。"""
        checked_url = self.napcat_url
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    checked_url,
                    timeout=aiohttp.ClientTimeout(total=5),
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
                detail="连接超时（5 秒内未收到响应）。",
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

    async def _check_qq_login_status(self) -> LoginCheckResult:
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

    async def _collect_status_snapshot(self) -> StatusSnapshot:
        service = await self._check_napcat_service_status()
        if service.state != "online":
            self._login_info = None
            snapshot = StatusSnapshot(
                overall_status="error",
                service=service,
                login=None,
            )
            self._last_snapshot = snapshot
            return snapshot

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
        self._last_snapshot = snapshot
        return snapshot

    async def _check_napcat_status(self) -> str:
        snapshot = await self._collect_status_snapshot()
        return snapshot.overall_status

    async def _recover_napcat(self):
        """恢复 NapCat。"""
        self._last_restart_time = datetime.now()
        self._log("=" * 50)
        self._log("开始恢复 NapCat")
        self._log("=" * 50)

        try:
            self._log("[1/4] 终止 QQ 进程...")
            subprocess.run(["pkill", "-f", "qq"], check=False, stderr=subprocess.DEVNULL)
            await asyncio.sleep(2)
            subprocess.run(
                ["pkill", "-9", "-f", "QQ"],
                check=False,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(1)
            self._log("[1/4] ✓ 进程已终止")

            self._log("[2/4] 清理残留状态...")
            await self._clear_login_state()
            self._log("[2/4] ✓ 状态已清理")

            self._log("[3/4] 启动 NapCat...")
            await self._start_napcat()
            self._log("[3/4] ✓ 启动命令已执行")

            self._log("[4/4] 等待 NapCat 启动 (15秒)...")
            await asyncio.sleep(15)

            status = "error"
            for i in range(3):
                snapshot = await self._collect_status_snapshot()
                status = snapshot.overall_status
                verify_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._emit_snapshot_logs(verify_time, snapshot)
                if status == "online":
                    self._log("=" * 50)
                    self._log("✓ NapCat 恢复成功!")
                    self._log("=" * 50)
                    return
                self._log(f"[4/4] 等待验证... ({i + 1}/3)", "WARNING")
                await asyncio.sleep(5)

            self._log(f"NapCat 恢复后综合状态: {STATUS_TEXT.get(status, status)}", "WARNING")

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
            await self._recover_napcat()
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
