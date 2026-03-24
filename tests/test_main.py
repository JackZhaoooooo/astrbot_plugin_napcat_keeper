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


def install_astrbot_stubs():
    logger = DummyLogger()

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")

    api_module.AstrBotConfig = dict
    api_module.logger = logger
    event_module.AstrMessageEvent = DummyEvent
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

    def make_plugin(self, config=None):
        with patch.object(self.module, "_get_file_logger", return_value=DummyLogger()):
            plugin = self.module.NapcatKeeperPlugin(object(), config or {})
        return plugin

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
        self.assertIn("QQ 登录检测", captured[1][1])
        self.assertEqual(captured[1][0], "WARNING")
        self.assertIn("综合判定", captured[2][1])
        self.assertEqual(captured[2][0], "WARNING")

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

        success = await plugin._auto_login_qq()

        self.assertTrue(success)
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

        success = await plugin._auto_login_qq()

        self.assertTrue(success)
        self.assertEqual(plugin._password_login_by_account.await_count, 0)
        self.assertEqual(plugin._quick_login_by_account.await_count, 1)
        self.assertEqual(
            plugin._quick_login_by_account.await_args.args[2],
            "123456789",
        )

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
            return True

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
