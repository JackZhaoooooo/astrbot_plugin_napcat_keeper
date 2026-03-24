"""
NapCat QQ 保活插件
自动检测 NapCat 登录状态，掉线时自动重启/重登恢复
"""

import asyncio
import logging
import os
import subprocess
from datetime import datetime

import aiohttp

from astrbot.api import AstrBotConfig, logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_TAG = "[NapcatKeeper]"
LOG_FILE = "/root/AstrBot/logs/napcat_keeper.log"
FILE_LOGGER_NAME = "astrbot_plugin_napcat_keeper.file"


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


file_logger = _build_file_logger()


class NapcatKeeperPlugin(Star):
    """NapCat QQ 保活插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.context = context

        # 配置项
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

        # 账号配置
        self.qq_account = config.get("qq_account", "")
        self.qq_password = config.get("qq_password", "")

        # 状态变量
        self._consecutive_failures = 0
        self._is_monitoring = False
        self._monitor_task = None
        self._last_restart_time = None
        self._login_info = None
        self._check_count = 0

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

        platform_log = getattr(astrbot_logger, normalized_level.lower(), astrbot_logger.info)
        file_log = getattr(file_logger, normalized_level.lower(), file_logger.info)

        file_log(clean_msg, exc_info=exc_info)
        platform_log(f"{PLUGIN_TAG} {clean_msg}", exc_info=exc_info)

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

                self._log(f"[{current_time}] 第 {self._check_count} 次检查 - 开始...")

                status = await self._check_napcat_status()

                if status == "online":
                    self._log(f"[{current_time}] 第 {self._check_count} 次检查 - 结果: 🟢 在线")
                    if self._consecutive_failures > 0:
                        self._log(
                            f"[{current_time}] 状态已恢复正常 (连续失败 {self._consecutive_failures} 次后恢复)"
                        )
                    self._consecutive_failures = 0

                elif status == "offline":
                    self._consecutive_failures += 1
                    self._log(
                        f"[{current_time}] 第 {self._check_count} 次检查 - 结果: 🔴 掉线 "
                        f"(第 {self._consecutive_failures}/{self.max_retries} 次)",
                        "WARNING",
                    )

                    if (
                        self.enable_auto_restart
                        and self._consecutive_failures >= self.max_retries
                    ):
                        self._log(f"[{current_time}] 连续失败达到阈值，开始执行恢复...", "ERROR")
                        await self._recover_napcat()
                        self._consecutive_failures = 0

                elif status == "error":
                    self._consecutive_failures += 1
                    self._log(
                        f"[{current_time}] 第 {self._check_count} 次检查 - 结果: ⚠️ 连接错误 "
                        f"(第 {self._consecutive_failures}/{self.max_retries} 次)",
                        "ERROR",
                    )

                    if (
                        self.enable_auto_restart
                        and self._consecutive_failures >= self.max_retries
                    ):
                        self._log(f"[{current_time}] 连续失败达到阈值，开始执行恢复...", "ERROR")
                        await self._recover_napcat()
                        self._consecutive_failures = 0

            except Exception as e:
                self._log(f"监控循环异常: {e}", "ERROR", exc_info=True)

            await asyncio.sleep(self.check_interval)

    async def _check_napcat_status(self) -> str:
        """检查 NapCat HTTP 端口是否可达。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.napcat_url,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status in [200, 301, 302, 303, 307, 308]:
                        self._log(f"HTTP 状态码: {resp.status} - NapCat 在线", "DEBUG")
                        return "online"
                    self._log(f"HTTP 状态码异常: {resp.status}", "WARNING")
                    return "error"

        except asyncio.TimeoutError:
            self._log("连接超时", "WARNING")
            return "error"
        except aiohttp.ClientError as e:
            self._log(f"连接失败: {e}", "WARNING")
            return "error"
        except Exception as e:
            self._log(f"检查状态异常: {e}", "ERROR", exc_info=True)
            return "error"

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
            subprocess.run(["pkill", "-9", "-f", "QQ"], check=False, stderr=subprocess.DEVNULL)
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
                status = await self._check_napcat_status()
                if status == "online":
                    self._log("=" * 50)
                    self._log("✓ NapCat 恢复成功!")
                    self._log("=" * 50)
                    return
                self._log(f"[4/4] 等待验证... ({i + 1}/3)")
                await asyncio.sleep(5)

            self._log(f"NapCat 恢复后状态: {status}", "WARNING")

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
            status = await self._check_napcat_status()
            status_text = {
                "online": "🟢 在线",
                "offline": "🔴 掉线",
                "error": "⚠️ 连接错误",
            }.get(status, "❓ 未知状态")

            login_info_text = ""
            if self._login_info:
                login_info_text = (
                    f"\n📱 登录: {self._login_info.get('nickname', '未知')} "
                    f"({self._login_info.get('user_id', 'N/A')})"
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
                f"📋 状态: {status_text}\n"
                f"⏱️ 检查间隔: {self.check_interval}秒\n"
                f"📈 已检查: {self._check_count} 次\n"
                f"⚠️ 连续失败: {self._consecutive_failures}/{self.max_retries}\n"
                f"🔧 自动恢复: {'启用' if self.enable_auto_restart else '禁用'}"
                f"{login_info_text}"
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
                yield event.plain_result(f"⚠️ NapCat 正在恢复，当前状态: {status}")

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
            "• 每分钟自动检查端口状态\n"
            "• 掉线时自动重启恢复\n"
            f"• 日志文件: {LOG_FILE}"
        )
        yield event.plain_result(help_text)


# AstrBot 旧版插件扫描会优先寻找类名以 Plugin 结尾的类，这里保留旧类名兼容。
NapcatKeeper = NapcatKeeperPlugin

__all__ = ["NapcatKeeperPlugin", "NapcatKeeper"]
