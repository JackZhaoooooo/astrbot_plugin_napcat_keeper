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

    def test_normalize_webhook_targets_filters_invalid_values(self):
        valid, invalid = self.module.NapcatKeeperPlugin._normalize_webhook_targets(
            [
                " https://example.com/a ",
                "http://example.com/b, ftp://bad.example.com/c",
                "not-a-url",
                "https://example.com/a",
                "",
                None,
            ]
        )

        self.assertEqual(valid, ["https://example.com/a", "http://example.com/b"])
        self.assertEqual(invalid, ["ftp://bad.example.com/c", "not-a-url"])

    def test_plugin_init_warns_invalid_notification_webhooks(self):
        self.make_plugin(
            {
                "logout_notify_webhooks": ["not-a-url"],
                "relogin_notify_webhooks": ["ftp://bad.example.com/hook"],
            }
        )

        self.assertTrue(
            any(
                level == "WARNING"
                and "忽略无效的退出登录 Webhook 地址" in message
                for level, message, _kwargs in self.platform_logger.records
            )
        )
        self.assertTrue(
            any(
                level == "WARNING"
                and "忽略无效的重新登录 Webhook 地址" in message
                for level, message, _kwargs in self.platform_logger.records
            )
        )

    async def test_send_notification_to_umos_skips_qq_official_session_proactively(self):
        context = types.SimpleNamespace(
            send_message=AsyncMock(return_value=True),
            platform_manager=types.SimpleNamespace(
                platform_insts=[
                    DummyPlatform(
                        id="default",
                        name="qq_official",
                        description="QQ 机器人官方 API 适配器",
                    )
                ]
            ),
        )
        plugin = self.make_plugin(context=context)
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message)
        )

        await plugin._send_notification_to_umos(
            ["default:FriendMessage:session-1"],
            "test message",
            "重新登录通知",
        )
        await plugin._send_notification_to_umos(
            ["default:FriendMessage:session-1"],
            "test message",
            "重新登录通知",
        )

        self.assertEqual(context.send_message.await_count, 0)
        self.assertTrue(
            any(
                level == "WARNING"
                and "QQ 官方平台会话依赖最近一条有效消息的 msg_id" in message
                for level, message in captured
            )
        )
        self.assertTrue(
            any(
                level == "DEBUG"
                and "已标记为不可主动通知" in message
                for level, message in captured
            )
        )

    async def test_send_notification_to_umos_disables_umo_after_invalid_msg_id_error(self):
        context = types.SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("请求参数msg_id无效或越权"))
        )
        plugin = self.make_plugin(context=context)
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message)
        )

        await plugin._send_notification_to_umos(
            ["default:FriendMessage:session-2"],
            "test message",
            "退出登录通知",
        )
        await plugin._send_notification_to_umos(
            ["default:FriendMessage:session-2"],
            "test message",
            "退出登录通知",
        )

        self.assertEqual(context.send_message.await_count, 1)
        self.assertTrue(
            any(
                level == "WARNING"
                and "msg_id 已失效或无权限" in message
                for level, message in captured
            )
        )
        self.assertTrue(
            any(
                level == "DEBUG"
                and "已标记为不可主动通知" in message
                for level, message in captured
            )
        )

    async def test_send_notification_to_webhooks_posts_json_payload(self):
        plugin = self.make_plugin(
            {
                "logout_notify_webhooks": ["https://example.com/hook-1"],
                "notification_webhook_timeout_seconds": 9,
            }
        )
        payload = {"event": "logout", "message": "test message"}
        plugin._post_notification_webhook = AsyncMock(
            side_effect=[
                (202, None),
                (200, None),
            ]
        )
        captured = []
        plugin._log = lambda message, level="INFO", **kwargs: captured.append(
            (level, message)
        )

        await plugin._send_notification_to_webhooks(
            ["https://example.com/hook-1", "https://example.com/hook-2"],
            payload,
            "退出登录通知",
        )

        self.assertEqual(plugin._post_notification_webhook.await_count, 2)
        self.assertEqual(
            plugin._post_notification_webhook.await_args_list[0].args[1],
            "https://example.com/hook-1",
        )
        self.assertEqual(
            plugin._post_notification_webhook.await_args_list[0].args[2],
            payload,
        )
        self.assertTrue(
            any(
                level == "INFO" and "Webhook 已发送" in message
                for level, message in captured
            )
        )

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

    async def test_handle_login_transition_notifications_sends_logout_webhook_payload(self):
        plugin = self.make_plugin(
            {
                "logout_notify_webhooks": ["https://example.com/logout-hook"],
            }
        )
        plugin._send_notification_to_umos = AsyncMock()
        plugin._send_notification_to_webhooks = AsyncMock()
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
            "2026-03-24 22:30:00",
        )

        self.assertEqual(plugin._send_notification_to_umos.await_count, 1)
        self.assertEqual(plugin._send_notification_to_webhooks.await_count, 1)
        self.assertEqual(
            plugin._send_notification_to_webhooks.await_args.args[0],
            ["https://example.com/logout-hook"],
        )
        payload = plugin._send_notification_to_webhooks.await_args.args[1]
        self.assertEqual(payload["event"], "logout")
        self.assertEqual(payload["account"]["user_id"], "123456789")
        self.assertEqual(payload["status"]["current_overall_status"], "offline")
        self.assertIn("NapCat Keeper 检测到 QQ 已退出登录", payload["message"])

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
                "qq_password": "pass123",
            }
        )
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")
        plugin._password_login_by_account = AsyncMock(
            return_value=(True, "已为 QQ 123456789 提交密码登录请求。")
        )
        plugin._quick_login_by_account = AsyncMock()
        plugin._log = lambda *args, **kwargs: None

        result = await plugin._auto_login_qq()

        self.assertTrue(result.submitted)
        self.assertIn("提交密码登录请求", result.detail)
        self.assertEqual(plugin._password_login_by_account.await_count, 1)
        self.assertEqual(plugin._quick_login_by_account.await_count, 0)
        self.assertEqual(
            plugin._password_login_by_account.await_args.args[2:],
            ("123456789", "pass123"),
        )

    async def test_auto_login_qq_uses_quick_login_without_password(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
                "qq_password": "",
            }
        )
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")
        plugin._password_login_by_account = AsyncMock()
        plugin._quick_login_by_account = AsyncMock(
            return_value=(True, "已为 QQ 123456789 提交快速登录请求。")
        )
        plugin._log = lambda *args, **kwargs: None

        result = await plugin._auto_login_qq()

        self.assertTrue(result.submitted)
        self.assertIn("提交快速登录请求", result.detail)
        self.assertEqual(plugin._password_login_by_account.await_count, 0)
        self.assertEqual(plugin._quick_login_by_account.await_count, 1)
        self.assertEqual(
            plugin._quick_login_by_account.await_args.args[2],
            "123456789",
        )

    async def test_auto_login_qq_returns_failure_reason_when_manual_action_needed(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
                "qq_password": "pass123",
            }
        )
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")
        plugin._password_login_by_account = AsyncMock(
            return_value=(
                False,
                "QQ 密码登录需要验证码，当前无法自动完成后续步骤。 | 验证地址: https://example.com/captcha",
            )
        )
        plugin._quick_login_by_account = AsyncMock(
            return_value=(False, "快速登录当前不可用。")
        )
        plugin._log = lambda *args, **kwargs: None

        result = await plugin._auto_login_qq()

        self.assertFalse(result.submitted)
        self.assertEqual(result.level, "WARNING")
        self.assertTrue(result.manual_action_required)
        self.assertIn("需要验证码", result.detail)
        self.assertIn("https://example.com/captcha", result.detail)
        self.assertEqual(plugin._quick_login_by_account.await_count, 1)

    async def test_auto_login_qq_falls_back_to_quick_login_when_password_login_fails(self):
        plugin = self.make_plugin(
            {
                "enable_auto_login": True,
                "qq_account": "123456789",
                "qq_password": "pass123",
            }
        )
        plugin._request_webui_credential = AsyncMock(return_value="credential-123")
        plugin._password_login_by_account = AsyncMock(
            return_value=(
                False,
                "QQ 密码登录需要验证码，当前无法自动完成后续步骤。 | 验证地址: https://example.com/captcha",
            )
        )
        plugin._quick_login_by_account = AsyncMock(
            return_value=(True, "已为 QQ 123456789 提交快速登录请求。")
        )
        plugin._log = lambda *args, **kwargs: None

        result = await plugin._auto_login_qq()

        self.assertTrue(result.submitted)
        self.assertFalse(result.manual_action_required)
        self.assertIn("已回退到快速登录并提交成功", result.detail)
        self.assertIn("密码登录原因", result.detail)
        self.assertIn("快速登录结果", result.detail)
        self.assertEqual(plugin._password_login_by_account.await_count, 1)
        self.assertEqual(plugin._quick_login_by_account.await_count, 1)

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

    async def test_recover_for_snapshot_always_uses_full_recovery_for_not_logged_in(self):
        plugin = self.make_plugin()
        snapshot = self.make_snapshot(
            "offline",
            login_state="not_logged_in",
            login_detail="WebUI 检测到当前未登录 QQ。",
        )
        plugin._recover_napcat = AsyncMock()
        plugin._log = lambda *args, **kwargs: None

        await plugin._recover_for_snapshot(snapshot)

        self.assertEqual(plugin._recover_napcat.await_count, 1)

    async def test_recover_for_snapshot_always_uses_full_recovery_when_service_is_abnormal(self):
        plugin = self.make_plugin()
        snapshot = self.make_snapshot(
            "error",
            service_state="error",
            service_detail="连接失败: connection refused",
        )
        plugin._recover_napcat = AsyncMock()
        plugin._log = lambda *args, **kwargs: None

        await plugin._recover_for_snapshot(snapshot)

        self.assertEqual(plugin._recover_napcat.await_count, 1)

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
                    "QQ 密码登录需要验证码，当前无法自动完成后续步骤。"
                    " | 验证地址: https://example.com/captcha"
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
            any("QQ 自动登录未成功。 | 原因:" in item[1] for item in captured)
        )
        self.assertTrue(
            any("QQ 自动登录未成功原因:" in item[1] for item in captured)
        )
        self.assertTrue(
            any("跳过重复快速重试" in item[1] for item in captured)
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


if __name__ == "__main__":
    unittest.main()
