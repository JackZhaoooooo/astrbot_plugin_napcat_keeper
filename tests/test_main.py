import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock


class DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


class DummyFilter:
    class PermissionType:
        ADMIN = "admin"

    def on_astrbot_loaded(self):
        return lambda f: f

    def command(self, _name):
        return lambda f: f

    def permission_type(self, _permission):
        return lambda f: f


class DummyStar:
    def __init__(self, context):
        self.context = context


class DummyEvent:
    def plain_result(self, message):
        return message


class DummyMessageChain:
    def message(self, _message):
        return self

    def file_image(self, _path):
        return self


def load_main_module():
    sys.modules.pop("main", None)

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")

    api_module.AstrBotConfig = dict
    api_module.logger = DummyLogger()
    event_module.AstrMessageEvent = DummyEvent
    event_module.MessageChain = DummyMessageChain
    event_module.filter = DummyFilter()
    star_module.Context = object
    star_module.Star = DummyStar

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module

    return importlib.import_module("main")


class NapcatKeeperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_main_module()
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))

    def make_plugin(self, config=None):
        return self.module.NapcatKeeperPlugin(self.context, config or {})

    def test_normalize_umo_list(self):
        plugin = self.make_plugin()
        result = plugin._normalize_umo_list("a:1, b:2\na:1，c:3")
        self.assertEqual(result, ["a:1", "b:2", "c:3"])

    def test_is_verify_required(self):
        plugin = self.make_plugin()
        self.assertTrue(plugin._is_verify_required("需要验证码验证"))
        self.assertTrue(plugin._is_verify_required("need verify"))
        self.assertFalse(plugin._is_verify_required("network timeout"))

    def test_deep_find_qr_url(self):
        plugin = self.make_plugin()
        payload = {
            "code": 0,
            "data": {
                "nested": {
                    "qrcodeUrl": "https://txz.qq.com/p?k=abc",
                }
            },
        }
        self.assertEqual(plugin._deep_find_qr_url(payload), "https://txz.qq.com/p?k=abc")

    async def test_attempt_relogin_switches_to_qr_when_password_requires_verify(self):
        plugin = self.make_plugin(
            {
                "napcat_token": "token123",
                "qq_account": "123456",
                "qq_password": "pass123",
            }
        )
        plugin._try_password_login = AsyncMock(return_value=(False, "需要验证码"))
        plugin._enter_qr_login_flow = AsyncMock()

        snapshot = self.module.LoginSnapshot(
            service_online=True,
            login_state="not_logged_in",
            detail="未登录",
        )
        await plugin._attempt_relogin(snapshot)

        self.assertEqual(plugin._enter_qr_login_flow.await_count, 1)

    async def test_attempt_relogin_uses_qr_directly_when_password_not_configured(self):
        plugin = self.make_plugin(
            {
                "qq_account": "123456",
            }
        )
        plugin._enter_qr_login_flow = AsyncMock()
        snapshot = self.module.LoginSnapshot(
            service_online=True,
            login_state="not_logged_in",
            detail="未登录",
        )

        await plugin._attempt_relogin(snapshot)

        self.assertEqual(plugin._enter_qr_login_flow.await_count, 1)


if __name__ == "__main__":
    unittest.main()
