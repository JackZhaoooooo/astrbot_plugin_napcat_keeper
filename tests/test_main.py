import importlib
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock


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
        return self.module.NapcatKeeperPlugin(context, dict(config or {}))

    def make_state(self, state, user_id=None, nickname=None, detail="detail"):
        return self.module.LoginState(
            state=state,
            endpoint="http://localhost:6099/api/get_login_info",
            detail=detail,
            user_id=user_id,
            nickname=nickname,
        )

    def test_normalize_umo_list_supports_string_and_deduplicates(self):
        plugin = self.make_plugin(
            {"notify_umos": "umo-a\numo-b,umo-a\n  \numo-c  "}
        )

        self.assertEqual(plugin.notify_umos, ["umo-a", "umo-b", "umo-c"])

    def test_build_login_state_from_payload_returns_logged_in(self):
        plugin = self.make_plugin()

        result = plugin._build_login_state_from_payload(
            "http://localhost:6099/api/get_login_info",
            {
                "status": "ok",
                "data": {
                    "user_id": 123456789,
                    "nickname": "NapCatBot",
                },
            },
        )

        self.assertEqual(result.state, "logged_in")
        self.assertEqual(result.user_id, "123456789")
        self.assertEqual(result.nickname, "NapCatBot")

    def test_build_login_state_from_payload_returns_logged_out(self):
        plugin = self.make_plugin()

        result = plugin._build_login_state_from_payload(
            "http://localhost:6099/api/get_login_info",
            {
                "status": "ok",
                "message": "not logged in",
                "data": {},
            },
        )

        self.assertEqual(result.state, "logged_out")
        self.assertIn("未返回有效账号信息", result.detail)
        self.assertIn("not logged in", result.detail)

    def test_payload_indicates_auth_failure_detects_unauthorized(self):
        plugin = self.make_plugin()

        result = plugin._payload_indicates_auth_failure(
            {"code": -1, "message": "Unauthorized"}
        )

        self.assertEqual(result, "鉴权失败: Unauthorized")

    async def test_check_once_sends_logout_notification_only_on_transition(self):
        context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        plugin = self.make_plugin(
            {"notify_umos": ["umo-1", "umo-2"]},
            context=context,
        )
        plugin._fetch_login_state = AsyncMock(
            side_effect=[
                self.make_state("logged_in", user_id="123456"),
                self.make_state("logged_out", detail="账号已退出"),
                self.make_state("logged_out", detail="账号已退出"),
            ]
        )

        await plugin.check_once()
        await plugin.check_once()
        await plugin.check_once()

        self.assertEqual(context.send_message.await_count, 2)
        first_call = context.send_message.await_args_list[0]
        self.assertEqual(first_call.args[0], "umo-1")
        self.assertIn("NapCat 检测到 QQ 已退出登录", first_call.args[1].get_plain_text())

    async def test_check_once_notifies_again_after_relogin(self):
        context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        plugin = self.make_plugin(
            {"notify_umos": ["umo-1", "umo-2"]},
            context=context,
        )
        plugin._fetch_login_state = AsyncMock(
            side_effect=[
                self.make_state("logged_in", user_id="123456"),
                self.make_state("logged_out", detail="第一次掉线"),
                self.make_state("logged_in", user_id="123456"),
                self.make_state("logged_out", detail="第二次掉线"),
            ]
        )

        await plugin.check_once()
        await plugin.check_once()
        await plugin.check_once()
        await plugin.check_once()

        self.assertEqual(context.send_message.await_count, 4)

    async def test_status_command_returns_formatted_message(self):
        plugin = self.make_plugin()
        plugin._fetch_login_state = AsyncMock(
            return_value=self.make_state(
                "logged_in",
                user_id="123456",
                nickname="NapCatBot",
                detail="接口正常",
            )
        )

        result = await plugin.napcat_status(DummyEvent())

        self.assertIn("NapCat 登录状态", result)
        self.assertIn("123456 (NapCatBot)", result)
        self.assertIn("🟢 已登录", result)

    async def test_fetch_login_state_uses_webui_result_when_available(self):
        plugin = self.make_plugin()
        plugin._request_webui_credential = AsyncMock(return_value=("credential", None))
        plugin._post_json = AsyncMock(
            side_effect=[
                (
                    {
                        "code": 0,
                        "data": {
                            "isLogin": True,
                        },
                        "message": "success",
                    },
                    None,
                ),
                (
                    {
                        "code": 0,
                        "data": {
                            "uin": "987654321",
                            "nick": "FallbackBot",
                        },
                        "message": "success",
                    },
                    None,
                ),
            ]
        )

        result = await plugin._fetch_login_state()

        self.assertEqual(result.state, "logged_in")
        self.assertEqual(result.user_id, "987654321")
        self.assertEqual(result.nickname, "FallbackBot")

    async def test_fetch_login_state_falls_back_to_second_onebot_endpoint(self):
        plugin = self.make_plugin()
        plugin._fetch_login_state_via_webui = AsyncMock(
            return_value=(None, "WebUI 鉴权失败")
        )
        plugin._request_json = AsyncMock(
            side_effect=[
                (
                    {
                        "code": -1,
                        "message": "Unauthorized",
                    },
                    None,
                ),
                (None, "HTTP 404"),
                (
                    {
                        "status": "ok",
                        "data": {
                            "user_id": "987654321",
                            "nickname": "FallbackBot",
                        },
                    },
                    None,
                ),
            ]
        )

        result = await plugin._fetch_login_state()

        self.assertEqual(result.state, "logged_in")
        self.assertEqual(result.user_id, "987654321")
        self.assertEqual(result.nickname, "FallbackBot")

    async def test_initial_logged_out_triggers_notification_when_enabled(self):
        context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        plugin = self.make_plugin(
            {
                "notify_umos": ["umo-1"],
                "notify_on_initial_logged_out": True,
            },
            context=context,
        )
        plugin._fetch_login_state = AsyncMock(
            return_value=self.make_state("logged_out", detail="初始即掉线")
        )

        await plugin.check_once()

        self.assertEqual(context.send_message.await_count, 1)

    async def test_initial_logged_out_no_notification_when_disabled(self):
        context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        plugin = self.make_plugin(
            {
                "notify_umos": ["umo-1"],
                "notify_on_initial_logged_out": False,
            },
            context=context,
        )
        plugin._fetch_login_state = AsyncMock(
            return_value=self.make_state("logged_out", detail="初始即掉线")
        )

        await plugin.check_once()

        self.assertEqual(context.send_message.await_count, 0)

    async def test_precheck_qq_official_msg_id_missing_blocks_send(self):
        context = types.SimpleNamespace(
            send_message=AsyncMock(return_value=True),
            platform_manager=types.SimpleNamespace(
                platform_insts=[
                    types.SimpleNamespace(
                        meta=lambda: types.SimpleNamespace(id="default"),
                        _session_last_inbound_message_id={},
                    )
                ]
            ),
        )
        plugin = self.make_plugin(
            {"notify_umos": ["default:FriendMessage:session-1"]},
            context=context,
        )

        delivered = await plugin._send_logout_notifications(
            self.make_state("logged_out", detail="测试")
        )

        self.assertFalse(delivered)
        self.assertEqual(context.send_message.await_count, 0)

    async def test_precheck_uses_outbound_anchor_when_available(self):
        platform = types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(id="default"),
            _session_last_inbound_message_id={},
            _session_last_outbound_message_id={"session-1": "outbound-abc"},
            remember_session_inbound_message_id=lambda *_args: None,
        )
        context = types.SimpleNamespace(
            send_message=AsyncMock(return_value=True),
            platform_manager=types.SimpleNamespace(platform_insts=[platform]),
        )
        plugin = self.make_plugin(
            {"notify_umos": ["default:FriendMessage:session-1"]},
            context=context,
        )

        delivered = await plugin._send_logout_notifications(
            self.make_state("logged_out", detail="测试")
        )

        self.assertTrue(delivered)
        self.assertEqual(context.send_message.await_count, 1)

    async def test_offline_reminds_again_when_interval_elapsed(self):
        context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        plugin = self.make_plugin(
            {
                "notify_umos": ["umo-1"],
                "notify_retry_cooldown_seconds": 5,
            },
            context=context,
        )
        plugin._fetch_login_state = AsyncMock(
            return_value=self.make_state("logged_out", detail="持续离线")
        )
        plugin._last_state = self.make_state("logged_out", detail="持续离线")

        plugin._last_notify_attempt_at = time.monotonic() - 6
        await plugin.check_once()
        self.assertEqual(context.send_message.await_count, 1)

        await plugin.check_once()
        self.assertEqual(context.send_message.await_count, 1)

        plugin._last_notify_attempt_at = time.monotonic() - 6
        await plugin.check_once()
        self.assertEqual(context.send_message.await_count, 2)


if __name__ == "__main__":
    unittest.main()
