"""登录跳转地址解析的离线回归测试。"""

import unittest
from unittest import mock

from swu_checkin.auth import urls


class LoginUrlTests(unittest.TestCase):
    def test_absolute_redirect_target_resolves_and_upgrades(self):
        response = mock.Mock(url="https://idm.swu.edu.cn/am/UI/Login", status=302)
        self.assertEqual(
            urls._absolute_redirect_target(response, "http://idm.swu.edu.cn/am/oauth2/authorize?service=initService"),
            "https://idm.swu.edu.cn/am/oauth2/authorize?service=initService",
        )
        self.assertEqual(
            urls._absolute_redirect_target(response, "/am/oauth2/authorize?service=initService"),
            "https://idm.swu.edu.cn/am/oauth2/authorize?service=initService",
        )
        self.assertEqual(
            urls._absolute_redirect_target(response, "https://portal.swu.edu.cn/next"),
            "https://portal.swu.edu.cn/next",
        )
        self.assertIsNone(urls._absolute_redirect_target(response, ""))
        self.assertIsNone(urls._absolute_redirect_target(response, None))


if __name__ == "__main__":
    unittest.main()
