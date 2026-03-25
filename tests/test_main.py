import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


class DummyLogger:
    def __init__(self):
        self.records = []

    def _record(self, level, message, **kwargs):
        self.records.append((level, message, kwargs))

    def info(self, message, **kwargs):
        self._record("INFO", message, **kwargs)

    def warning(self, message, **kwargs):
        self._record("WARNING", message, **kwargs)

    def error(self, message, **kwargs):
        self._record("ERROR", message, **kwargs)

    def debug(self, message, **kwargs):
        self._record("DEBUG", message, **kwargs)


class DummyFilter:
    def on_astrbot_loaded(self):
        return lambda func: func

    def command(self, _name):
        return lambda func: func


class DummyStar:
    def __init__(self, context):
        self.context = context


class DummyEvent:
    def plain_result(self, message):
        return message


class DummyPlain:
    def __init__(self, text):
        self.text = text


class DummyMessageChain:
    def __init__(self):
        self.chain = []

    def message(self, message):
        self.chain.append(DummyPlain(message))
        return self

    def get_plain_text(self):
        return " ".join(item.text for item in self.chain)


class DummyPlatformMeta:
    def __init__(self, *, id, name, description=""):
        self.id = id
        self.name = name
        self.description = description


class DummyPlatform:
    def __init__(self, *, id, name, description=""):
        self._meta = DummyPlatformMeta(id=id, name=name, description=description)

    def meta(self):
        return self._meta


def install_astrbot_stubs():
    logger = DummyLogger()

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")

    api_module.AstrBotConfig = dict
    api_module.logger = logger
    event_module.AstrMessageEvent = DummyEvent
    event_module.MessageChain = DummyMessageChain
    event_module.filter = DummyFilter()
    star_module.Context = object
    star_module.Star = DummyStar

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module

    return logger


def load_plugin_module():
    sys.modules.pop("main", None)
    logger = install_astrbot_stubs()
    module = importlib.import_module("main")
    return module, logger


class NapcatKeeperPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module, self.platform_logger = load_plugin_module()

    def make_plugin(self, config=None, context=None):
        if context is None:
            context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        with patch.object(self.module, "_get_file_logger", return_value=DummyLogger()):
            plugin = self.module.NapcatKeeperPlugin(context, config or {})
        return plugin

    def make_snapshot(
        self,
        overall_status,
        *,
        service_state="online",
        service_detail="HTTP 301，NapCat 服务可达。",
        service_url="http://localhost:6099",
        login_state=None,
        login_detail=None,
        user_id=None,
        nickname=None,
        endpoint="http://localhost:6099/get_login_info",
    ):
        login = None
        if login_state is not None:
            login = self.module.LoginCheckResult(
                state=login_state,
                endpoint=endpoint,
                detail=login_detail or "默认登录说明",
                user_id=user_id,
                nickname=nickname,
            )
        return self.module.StatusSnapshot(
            overall_status=overall_status,
            service=self.module.ServiceCheckResult(
                state=service_state,
                checked_url=service_url,
                status_code=301 if service_state == "online" else None,
                detail=service_detail,
            ),
            login=login,
        )

    def test_extract_login_identity_from_payload(self):
        user_id, nickname = self.module.NapcatKeeperPlugin._extract_login_identity(
            {
                "status": "ok",
                "data": {
                    "user_id": 123456789,
                    "nickname": "NapCatBot",
                },
            }
        )

        self.assertEqual(user_id, "123456789")
        self.assertEqual(nickname, "NapCatBot")

    def test_build_login_result_marks_not_logged_in_when_account_missing(self):
        result = self.module.NapcatKeeperPlugin._build_login_result(
            "http://localhost:6099/get_login_info",
            status_code=200,
            payload={"status": "ok", "data": {}},
        )

        self.assertEqual(result.state, "not_logged_in")
        self.assertIn("未返回已登录 QQ 账号信息", result.detail)

    def test_build_webui_api_url_and_hash_helpers_match_napcat_contract(self):
        plugin = self.make_plugin({"napcat_url": "http://localhost:6099/webui"})

        self.assertEqual(
            plugin._build_webui_api_url("/QQLogin/CheckLoginStatus"),
            "http://localhost:6099/api/QQLogin/CheckLoginStatus",
        )
        self.assertEqual(
            plugin._hash_webui_token("token123"),
            "0f2873cd6b724fe68cb5fd057fbb8bd5a0b09ed4b5a9ea15945bfed88fc58de1",
        )
        self.assertEqual(
            plugin._password_md5("pass123"),
            "32250170a0dca92d53ec9624f336ca24",
        )

    async def test_request_webui_credential_reuses_cached_credential(self):
        plugin = self.make_plugin({"napcat_token": "token123"})
        plugin._post_json_request = AsyncMock(
            return_value=(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": {"Credential": "credential-123"},
                },
                None,
            )
        )

        first = await plugin._request_webui_credential(session=object())
        second = await plugin._request_webui_credential(session=object())

        self.assertEqual(first, "credential-123")
        self.assertEqual(second, "credential-123")
        self.assertEqual(plugin._post_json_request.await_count, 1)

    async def test_request_webui_credential_reuses_cached_value_when_auth_rate_limited(self):
        plugin = self.make_plugin({"napcat_token": "token123"})
        plugin._webui_credential = "cached-credential"
        plugin._webui_credential_cached_at = 0.0
        plugin._post_json_request = AsyncMock(
            return_value=(
                200,
                {
                    "code": 1,
                    "message": "login rate limit",
                    "data": None,
                },
                None,
            )
        )
        plugin._log = lambda *args, **kwargs: None

        with patch.object(
            self.module.time,
            "monotonic",
            return_value=self.module.WEBUI_CREDENTIAL_CACHE_TTL_SECONDS + 1000,
        ):
            credential = await plugin._request_webui_credential(session=object())

        self.assertEqual(credential, "cached-credential")
        self.assertEqual(plugin._post_json_request.await_count, 1)

    def test_emit_snapshot_logs_are_clear(self):
        plugin = self.make_plugin()
        plugin._check_count = 7
        snapshot = self.module.StatusSnapshot(
            overall_status="offline",
            service=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=301,
                detail="HTTP 301，NapCat 服务可达。",
            ),
            login=self.module.LoginCheckResult(
                state="not_logged_in",
                endpoint="http://localhost:6099/get_login_info",
                detail="接口已响应，但未返回已登录 QQ 账号信息。",
            ),
        )

        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message, kwargs)
        )

        plugin._emit_snapshot_logs("2026-03-24 18:00:00", snapshot)

        self.assertEqual(len(captured), 3)
        self.assertEqual(captured[0][0], "INFO")
        self.assertIn("NapCat 服务检测", captured[0][1])
        self.assertNotIn("第 7 次检查 -", captured[0][1])
        self.assertIn("QQ 登录检测", captured[1][1])
        self.assertEqual(captured[1][0], "WARNING")
        self.assertNotIn("第 7 次检查 -", captured[1][1])
        self.assertIn("综合判定", captured[2][1])
        self.assertEqual(captured[2][0], "WARNING")
        self.assertNotIn("第 7 次检查 -", captured[2][1])

    def test_emit_snapshot_logs_skips_online_polling_when_debug_disabled(self):
        plugin = self.make_plugin()
        snapshot = self.module.StatusSnapshot(
            overall_status="online",
            service=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=200,
                detail="HTTP 200，NapCat 服务可达。",
            ),
            login=self.module.LoginCheckResult(
                state="logged_in",
                endpoint="http://localhost:6099/api/QQLogin/CheckLoginStatus",
                detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
                user_id="123456789",
                nickname="NapCatBot",
            ),
        )

        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message, kwargs)
        )

        plugin._emit_snapshot_logs("2026-03-24 18:00:00", snapshot)

        self.assertEqual(captured, [])

    def test_emit_snapshot_logs_outputs_online_polling_when_debug_enabled(self):
        plugin = self.make_plugin({"debug": True})
        snapshot = self.module.StatusSnapshot(
            overall_status="online",
            service=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=200,
                detail="HTTP 200，NapCat 服务可达。",
            ),
            login=self.module.LoginCheckResult(
                state="logged_in",
                endpoint="http://localhost:6099/api/QQLogin/CheckLoginStatus",
                detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
                user_id="123456789",
                nickname="NapCatBot",
            ),
        )

        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message, kwargs)
        )

        plugin._emit_snapshot_logs("2026-03-24 18:00:00", snapshot)

        self.assertEqual(len(captured), 3)
        self.assertTrue(any("NapCat 服务检测" in item[1] for item in captured))
        self.assertTrue(any("QQ 登录检测" in item[1] for item in captured))
        self.assertTrue(any("综合判定" in item[1] for item in captured))

    def test_emit_snapshot_logs_force_outputs_online_polling_for_recovery(self):
        plugin = self.make_plugin()
        snapshot = self.module.StatusSnapshot(
            overall_status="online",
            service=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=200,
                detail="HTTP 200，NapCat 服务可达。",
            ),
            login=self.module.LoginCheckResult(
                state="logged_in",
                endpoint="http://localhost:6099/api/QQLogin/CheckLoginStatus",
                detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
                user_id="123456789",
                nickname="NapCatBot",
            ),
        )

        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message, kwargs)
        )

        plugin._emit_snapshot_logs("2026-03-24 18:00:00", snapshot, force=True)

        self.assertEqual(len(captured), 3)
        self.assertTrue(any("综合判定" in item[1] for item in captured))

    def test_normalize_umo_list_supports_multiple_formats_and_deduplicates(self):
        result = self.module.NapcatKeeperPlugin._normalize_umo_list(
            [
                "  umo-1  ",
                "umo-2, umo-3",
                "umo-3",
                "umo-4\numo-5",
                "umo-6，umo-7",
                "",
                None,
            ]
        )

        self.assertEqual(
            result,
            ["umo-1", "umo-2", "umo-3", "umo-4", "umo-5", "umo-6", "umo-7"],
        )

    async def test_send_notification_to_umos_attempts_qq_official_and_detects_success_by_outbound_anchor(
        self,
    ):
        platform = DummyPlatform(
            id="default",
            name="qq_official",
            description="QQ 机器人官方 API 适配器",
        )
        platform._session_last_inbound_message_id = {"session-1": "inbound-1"}
        platform._session_last_outbound_message_id = {}

        async def send_message_side_effect(umo, _message_chain):
            if umo == "default:FriendMessage:session-1":
                platform._session_last_outbound_message_id["session-1"] = "outbound-1"
            return True

        context = types.SimpleNamespace(
            send_message=AsyncMock(side_effect=send_message_side_effect),
            platform_manager=types.SimpleNamespace(platform_insts=[platform]),
        )
        plugin = self.make_plugin(context=context)
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message)
        )

        delivered = await plugin._send_notification_to_umos(
            ["default:FriendMessage:session-1"],
            "test message",
            "重新登录通知",
        )

        self.assertTrue(delivered)
        self.assertEqual(context.send_message.await_count, 1)
        self.assertTrue(
            any(
                level == "INFO" and "开始尝试发送" in message
                for level, message in captured
            )
        )
        self.assertTrue(
            any(
                level == "INFO"
                and "QQ 官方平台已生成新的出站消息锚点" in message
                for level, message in captured
            )
        )

    async def test_send_notification_to_umos_retries_after_invalid_msg_id_error(self):
        context = types.SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("请求参数msg_id无效或越权"))
        )
        plugin = self.make_plugin(context=context)
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message)
        )

        first = await plugin._send_notification_to_umos(
            ["default:FriendMessage:session-2"],
            "test message",
            "退出登录通知",
        )
        second = await plugin._send_notification_to_umos(
            ["default:FriendMessage:session-2"],
            "test message",
            "退出登录通知",
        )

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(context.send_message.await_count, 2)
        self.assertTrue(
            any(
                level == "WARNING"
                and "msg_id 已失效或无权限" in message
                and "将继续尝试后续通知目标" in message
                for level, message in captured
            )
        )
        self.assertFalse(any("已标记为不可主动通知" in message for _level, message in captured))

    async def test_handle_login_transition_notifications_sends_logout_to_multiple_umos(self):
        context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        plugin = self.make_plugin(
            {
                "logout_notify_umos": ["platform:1", " platform:2 ", "platform:1"],
            },
            context=context,
        )
        plugin._log = lambda *args, **kwargs: None

        previous_snapshot = self.make_snapshot(
            "online",
            login_state="logged_in",
            login_detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
            user_id="123456789",
            nickname="NapCatBot",
        )
        current_snapshot = self.make_snapshot(
            "offline",
            login_state="not_logged_in",
            login_detail="WebUI 检测到当前未登录 QQ。",
        )

        await plugin._handle_login_transition_notifications(
            previous_snapshot,
            current_snapshot,
            "2026-03-24 20:00:00",
        )

        self.assertEqual(context.send_message.await_count, 2)
        self.assertEqual(
            [call.args[0] for call in context.send_message.await_args_list],
            ["platform:1", "platform:2"],
        )
        message = context.send_message.await_args_list[0].args[1].get_plain_text()
        self.assertIn("NapCat Keeper 检测到 QQ 已退出登录", message)
        self.assertIn("NapCatBot (123456789)", message)
        self.assertIn("WebUI 检测到当前未登录 QQ。", message)

    async def test_handle_login_transition_notifications_sends_relogin_notice(self):
        context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        plugin = self.make_plugin(
            {
                "relogin_notify_umos": "platform:1,\nplatform:2",
            },
            context=context,
        )
        plugin._log = lambda *args, **kwargs: None

        previous_snapshot = self.make_snapshot(
            "error",
            service_state="error",
            service_detail="连接失败: connection refused",
        )
        current_snapshot = self.make_snapshot(
            "online",
            login_state="logged_in",
            login_detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
            user_id="123456789",
            nickname="NapCatBot",
        )

        await plugin._handle_login_transition_notifications(
            previous_snapshot,
            current_snapshot,
            "2026-03-24 20:01:00",
        )

        self.assertEqual(context.send_message.await_count, 2)
        message = context.send_message.await_args_list[0].args[1].get_plain_text()
        self.assertIn("NapCat Keeper 检测到 QQ 已重新登录", message)
        self.assertIn("NapCatBot (123456789)", message)
        self.assertIn("WebUI 检测到 QQ 已登录", message)

    async def test_handle_login_transition_notifications_warns_when_umo_delivery_fails(self):
        plugin = self.make_plugin()
        plugin._send_notification_to_umos = AsyncMock(return_value=False)
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message)
        )

        previous_snapshot = self.make_snapshot(
            "online",
            login_state="logged_in",
            login_detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
            user_id="123456789",
            nickname="NapCatBot",
        )
        current_snapshot = self.make_snapshot(
            "offline",
            login_state="not_logged_in",
            login_detail="WebUI 检测到当前未登录 QQ。",
        )

        await plugin._handle_login_transition_notifications(
            previous_snapshot,
            current_snapshot,
            "2026-03-24 22:30:00",
        )

        self.assertEqual(plugin._send_notification_to_umos.await_count, 1)
        self.assertTrue(
            any(
                level == "WARNING" and "退出登录通知未通过任何 UMO 发送成功。" in message
                for level, message in captured
            )
        )

    async def test_handle_login_transition_notifications_no_warning_when_umo_succeeds(self):
        plugin = self.make_plugin()
        plugin._send_notification_to_umos = AsyncMock(return_value=True)
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message)
        )

        previous_snapshot = self.make_snapshot(
            "offline",
            login_state="not_logged_in",
            login_detail="WebUI 检测到当前未登录 QQ。",
        )
        current_snapshot = self.make_snapshot(
            "online",
            login_state="logged_in",
            login_detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
            user_id="123456789",
            nickname="NapCatBot",
        )

        await plugin._handle_login_transition_notifications(
            previous_snapshot,
            current_snapshot,
            "2026-03-24 22:31:00",
        )

        self.assertEqual(plugin._send_notification_to_umos.await_count, 1)
        self.assertFalse(
            any("重新登录通知未通过任何 UMO 发送成功。" in message for _level, message in captured)
        )

    async def test_handle_login_transition_notifications_skips_first_snapshot(self):
        context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        plugin = self.make_plugin(
            {
                "logout_notify_umos": ["platform:1"],
                "relogin_notify_umos": ["platform:2"],
            },
            context=context,
        )
        plugin._log = lambda *args, **kwargs: None

        current_snapshot = self.make_snapshot(
            "online",
            login_state="logged_in",
            login_detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
            user_id="123456789",
            nickname="NapCatBot",
        )

        await plugin._handle_login_transition_notifications(
            None,
            current_snapshot,
            "2026-03-24 20:02:00",
        )

        self.assertEqual(context.send_message.await_count, 0)

    async def test_handle_login_transition_notifications_skips_unchanged_or_service_error_state(self):
        context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        plugin = self.make_plugin(
            {
                "logout_notify_umos": ["platform:1"],
                "relogin_notify_umos": ["platform:2"],
            },
            context=context,
        )
        plugin._log = lambda *args, **kwargs: None

        previous_logged_in = self.make_snapshot(
            "online",
            login_state="logged_in",
            login_detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
            user_id="123456789",
            nickname="NapCatBot",
        )
        current_logged_in = self.make_snapshot(
            "online",
            login_state="logged_in",
            login_detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
            user_id="123456789",
            nickname="NapCatBot",
        )
        current_service_error = self.make_snapshot(
            "error",
            service_state="error",
            service_detail="连接失败: connection refused",
        )

        await plugin._handle_login_transition_notifications(
            previous_logged_in,
            current_logged_in,
            "2026-03-24 20:03:00",
        )
        await plugin._handle_login_transition_notifications(
            previous_logged_in,
            current_service_error,
            "2026-03-24 20:04:00",
        )

        self.assertEqual(context.send_message.await_count, 0)

    async def test_initialize_starts_monitor_task_without_waiting_for_astrbot_loaded(self):
        plugin = self.make_plugin()
        gate = asyncio.Event()

        async def fake_monitor_loop():
            await gate.wait()

        plugin._monitor_loop = fake_monitor_loop
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message, kwargs)
        )

        await plugin.initialize()

        self.assertTrue(plugin._is_monitoring)
        self.assertIsNotNone(plugin._monitor_task)
        self.assertFalse(plugin._monitor_task.done())
        self.assertTrue(any("initialize()" in item[1] for item in captured))

        await plugin.terminate()

    async def test_on_astrbot_loaded_does_not_duplicate_monitor_task_after_initialize(self):
        plugin = self.make_plugin()
        gate = asyncio.Event()

        async def fake_monitor_loop():
            await gate.wait()

        plugin._monitor_loop = fake_monitor_loop
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message, kwargs)
        )

        await plugin.initialize()
        first_task = plugin._monitor_task
        await plugin.on_astrbot_loaded()

        self.assertIs(plugin._monitor_task, first_task)
        self.assertTrue(
            any("重复启动" in item[1] and item[0] == "DEBUG" for item in captured)
        )

        await plugin.terminate()

    async def test_collect_status_snapshot_online_sets_login_info(self):
        plugin = self.make_plugin()
        plugin._check_napcat_service_status = AsyncMock(
            return_value=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=301,
                detail="HTTP 301，NapCat 服务可达。",
            )
        )
        plugin._check_qq_login_status = AsyncMock(
            return_value=self.module.LoginCheckResult(
                state="logged_in",
                endpoint="http://localhost:6099/get_login_info",
                detail="已获取登录账号信息: NapCatBot (123456789)。",
                user_id="123456789",
                nickname="NapCatBot",
            )
        )

        snapshot = await plugin._collect_status_snapshot()

        self.assertEqual(snapshot.overall_status, "online")
        self.assertEqual(plugin._login_info["user_id"], "123456789")
        self.assertEqual(plugin._login_info["nickname"], "NapCatBot")

    async def test_collect_status_snapshot_offline_clears_login_info(self):
        plugin = self.make_plugin()
        plugin._login_info = {"user_id": "old", "nickname": "OldBot"}
        plugin._check_napcat_service_status = AsyncMock(
            return_value=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=200,
                detail="HTTP 200，NapCat 服务可达。",
            )
        )
        plugin._check_qq_login_status = AsyncMock(
            return_value=self.module.LoginCheckResult(
                state="not_logged_in",
                endpoint="http://localhost:6099/get_login_info",
                detail="接口已响应，但未返回已登录 QQ 账号信息。",
            )
        )

        snapshot = await plugin._collect_status_snapshot()

        self.assertEqual(snapshot.overall_status, "offline")
        self.assertIsNone(plugin._login_info)

    async def test_check_qq_login_status_via_webui_reports_logged_in_account(self):
        plugin = self.make_plugin({"napcat_token": "token123"})
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")
        plugin._call_webui_api = AsyncMock(
            side_effect=[
                (
                    plugin._build_webui_api_url("/QQLogin/CheckLoginStatus"),
                    200,
                    {
                        "code": 0,
                        "message": "success",
                        "data": {
                            "isLogin": True,
                            "isOffline": False,
                            "loginError": "",
                        },
                    },
                    None,
                ),
                (
                    plugin._build_webui_api_url("/QQLogin/GetQQLoginInfo"),
                    200,
                    {
                        "code": 0,
                        "message": "success",
                        "data": {
                            "uin": 123456789,
                            "nickname": "NapCatBot",
                        },
                    },
                    None,
                ),
            ]
        )

        result = await plugin._check_qq_login_status_via_webui()

        self.assertEqual(result.state, "logged_in")
        self.assertEqual(result.user_id, "123456789")
        self.assertEqual(result.nickname, "NapCatBot")
        self.assertIn("WebUI 检测到 QQ 已登录", result.detail)

    async def test_check_qq_login_status_via_webui_treats_duplicate_login_message_as_not_logged_in(self):
        plugin = self.make_plugin(
            {
                "napcat_token": "token123",
                "qq_account": "3412404961",
            }
        )
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")
        plugin._call_webui_api = AsyncMock(
            return_value=(
                plugin._build_webui_api_url("/QQLogin/CheckLoginStatus"),
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": {
                        "isLogin": False,
                        "isOffline": False,
                        "loginError": "当前账号(3412404961)已登录,无法重复登录",
                    },
                },
                None,
            )
        )

        result = await plugin._check_qq_login_status_via_webui()

        self.assertEqual(result.state, "not_logged_in")
        self.assertEqual(result.user_id, "3412404961")
        self.assertIn("当前账号(3412404961)已登录,无法重复登录", result.detail)

    async def test_check_qq_login_status_falls_back_to_legacy_when_webui_fails(self):
        plugin = self.make_plugin()
        plugin._check_qq_login_status_via_webui = AsyncMock(
            return_value=self.module.LoginCheckResult(
                state="error",
                endpoint="http://localhost:6099/api/QQLogin/CheckLoginStatus",
                detail="WebUI 检测失败",
            )
        )
        plugin._check_qq_login_status_via_onebot = AsyncMock(
            return_value=self.module.LoginCheckResult(
                state="logged_in",
                endpoint="http://localhost:6099/get_login_info",
                detail="已获取登录账号信息: NapCatBot (123456789)。",
                user_id="123456789",
                nickname="NapCatBot",
            )
        )

        result = await plugin._check_qq_login_status()

        self.assertEqual(result.state, "logged_in")
        self.assertEqual(result.endpoint, "http://localhost:6099/get_login_info")
        self.assertEqual(result.user_id, "123456789")

    async def test_check_qq_login_status_combines_dual_failures(self):
        plugin = self.make_plugin()
        plugin._check_qq_login_status_via_webui = AsyncMock(
            return_value=self.module.LoginCheckResult(
                state="error",
                endpoint="webui-endpoint",
                detail="WebUI 鉴权失败",
            )
        )
        plugin._check_qq_login_status_via_onebot = AsyncMock(
            return_value=self.module.LoginCheckResult(
                state="error",
                endpoint="onebot-endpoint",
                detail="get_login_info 404",
            )
        )

        result = await plugin._check_qq_login_status()

        self.assertEqual(result.state, "error")
        self.assertIn("NapCat WebUI 检测失败", result.detail)
        self.assertIn("备用 get_login_info 检测也失败", result.detail)

    async def test_password_login_by_account_uses_md5_payload(self):
        plugin = self.make_plugin()
        plugin._call_webui_api = AsyncMock(
            return_value=(
                "http://localhost:6099/api/QQLogin/PasswordLogin",
                200,
                {"code": 0, "message": "success", "data": None},
                None,
            )
        )

        success, detail = await plugin._password_login_by_account(
            object(),
            "credential-123",
            "123456789",
            "pass123",
        )

        self.assertTrue(success)
        self.assertIn("提交密码登录请求", detail)
        self.assertEqual(
            plugin._call_webui_api.await_args.kwargs["payload"],
            {
                "uin": "123456789",
                "passwordMd5": "32250170a0dca92d53ec9624f336ca24",
            },
        )

    async def test_password_login_by_account_treats_duplicate_login_conflict_as_failure(self):
        plugin = self.make_plugin()
        plugin._call_webui_api = AsyncMock(
            return_value=(
                "http://localhost:6099/api/QQLogin/PasswordLogin",
                200,
                {
                    "code": 1,
                    "message": "当前账号(3412404961)已登录,无法重复登录",
                    "data": None,
                },
                None,
            )
        )

        success, detail = await plugin._password_login_by_account(
            object(),
            "credential-123",
            "3412404961",
            "pass123",
        )

        self.assertFalse(success)
        self.assertIn("NapCat 端尚未完成登录", detail)
        self.assertIn("无法重复登录", detail)

    async def test_quick_login_by_account_sets_account_then_triggers_login(self):
        plugin = self.make_plugin()
        plugin._call_webui_api = AsyncMock(
            side_effect=[
                (
                    "http://localhost:6099/api/QQLogin/SetQuickLoginQQ",
                    200,
                    {"code": 0, "message": "success", "data": None},
                    None,
                ),
                (
                    "http://localhost:6099/api/QQLogin/SetQuickLogin",
                    200,
                    {"code": 0, "message": "success", "data": None},
                    None,
                ),
            ]
        )

        success, detail = await plugin._quick_login_by_account(
            object(),
            "credential-123",
            "123456789",
        )

        self.assertTrue(success)
        self.assertIn("提交快速登录请求", detail)
        self.assertEqual(plugin._call_webui_api.await_args_list[0].args[1], "/QQLogin/SetQuickLoginQQ")
        self.assertEqual(plugin._call_webui_api.await_args_list[1].args[1], "/QQLogin/SetQuickLogin")

    async def test_quick_login_by_account_treats_duplicate_login_conflict_as_failure(self):
        plugin = self.make_plugin()
        plugin._call_webui_api = AsyncMock(
            side_effect=[
                (
                    "http://localhost:6099/api/QQLogin/SetQuickLoginQQ",
                    200,
                    {"code": 0, "message": "success", "data": None},
                    None,
                ),
                (
                    "http://localhost:6099/api/QQLogin/SetQuickLogin",
                    200,
                    {
                        "code": 1,
                        "message": "当前账号(3412404961)已登录,无法重复登录",
                        "data": None,
                    },
                    None,
                ),
            ]
        )

        success, detail = await plugin._quick_login_by_account(
            object(),
            "credential-123",
            "3412404961",
        )

        self.assertFalse(success)
        self.assertIn("NapCat 端尚未完成登录", detail)
        self.assertIn("无法重复登录", detail)

    async def test_auto_login_qq_uses_password_login_when_password_configured(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
                "napcat_token": "token123",
                "qq_password": "pass123",
            }
        )
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")
        plugin._password_login_by_account = AsyncMock(
            return_value=(True, "已为 QQ 123456789 提交密码登录请求。")
        )
        plugin._prepare_manual_login_assistance = AsyncMock()
        plugin._log = lambda *args, **kwargs: None

        result = await plugin._auto_login_qq()

        self.assertTrue(result.submitted)
        self.assertFalse(result.manual_action_required)
        self.assertEqual(result.level, "INFO")
        self.assertIn("QQ 普通登录已提交", result.detail)
        self.assertEqual(plugin._request_webui_credential.await_count, 1)
        self.assertEqual(plugin._password_login_by_account.await_count, 1)
        self.assertEqual(plugin._prepare_manual_login_assistance.await_count, 0)
        self.assertEqual(
            plugin._password_login_by_account.await_args.args[2:],
            ("123456789", "pass123"),
        )

    async def test_auto_login_qq_switches_to_qrcode_when_password_login_requires_scan(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
                "napcat_token": "token123",
                "qq_password": "pass123",
            }
        )
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")
        plugin._password_login_by_account = AsyncMock(
            return_value=(
                False,
                "QQ 密码登录触发验证码校验，无法直接完成普通登录，需要切换为二维码登录。",
            )
        )

        async def prepare_side_effect(*_args, **_kwargs):
            plugin._manual_login_pending_context = {
                "account": "123456789",
                "qrcode_url": "https://txz.qq.com/p?k=abc",
                "qrcode_expires_at": 9999999999.0,
            }
            return (
                "QQ 普通登录未能直接完成，已切换为二维码登录。"
                " | 原因: QQ 密码登录触发验证码校验，无法直接完成普通登录，需要切换为二维码登录。"
                " | 已生成 QQ 登录二维码（2 分内有效）: https://txz.qq.com/p?k=abc"
            )

        plugin._prepare_manual_login_assistance = AsyncMock(side_effect=prepare_side_effect)
        plugin._log = lambda *args, **kwargs: None

        result = await plugin._auto_login_qq()

        self.assertFalse(result.submitted)
        self.assertTrue(result.manual_action_required)
        self.assertEqual(result.level, "WARNING")
        self.assertIn("QQ 普通登录未能直接完成", result.detail)
        self.assertIn("https://txz.qq.com/p?k=abc", result.detail)
        self.assertEqual(plugin._password_login_by_account.await_count, 1)
        self.assertEqual(plugin._prepare_manual_login_assistance.await_args.args[2], "123456789")

    async def test_auto_login_qq_uses_qrcode_without_password(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
            }
        )
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")

        async def prepare_side_effect(*_args, **_kwargs):
            plugin._manual_login_pending_context = {
                "account": "123456789",
                "qrcode_url": "https://txz.qq.com/p?k=guest",
                "qrcode_expires_at": 9999999999.0,
            }
            return (
                "未配置 qq_password，无法执行账号密码登录，已直接切换为二维码登录。"
                " | 已生成 QQ 登录二维码（2 分内有效）: https://txz.qq.com/p?k=guest"
            )

        plugin._prepare_manual_login_assistance = AsyncMock(side_effect=prepare_side_effect)
        plugin._log = lambda *args, **kwargs: None

        result = await plugin._auto_login_qq()

        self.assertFalse(result.submitted)
        self.assertTrue(result.manual_action_required)
        self.assertEqual(result.level, "WARNING")
        self.assertIn("未配置 qq_password", result.detail)
        self.assertIn("https://txz.qq.com/p?k=guest", result.detail)

    async def test_auto_login_qq_returns_error_when_qrcode_generation_fails(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
            }
        )
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")
        plugin._prepare_manual_login_assistance = AsyncMock(
            return_value="未配置 qq_password，无法执行账号密码登录，已直接切换为二维码登录。 | 生成 QQ 登录二维码失败: 接口超时。"
        )
        plugin._log = lambda *args, **kwargs: None

        result = await plugin._auto_login_qq()

        self.assertFalse(result.submitted)
        self.assertFalse(result.manual_action_required)
        self.assertEqual(result.level, "ERROR")
        self.assertIn("生成 QQ 登录二维码失败", result.detail)

    async def test_wait_for_napcat_service_ready_returns_early_when_service_becomes_online(self):
        plugin = self.make_plugin()
        plugin._check_napcat_service_status = AsyncMock(
            side_effect=[
                self.module.ServiceCheckResult(
                    state="error",
                    checked_url="http://localhost:6099",
                    status_code=None,
                    detail="连接失败: connection refused",
                ),
                self.module.ServiceCheckResult(
                    state="error",
                    checked_url="http://localhost:6099",
                    status_code=None,
                    detail="连接失败: connection refused",
                ),
                self.module.ServiceCheckResult(
                    state="online",
                    checked_url="http://localhost:6099",
                    status_code=301,
                    detail="HTTP 301，NapCat 服务可达。",
                ),
            ]
        )

        with patch.object(self.module.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
            result = await plugin._wait_for_napcat_service_ready(
                timeout_seconds=5,
                poll_interval_seconds=1,
            )

        self.assertEqual(result.state, "online")
        self.assertEqual(plugin._check_napcat_service_status.await_count, 3)
        self.assertEqual(sleep_mock.await_count, 2)

    async def test_recover_napcat_triggers_auto_login_before_verification(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
            }
        )
        order = []

        async def auto_login():
            order.append("auto_login")
            return self.module.AutoLoginAttemptResult(
                submitted=True,
                detail="已为 QQ 123456789 提交快速登录请求。",
            )

        async def collect_snapshot():
            order.append("collect")
            return self.module.StatusSnapshot(
                overall_status="online",
                service=self.module.ServiceCheckResult(
                    state="online",
                    checked_url="http://localhost:6099",
                    status_code=301,
                    detail="HTTP 301，NapCat 服务可达。",
                ),
                login=self.module.LoginCheckResult(
                    state="logged_in",
                    endpoint="http://localhost:6099/api/QQLogin/CheckLoginStatus",
                    detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
                    user_id="123456789",
                    nickname="NapCatBot",
                ),
            )

        plugin._clear_login_state = AsyncMock()
        plugin._start_napcat = AsyncMock()
        plugin._wait_for_napcat_service_ready = AsyncMock(
            return_value=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=301,
                detail="HTTP 301，NapCat 服务可达。",
            )
        )
        plugin._auto_login_qq = AsyncMock(side_effect=auto_login)
        plugin._collect_status_snapshot = AsyncMock(side_effect=collect_snapshot)
        plugin._emit_snapshot_logs = lambda *args, **kwargs: order.append("emit")
        plugin._log = lambda *args, **kwargs: None

        with patch.object(self.module.subprocess, "run"), patch.object(
            self.module.asyncio,
            "sleep",
            new=AsyncMock(),
        ):
            await plugin._recover_napcat()

        self.assertIn("auto_login", order)
        self.assertIn("collect", order)
        self.assertLess(order.index("auto_login"), order.index("collect"))

    async def test_recover_for_snapshot_uses_login_only_recovery_when_service_is_online(self):
        plugin = self.make_plugin()
        snapshot = self.make_snapshot(
            "offline",
            login_state="not_logged_in",
            login_detail="WebUI 检测到当前未登录 QQ。",
        )
        plugin._recover_napcat = AsyncMock()
        plugin._recover_login_only = AsyncMock()
        plugin._log = lambda *args, **kwargs: None

        await plugin._recover_for_snapshot(snapshot)

        self.assertEqual(plugin._recover_login_only.await_count, 1)
        self.assertEqual(plugin._recover_napcat.await_count, 0)

    async def test_recover_for_snapshot_always_uses_full_recovery_when_service_is_abnormal(self):
        plugin = self.make_plugin()
        snapshot = self.make_snapshot(
            "error",
            service_state="error",
            service_detail="连接失败: connection refused",
        )
        plugin._recover_napcat = AsyncMock()
        plugin._recover_login_only = AsyncMock()
        plugin._log = lambda *args, **kwargs: None

        await plugin._recover_for_snapshot(snapshot)

        self.assertEqual(plugin._recover_napcat.await_count, 1)
        self.assertEqual(plugin._recover_login_only.await_count, 0)

    async def test_recover_login_only_triggers_auto_login_before_verification(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
            }
        )
        order = []
        offline_snapshot = self.make_snapshot(
            "offline",
            login_state="not_logged_in",
            login_detail="WebUI 检测到当前未登录 QQ。",
        )

        async def auto_login(**kwargs):
            order.append("auto_login")
            return self.module.AutoLoginAttemptResult(
                submitted=True,
                detail="已为 QQ 123456789 提交快速登录请求。",
            )

        async def verify_snapshot(**kwargs):
            order.append("collect")
            return self.make_snapshot(
                "online",
                login_state="logged_in",
                login_detail="WebUI 检测到 QQ 已登录: NapCatBot (123456789)。",
                user_id="123456789",
                nickname="NapCatBot",
                endpoint="http://localhost:6099/api/QQLogin/CheckLoginStatus",
            )

        plugin._auto_login_qq = AsyncMock(side_effect=auto_login)
        plugin._verify_recovery_status = AsyncMock(side_effect=verify_snapshot)
        plugin._log = lambda *args, **kwargs: None

        with patch.object(
            self.module.asyncio,
            "sleep",
            new=AsyncMock(),
        ):
            await plugin._recover_login_only(offline_snapshot)

        self.assertIn("auto_login", order)
        self.assertIn("collect", order)
        self.assertLess(order.index("auto_login"), order.index("collect"))

    async def test_recover_login_only_keeps_manual_pending_during_retry(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
            }
        )
        offline_snapshot = self.make_snapshot(
            "offline",
            login_state="not_logged_in",
            login_detail="WebUI 检测到当前未登录 QQ。",
        )
        plugin._auto_login_qq = AsyncMock(
            return_value=self.module.AutoLoginAttemptResult(
                submitted=False,
                detail="仍需人工处理。",
                manual_action_required=True,
            )
        )
        plugin._verify_recovery_status = AsyncMock(return_value=offline_snapshot)
        plugin._log = lambda *args, **kwargs: None

        await plugin._recover_login_only(offline_snapshot, keep_manual_pending=True)

        self.assertEqual(plugin._auto_login_qq.await_count, 1)
        self.assertEqual(
            plugin._auto_login_qq.await_args.kwargs,
            {
                "reset_manual_pending": False,
                "notify_manual_action": False,
            },
        )

    async def test_recover_napcat_skips_repeated_retry_when_auto_login_needs_manual_action(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
                "qq_password": "pass123",
            }
        )
        offline_snapshot = self.module.StatusSnapshot(
            overall_status="offline",
            service=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=301,
                detail="HTTP 301，NapCat 服务可达。",
            ),
            login=self.module.LoginCheckResult(
                state="not_logged_in",
                endpoint="http://localhost:6099/api/QQLogin/CheckLoginStatus",
                detail="WebUI 检测到当前未登录 QQ。",
            ),
        )

        plugin._clear_login_state = AsyncMock()
        plugin._start_napcat = AsyncMock()
        plugin._wait_for_napcat_service_ready = AsyncMock(
            return_value=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=301,
                detail="HTTP 301，NapCat 服务可达。",
            )
        )
        plugin._auto_login_qq = AsyncMock(
            return_value=self.module.AutoLoginAttemptResult(
                submitted=False,
                detail=(
                    "NapCat 当前未登录，需要扫码完成 QQ 登录。"
                    " | 已生成 QQ 登录二维码（2 分内有效）: https://txz.qq.com/p?k=abc"
                ),
                level="WARNING",
                manual_action_required=True,
            )
        )
        plugin._collect_status_snapshot = AsyncMock(
            return_value=offline_snapshot
        )
        plugin._emit_snapshot_logs = lambda *args, **kwargs: None
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message, kwargs)
        )

        with patch.object(self.module.subprocess, "run"), patch.object(
            self.module.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            await plugin._recover_napcat()

        self.assertTrue(
            any("已生成 QQ 登录二维码，等待扫码登录" in item[1] for item in captured)
        )
        self.assertTrue(
            any("当前仍在等待二维码扫码登录:" in item[1] for item in captured)
        )
        self.assertTrue(
            any("本轮只做 1 次状态校验" in item[1] for item in captured)
        )
        self.assertEqual(
            plugin._collect_status_snapshot.await_count,
            1,
        )
        sleep_calls = [call.args[0] for call in sleep_mock.await_args_list]
        self.assertNotIn(15, sleep_calls)
        self.assertNotIn(8, sleep_calls)
        self.assertNotIn(5, sleep_calls)
        self.assertEqual(sleep_calls.count(2), 1)

    async def test_recover_napcat_keeps_fast_retry_for_general_auto_login_failures(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
                "qq_password": "pass123",
            }
        )
        offline_snapshot = self.module.StatusSnapshot(
            overall_status="offline",
            service=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=301,
                detail="HTTP 301，NapCat 服务可达。",
            ),
            login=self.module.LoginCheckResult(
                state="not_logged_in",
                endpoint="http://localhost:6099/api/QQLogin/CheckLoginStatus",
                detail="WebUI 检测到当前未登录 QQ。",
            ),
        )

        plugin._clear_login_state = AsyncMock()
        plugin._start_napcat = AsyncMock()
        plugin._wait_for_napcat_service_ready = AsyncMock(
            return_value=self.module.ServiceCheckResult(
                state="online",
                checked_url="http://localhost:6099",
                status_code=301,
                detail="HTTP 301，NapCat 服务可达。",
            )
        )
        plugin._auto_login_qq = AsyncMock(
            return_value=self.module.AutoLoginAttemptResult(
                submitted=False,
                detail="QQ 自动登录请求失败: connection reset by peer",
                level="ERROR",
                manual_action_required=False,
            )
        )
        plugin._collect_status_snapshot = AsyncMock(
            side_effect=[
                offline_snapshot,
                offline_snapshot,
                offline_snapshot,
                offline_snapshot,
            ]
        )
        plugin._emit_snapshot_logs = lambda *args, **kwargs: None
        plugin._log = lambda *args, **kwargs: None

        with patch.object(self.module.subprocess, "run"), patch.object(
            self.module.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            await plugin._recover_napcat()

        self.assertEqual(
            plugin._collect_status_snapshot.await_count,
            self.module.RECOVERY_VERIFY_ATTEMPTS_DEFAULT,
        )
        sleep_calls = [call.args[0] for call in sleep_mock.await_args_list]
        self.assertIn(2, sleep_calls)

    async def test_cmd_status_reports_service_and_login_details(self):
        plugin = self.make_plugin()
        plugin._check_count = 12
        plugin._consecutive_failures = 1
        plugin._collect_status_snapshot = AsyncMock(
            return_value=self.module.StatusSnapshot(
                overall_status="online",
                service=self.module.ServiceCheckResult(
                    state="online",
                    checked_url="http://localhost:6099",
                    status_code=301,
                    detail="HTTP 301，NapCat 服务可达。",
                ),
                login=self.module.LoginCheckResult(
                    state="logged_in",
                    endpoint="http://localhost:6099/get_login_info",
                    detail="已获取登录账号信息: NapCatBot (123456789)。",
                    user_id="123456789",
                    nickname="NapCatBot",
                ),
            )
        )

        event = DummyEvent()
        results = []
        async for item in plugin.cmd_status(event):
            results.append(item)

        self.assertEqual(len(results), 1)
        message = results[0]
        self.assertIn("NapCat 服务", message)
        self.assertIn("QQ 登录", message)
        self.assertIn("NapCatBot (123456789)", message)

    async def test_cmd_status_reports_manual_login_pending_details(self):
        plugin = self.make_plugin()
        plugin._check_count = 3
        plugin._manual_login_pending_until = 9999999999.0
        plugin._manual_login_pending_reason = "NapCat 当前未登录，需要扫码完成 QQ 登录。"
        plugin._manual_login_pending_context = {
            "account": "123456789",
            "qrcode_url": "https://example.com/original-qr",
            "qrcode_expires_at": 220.0,
        }
        plugin._collect_status_snapshot = AsyncMock(
            return_value=self.make_snapshot(
                "offline",
                login_state="not_logged_in",
                login_detail="WebUI 检测到当前未登录 QQ。",
            )
        )

        with patch.object(
            self.module.time,
            "monotonic",
            return_value=100.0,
        ):
            event = DummyEvent()
            results = []
            async for item in plugin.cmd_status(event):
                results.append(item)

        self.assertEqual(len(results), 1)
        message = results[0]
        self.assertIn("二维码登录等待", message)
        self.assertIn("需要扫码完成 QQ 登录", message)
        self.assertIn("https://example.com/original-qr", message)

    async def test_handle_manual_login_pending_snapshot_skips_retry_before_interval(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
            }
        )
        snapshot = self.make_snapshot(
            "offline",
            login_state="not_logged_in",
            login_detail="WebUI 检测到当前未登录 QQ。",
        )
        plugin._manual_login_pending_until = 200.0
        plugin._manual_login_pending_reason = "NapCat 当前未登录，需要扫码完成 QQ 登录。"
        plugin._manual_login_pending_context = {
            "account": "123456789",
            "qrcode_url": "https://example.com/original-qr",
            "qrcode_expires_at": 130.0,
        }
        plugin._recover_login_only = AsyncMock()
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message, kwargs)
        )

        with patch.object(self.module.time, "monotonic", return_value=110.0):
            handled = await plugin._handle_manual_login_pending_snapshot(
                snapshot,
                "2026-03-25 12:00:00",
            )

        self.assertTrue(handled)
        self.assertEqual(plugin._recover_login_only.await_count, 0)
        self.assertTrue(any("本轮继续等待扫码登录" in item[1] for item in captured))

    async def test_handle_manual_login_pending_snapshot_retries_login_only_when_due(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
            }
        )
        snapshot = self.make_snapshot(
            "offline",
            login_state="not_logged_in",
            login_detail="WebUI 检测到当前未登录 QQ。",
        )
        plugin._manual_login_pending_until = 200.0
        plugin._manual_login_pending_reason = "NapCat 当前未登录，需要扫码完成 QQ 登录。"
        plugin._manual_login_pending_context = {
            "account": "123456789",
            "qrcode_url": "https://example.com/original-qr",
            "qrcode_expires_at": 120.0,
        }
        plugin._recover_login_only = AsyncMock()
        plugin._log = lambda *args, **kwargs: None

        with patch.object(self.module.time, "monotonic", return_value=130.0):
            handled = await plugin._handle_manual_login_pending_snapshot(
                snapshot,
                "2026-03-25 12:00:00",
            )

        self.assertTrue(handled)
        self.assertEqual(plugin._recover_login_only.await_count, 1)
        self.assertEqual(
            plugin._recover_login_only.await_args.kwargs,
            {"keep_manual_pending": True},
        )

    async def test_prepare_manual_login_assistance_reuses_cached_qr_before_timeout(self):
        plugin = self.make_plugin()
        original_until = 9999999999.0
        plugin._manual_login_pending_until = original_until
        plugin._manual_login_pending_reason = "NapCat 当前未登录，需要扫码完成 QQ 登录。"
        plugin._manual_login_pending_context = {
            "account": "123456789",
            "qrcode_url": "https://example.com/original-qr",
            "qrcode_expires_at": 500.0,
        }
        plugin._fetch_login_qrcode = AsyncMock()
        plugin._log = lambda *args, **kwargs: None

        with patch.object(self.module.time, "monotonic", return_value=400.0):
            detail = await plugin._prepare_manual_login_assistance(
                object(),
                "credential-123",
                "123456789",
                "NapCat 当前未登录，需要扫码完成 QQ 登录。",
                notify=False,
            )

        self.assertIn("https://example.com/original-qr", detail)
        self.assertEqual(plugin._fetch_login_qrcode.await_count, 0)
        self.assertEqual(plugin._manual_login_pending_until, original_until)
        self.assertEqual(
            plugin._manual_login_pending_context["qrcode_url"],
            "https://example.com/original-qr",
        )

    async def test_prepare_manual_login_assistance_refreshes_qr_after_timeout(self):
        plugin = self.make_plugin()
        plugin._manual_login_pending_until = 9999999999.0
        plugin._manual_login_pending_reason = "NapCat 当前未登录，需要扫码完成 QQ 登录。"
        plugin._manual_login_pending_context = {
            "account": "123456789",
            "qrcode_url": "https://example.com/original-qr",
            "qrcode_expires_at": 405.0,
        }
        plugin._fetch_login_qrcode = AsyncMock(
            return_value=("https://example.com/new-qr", "已获取 QQ 登录二维码。")
        )
        plugin._notify_manual_login_required = AsyncMock(return_value=True)
        plugin._log = lambda *args, **kwargs: None

        with patch.object(self.module.time, "monotonic", return_value=410.0):
            detail = await plugin._prepare_manual_login_assistance(
                object(),
                "credential-123",
                "123456789",
                "NapCat 当前未登录，需要扫码完成 QQ 登录。",
                notify=False,
            )

        self.assertIn("https://example.com/new-qr", detail)
        self.assertEqual(plugin._fetch_login_qrcode.await_count, 1)
        self.assertEqual(plugin._notify_manual_login_required.await_count, 1)
        self.assertEqual(
            plugin._manual_login_pending_context["qrcode_url"],
            "https://example.com/new-qr",
        )
        self.assertGreater(plugin._manual_login_pending_until, 410.0)


if __name__ == "__main__":
    unittest.main()
