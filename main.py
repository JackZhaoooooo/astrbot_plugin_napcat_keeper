"""
NapCat QQ 保活插件
自动检测 NapCat 登录状态，掉线时自动重启恢复
"""

import asyncio
import aiohttp
import subprocess
import os
from datetime import datetime
from pathlib import Path

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
        
        # 默认配置
        self.napcat_url = config.get("napcat_url", "http://localhost:6099")
        self.check_interval = config.get("check_interval", 60)  # 检查间隔（秒）
        self.max_retries = config.get("max_retries", 3)  # 连续失败次数阈值
        self.napcat_dir = config.get("napcat_dir", "/root/AstrBot/napcat")
        self.launcher_script = config.get("launcher_script", "/root/AstrBot/napcat/launcher.sh")
        self.enable_auto_restart = config.get("enable_auto_restart", True)
        self.notify_on_restart = config.get("notify_on_restart", True)
        
        # 状态变量
        self._consecutive_failures = 0
        self._is_monitoring = False
        self._monitor_task = None
        self._last_restart_time = None
        
        logger.info(f"[NapcatKeeper] 插件初始化完成")
        logger.info(f"[NapcatKeeper] NapCat URL: {self.napcat_url}")
        logger.info(f"[NapcatKeeper] 检查间隔: {self.check_interval}秒")
        logger.info(f"[NapcatKeeper] 自动重启: {'启用' if self.enable_auto_restart else '禁用'}")
    
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后启动监控"""
        logger.info("[NapcatKeeper] AstrBot 加载完成，启动 NapCat 保活监控...")
        self._is_monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
    
    async def _monitor_loop(self):
        """主监控循环"""
        while self._is_monitoring:
            try:
                # 检查 NapCat 状态
                status = await self._check_napcat_status()
                
                if status == "online":
                    if self._consecutive_failures > 0:
                        logger.info(f"[NapcatKeeper] NapCat 状态恢复正常 (已连续失败 {self._consecutive_failures} 次后恢复)")
                    self._consecutive_failures = 0
                    
                elif status == "offline":
                    self._consecutive_failures += 1
                    logger.warning(f"[NapcatKeeper] NapCat 登录失效 (第 {self._consecutive_failures} 次)")
                    
                    if self.enable_auto_restart and self._consecutive_failures >= self.max_retries:
                        logger.error(f"[NapcatKeeper] 连续失败达到阈值 ({self.max_retries})，执行重启...")
                        await self._restart_napcat()
                        self._consecutive_failures = 0
                
                elif status == "error":
                    self._consecutive_failures += 1
                    logger.error(f"[NapcatKeeper] 无法连接 NapCat (第 {self._consecutive_failures} 次)")
                    
                    if self.enable_auto_restart and self._consecutive_failures >= self.max_retries:
                        logger.error(f"[NapcatKeeper] 连续失败达到阈值，执行重启...")
                        await self._restart_napcat()
                        self._consecutive_failures = 0
                        
            except Exception as e:
                logger.error(f"[NapcatKeeper] 监控循环异常: {e}")
            
            await asyncio.sleep(self.check_interval)
    
    async def _check_napcat_status(self) -> str:
        """
        检查 NapCat 登录状态
        返回: 'online' | 'offline' | 'error'
        """
        try:
            async with aiohttp.ClientSession() as session:
                # 尝试获取登录状态
                async with session.get(
                    f"{self.napcat_url}/api/status/auth",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # 根据 NapCat API 返回判断状态
                        if data.get("code") == 0 or data.get("isLoggedIn") == True:
                            return "online"
                        else:
                            return "offline"
                    else:
                        return "error"
        except asyncio.TimeoutError:
            logger.warning("[NapcatKeeper] NapCat API 请求超时")
            return "error"
        except aiohttp.ClientError as e:
            logger.warning(f"[NapcatKeeper] NapCat API 连接失败: {e}")
            return "error"
        except Exception as e:
            logger.error(f"[NapcatKeeper] 检查状态异常: {e}")
            return "error"
    
    async def _restart_napcat(self):
        """重启 NapCat"""
        logger.info("[NapcatKeeper] 正在重启 NapCat...")
        self._last_restart_time = datetime.now()
        
        try:
            # 1. 杀掉 QQ 进程
            logger.info("[NapcatKeeper] 1/3 终止 QQ 进程...")
            subprocess.run(
                ["pkill", "-f", "qq"],
                stderr=subprocess.DEVNULL
            )
            await asyncio.sleep(2)
            
            # 2. 清理可能的残留进程
            subprocess.run(
                ["pkill", "-9", "-f", "QQ"],
                stderr=subprocess.DEVNULL
            )
            await asyncio.sleep(1)
            
            # 3. 启动新进程
            logger.info("[NapcatKeeper] 2/3 启动 NapCat...")
            
            # 检查启动脚本是否存在
            if os.path.exists(self.launcher_script):
                subprocess.Popen(
                    [self.launcher_script],
                    cwd=self.napcat_dir,
                    stdout=open("/root/AstrBot/napcat_restart.log", "w"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True
                )
            else:
                # 备选方案：直接启动
                logger.warning("[NapcatKeeper] 启动脚本不存在，使用备选方案...")
                subprocess.Popen(
                    ["bash", "-c", 
                     f"cd {self.napcat_dir} && ./launcher.sh"],
                    stdout=open("/root/AstrBot/napcat_restart.log", "w"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True
                )
            
            logger.info("[NapcatKeeper] 3/3 NapCat 重启命令已执行")
            logger.info("[NapcatKeeper] 等待 NapCat 启动 (10秒)...")
            await asyncio.sleep(10)
            
            # 验证重启是否成功
            status = await self._check_napcat_status()
            if status == "online":
                logger.info("[NapcatKeeper] ✓ NapCat 重启成功!")
                if self.notify_on_restart:
                    await self._send_notification("✅ NapCat 已自动重启恢复！")
            else:
                logger.warning(f"[NapcatKeeper] NapCat 重启后状态: {status}，将在下次检查时继续监控")
                if self.notify_on_restart:
                    await self._send_notification(f"⚠️ NapCat 重启后状态: {status}")
                    
        except Exception as e:
            logger.error(f"[NapcatKeeper] 重启失败: {e}")
    
    async def _send_notification(self, message: str):
        """发送通知给管理员"""
        try:
            # 获取所有支持的 session 发送通知
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                try:
                    # 尝试发送通知，这里简化处理
                    logger.info(f"[NapcatKeeper] 通知: {message}")
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[NapcatKeeper] 发送通知失败: {e}")
    
    async def terminate(self):
        """插件卸载时清理"""
        logger.info("[NapcatKeeper] 插件卸载，停止监控...")
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
            
            restart_info = ""
            if self._last_restart_time:
                restart_info = f"\n🔄 上次重启: {self._last_restart_time.strftime('%Y-%m-%d %H:%M:%S')}"
            
            message = (
                f"📊 NapCat 状态监控\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🔗 NapCat URL: {self.napcat_url}\n"
                f"📋 当前状态: {status_text}\n"
                f"⏱️ 检查间隔: {self.check_interval}秒\n"
                f"⚠️ 连续失败: {self._consecutive_failures}/{self.max_retries}\n"
                f"🔧 自动重启: {'启用' if self.enable_auto_restart else '禁用'}\n"
                f"📁 启动目录: {self.napcat_dir}"
                f"{restart_info}"
            )
            yield event.plain_result(message)
            
        except Exception as e:
            yield event.plain_result(f"检查状态失败: {e}")
    
    @filter.command("napcat_restart")
    async def cmd_restart(self, event: AstrMessageEvent):
        """手动重启 NapCat"""
        try:
            yield event.plain_result("🔄 正在重启 NapCat，请稍候...")
            await self._restart_napcat()
            status = await self._check_napcat_status()
            
            if status == "online":
                yield event.plain_result("✅ NapCat 重启成功！")
            else:
                yield event.plain_result(f"⚠️ NapCat 已重启，当前状态: {status}")
                
        except Exception as e:
            yield event.plain_result(f"❌ 重启失败: {e}")
    
    @filter.command("napcat_keeper_help")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看插件帮助"""
        help_text = (
            "🔧 NapCat Keeper 保活插件\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "/napcat_status - 查看 NapCat 当前状态\n"
            "/napcat_restart - 手动重启 NapCat\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 功能说明:\n"
            "• 自动检测 NapCat 登录状态\n"
            "• 掉线时自动重启恢复\n"
            "• 可配置检查间隔和重试次数"
        )
        yield event.plain_result(help_text)
