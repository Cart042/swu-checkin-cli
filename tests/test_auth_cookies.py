"""统一认证跳转遗留设备 Cookie 的离线回归测试。"""

import unittest
from unittest import mock

from swu_checkin.auth import cookies


class LoginCookieTests(unittest.TestCase):
    def test_drop_federation_cookies_only_touches_login_host(self):
        context = mock.Mock()
        context.cookies.return_value = [
            {"name": "61zqTsrO93nzO", "value": "a", "domain": "idm.swu.edu.cn", "path": "/"},
            {"name": "61zqTsrO93nzP", "value": "b", "domain": "idm.swu.edu.cn", "path": "/"},
            {"name": "SESSION", "value": "c", "domain": "idm.swu.edu.cn", "path": "/am"},
            {"name": "61zqTsrO93nzO", "value": "d", "domain": "uaaap.swu.edu.cn", "path": "/"},
            {"name": "iPlanetDirectoryPro", "value": "e", "domain": "idm.swu.edu.cn", "path": "/"},
        ]
        dropped = cookies._drop_federation_cookies(context, "idm.swu.edu.cn", "offline")
        self.assertTrue(dropped)
        # Deletion is per name+domain: the rest of the jar is never rebuilt, so
        # a failure cannot leave the browser without its session cookies.
        self.assertEqual(
            context.clear_cookies.call_args_list,
            [
                mock.call(name="61zqTsrO93nzO", domain="idm.swu.edu.cn"),
                mock.call(name="61zqTsrO93nzP", domain="idm.swu.edu.cn"),
            ],
        )
        context.add_cookies.assert_not_called()
        context.clear_cookies.reset_mock()

    def test_drop_federation_cookies_matches_domain_cookies_too(self):
        context = mock.Mock()
        context.cookies.return_value = [
            {"name": "61zqTsrO93nzO", "value": "a", "domain": ".swu.edu.cn", "path": "/"},
            {"name": "SESSION", "value": "b", "domain": ".swu.edu.cn", "path": "/"},
        ]
        dropped = cookies._drop_federation_cookies(context, "idm.swu.edu.cn", "offline")
        self.assertTrue(dropped)
        # A device cookie scoped to the registrable domain is replayed to the
        # login host as well, so it has to be removed with the same rule.
        context.clear_cookies.assert_called_once_with(name="61zqTsrO93nzO", domain=".swu.edu.cn")

    def test_drop_federation_cookies_reports_failure_without_emptying_jar(self):
        context = mock.Mock()
        context.cookies.return_value = [
            {"name": "61zqTsrO93nzO", "value": "a", "domain": "idm.swu.edu.cn", "path": "/"},
        ]
        context.clear_cookies.side_effect = RuntimeError("cookie store busy")
        self.assertFalse(cookies._drop_federation_cookies(context, "idm.swu.edu.cn", "offline"))
        context.add_cookies.assert_not_called()

    def test_drop_federation_cookies_ignores_other_hosts(self):
        context = mock.Mock()
        context.cookies.return_value = [
            {"name": "61zqTsrO93nzO", "value": "a", "domain": "uaaap.swu.edu.cn", "path": "/"},
        ]
        self.assertTrue(cookies._drop_federation_cookies(context, "idm.swu.edu.cn", "offline"))
        context.clear_cookies.assert_not_called()
        context.add_cookies.assert_not_called()


if __name__ == "__main__":
    unittest.main()
