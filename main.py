"""
NapCat QQ 保活插件
自动检测 NapCat 登录状态，掉线时自动重启/重登恢复
"""

import asyncio
import aiohttp
import subprocess
import os
import json
import logging
from datetime import datetime
from pathlib import Path

# 设置独立的日志文件
LOG_FILE = "/root/AstrBot/logs/napcat_keeper.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# 配置独立日志器
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

plugin_logger = logging.getLogger("NapcatKeeper")
plugin_logger.setLevel(logging.INFO)
plugin_logger.addHandler(file_handler)

from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api import AstrBotConfig


class NapcatKeeper(Star):
    """NapCat QQ 保活插件"""
    
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
        self.launcher_script = config.get("launcher_script", "/root/AstrBot/napcat/launcher.sh")
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
        
        # 使用独立日志
        plugin_logger.info("=" * 50)
        plugin_logger.info(" NapcatKeeper 插件初始化")
        plugin_logger.info(f" NapCat URL: {self.napcat_url}")
        plugin_logger.info(f" 检查间隔: {self.check_interval}秒")
        plugin_logger.info(f" 自动恢复: {'启用' if self.enable_auto_restart else '禁用'}")
        plugin_logger.info(f" 自动登录: {'启用' if self.enable_auto_login else '禁用'}")
        plugin_logger.info("=" * 50)
        
        # 同时输出到 AstrBot 日志
        logger.info(f"[NapcatKeeper] 插件初始化完成")
    
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后启动监控"""
        plugin_logger.info("AstrBot 加载完成，启动 NapCat 保活监控...")
        logger.info("[NapcatKeeper] AstrBot 加载完成，启动监控...")
        self._is_monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
    
    async def _monitor_loop(self):
        """主监控循环"""
        plugin_logger.info(f"监控循环已启动，每 {self.check_interval} 秒检测一次")
        
        while self._is_monitoring:
            try:
                self._check_count += 1
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # 记录检测日志
                plugin_logger.info(f"[{current_time}] 第 {self._check_count} 次检测 - 开始...")
                
                status = await self._check_napcat_status()
                
                if status == "online":
                    plugin_logger.info(f"[{current_time}] 第 {self._check_count} 次检测 - 结果: 🟢 在线")
                    if self._consecutive_failures > 0:
                        plugin_logger.info(f"[{current_time}] 状态已恢复正常 (连续失败 {self._consecutive_failures} 次后恢复)")
                    self._consecutive_failures = 0
                    
                elif status == "offline":
                    self._consecutive_failures += 1
                    plugin_logger.warning(f"[{current_time}] 第 {self._check_count} 次检测 - 结果: 🔴 掉线 (第 {self._consecutive_failures}/{self.max_retries} 次)")
                    
                    if self.enable_auto_restart and self._consecutive_failures >= self.max_retries:
                        plugin_logger.error(f"[{current_time}] 连续失败达到阈值，开始执行恢复...")
                        await self._recover_napcat()
                        self._consecutive_failures = 0
                
                elif status == "error":
                    self._consecutive_failures += 1
                    plugin_logger.error(f"[{current_time}] 第 {self._check_count} 次检测 - 结果: ⚠️ 连接错误 (第 {self._consecutive_failures}/{self.max_retries} 次)")
                    
                    if self.enable_auto_restart and self._consecutive_failures >= self.max_retries:
                        plugin_logger.error(f"[{current_time}] 连续失败达到阈值，开始执行恢复...")
                        await self._recover_napcat()
                        self._consecutive_failures = 0
                        
            except Exception as e:
                plugin_logger.error(f"监控循环异常: {e}")
            
            await asyncio.sleep(self.check_interval)
    
    async def _check_napcat_status(self) -> str:
        """检查 NapCat 登录状态"""
        try:
            headers = {}
            if self.napcat_token:
                headers["Authorization"] = f"Bearer {self.napcat_token}"
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.napcat_url}/get_login_info",
                    json={},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "ok" and data.get("retcode") == 0:
                            self._login_info = data.get("data", {})
                            plugin_logger.debug(f"登录信息: {self._login_info}")
                            return "online"
                        plugin_logger.warning(f"API返回异常: {data}")
                        return "offline"
                    plugin_logger.warning(f"HTTP状态码: {resp.status}")
                    return "error"
        except asyncio.TimeoutError:
            plugin_logger.warning("API 请求超时")
            return "error"
        except aiohttp.ClientError as e:
            plugin_logger.warning(f"API 连接失败: {e}")
            return "error"
        except Exception as e:
            plugin_logger.error(f"检查状态异常: {e}")
            return "error"
    
    async def _recover_napcat(self):
        """恢复 NapCat"""
        self._last_restart_time = datetime.now()
        plugin_logger.info("=" * 50)
        plugin_logger.info(" 开始恢复 NapCat")
        plugin_logger.info("=" * 50)
        
        try:
            # 步骤1
            plugin_logger.info("[1/4] 终止 QQ 进程...")
            subprocess.run(["pkill", "-f", "qq"], stderr=subprocess.DEVNULL)
            await asyncio.sleep(2)
            subprocess.run(["pkill", "-9", "-f", "QQ"], stderr=subprocess.DEVNULL)
            await asyncio.sleep(1)
            plugin_logger.info("[1/4] ✓ 进程已终止")
            
            # 步骤2
            plugin_logger.info("[2/4] 清理残留状态...")
            await self._clear_login_state()
            plugin_logger.info("[2/4] ✓ 状态已清理")
            
            # 步骤3
            plugin_logger.info("[3/4] 启动 NapCat...")
            await self._start_napcat()
            plugin_logger.info("[3/4] ✓ 启动命令已执行")
            
            # 步骤4
            plugin_logger.info("[4/4] 等待 NapCat 启动 (15秒)...")
            await asyncio.sleep(15)
            
            # 验证
            for i in range(3):
                status = await self._check_napcat_status()
                if status == "online":
                    plugin_logger.info("=" * 50)
                    plugin_logger.info(" ✓ NapCat 恢复成功!")
                    plugin_logger.info("=" * 50)
                    return
                plugin_logger.info(f"[4/4] 等待验证... ({i+1}/3)")
                await asyncio.sleep(5)
            
            plugin_logger.warning(f"NapCat 恢复后状态: {status}")
                
        except Exception as e:
            plugin_logger.error(f"恢复失败: {e}")
    
    async def _clear_login_state(self):
        """清理登录状态"""
        try:
            napcat_data_dir = os.path.join(self.napcat_dir, "app", ".config", "QQ")
            if os.path.exists(napcat_data_dir):
                plugin_logger.info(f"清理目录: {napcat_data_dir}")
        except Exception as e:
            plugin_logger.warning(f"清理状态失败: {e}")
    
    async def _start_napcat(self):
        """启动 NapCat"""
        try:
            if os.path.exists(self.launcher_script):
                subprocess.Popen(
                    [self.launcher_script],
                    cwd=self.napcat_dir,
                    stdout=open("/root/AstrBot/napcat_restart.log", "w"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True
                )
                plugin_logger.info(f"使用启动脚本: {self.launcher_script}")
            else:
                subprocess.Popen(
                    ["bash", "-c", f"cd {self.napcat_dir} && ./launcher.sh"],
                    stdout=open("/root/AstrBot/napcat_restart.log", "w"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True
                )
                plugin_logger.info(f"使用备选启动: {self.napcat_dir}/launcher.sh")
        except Exception as e:
            plugin_logger.error(f"启动 NapCat 失败: {e}")
    
    async def terminate(self):
        """插件卸载"""
        plugin_logger.info("插件卸载，停止监控...")
        self._is_monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
    
    # ==================== 指令接口 ====================
    
    @filter.command("napcat_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 NapCat 当前状态"""
        try:
            status = await self._check_napcat_status()
            status_text = {
                "online": "🟢 在线",
                "offline": "🔴 掉线",
                "error": "⚠️ 连接错误"
            }.get(status, "❓ 未知状态")
            
            login_info_text = ""
            if self._login_info:
                login_info_text = f"\n📱 登录: {self._login_info.get('nickname', '未知')} ({self._login_info.get('user_id', 'N/A')})"
            
            restart_info = ""
            if self._last_restart_time:
                restart_info = f"\n🔄 上次恢复: {self._last_restart_time.strftime('%Y-%m-%d %H:%M:%S')}"
            
            message = (
                f"📊 NapCat 状态监控\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🔗 地址: {self.napcat_url}\n"
                f"📋 状态: {status_text}\n"
                f"⏱️ 检查间隔: {self.check_interval}秒\n"
                f"📊 已检测: {self._check_count} 次\n"
                f"⚠️ 连续失败: {self._consecutive_failures}/{self.max_retries}\n"
                f"🔧 自动恢复: {'启用' if self.enable_auto_restart else '禁用'}"
                f"{login_info_text}"
                f"{restart_info}\n"
                f"📁 日志文件: /root/AstrBot/logs/napcat_keeper.log"
            )
            yield event.plain_result(message)
            
        except Exception as e:
            yield event.plain_result(f"检查状态失败: {e}")
    
    @filter.command("napcat_recover")
    async def cmd_recover(self, event: AstrMessageEvent):
        """手动恢复 NapCat"""
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
        """查看插件帮助"""
        help_text = (
            "🔧 NapCat Keeper 保活插件\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "/napcat_status - 查看 NapCat 当前状态\n"
            "/napcat_recover - 手动恢复 NapCat\n"
            "/napcat_logs - 查看最近日志\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 功能:\n"
            "• 每分钟自动检测登录状态\n"
            "• 掉线时自动重启恢复\n"
            "• 日志文件: /root/AstrBot/logs/napcat_keeper.log"
        )
        yield event.plain_result(help_text)
