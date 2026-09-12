"""共享数据结构的边界测试：状态码、结果对象和账号形状。"""

import dataclasses
import unittest

from swu_checkin import models, status
from swu_checkin.status import CheckinStatus


class CheckinStatusTests(unittest.TestCase):
    def test_members_keep_the_historical_integer_values(self):
        expected = {
            "NO_TASK": 0,
            "SUCCESS": 1,
            "ALREADY_CHECKED_IN": 2,
            "CREDENTIAL_FAILED": 3,
            "CONNECTION_ERROR": 4,
            "ON_LEAVE": 5,
            "PAGE_LOAD_FAILED": 6,
            "CAPTCHA_FAILED": 7,
            "TOKEN_EXTRACT_FAILED": 8,
            "LOGIN_PAGE_CHANGED": 9,
            "SCHOOL_API_ERROR": 10,
            "TOKEN_INVALID": 11,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertEqual(int(getattr(CheckinStatus, name)), value)

    def test_status_messages_stay_addressable_by_plain_integers(self):
        # 推送汇总、日志和第三方调用方历史上都按整数取文案。
        self.assertEqual(status.STATUS_MESSAGES[4], "连接错误或请求超时，请稍后重试。")
        self.assertEqual(
            status.STATUS_MESSAGES[CheckinStatus.CONNECTION_ERROR],
            status.STATUS_MESSAGES[4],
        )
        self.assertEqual(len(status.STATUS_MESSAGES), 12)
        self.assertEqual(len(set(status.STATUS_MESSAGES)), 12)

    def test_coerce_status_falls_back_to_a_school_error(self):
        self.assertIs(status.coerce_status(4), CheckinStatus.CONNECTION_ERROR)
        self.assertIs(status.coerce_status(CheckinStatus.SUCCESS), CheckinStatus.SUCCESS)
        for value in (None, "not-a-status", 99):
            with self.subTest(value=value):
                self.assertIs(status.coerce_status(value), CheckinStatus.SCHOOL_API_ERROR)


class ResultObjectTests(unittest.TestCase):
    def test_checkin_result_replaces_the_positional_tuple(self):
        result = models.CheckinResult(
            username="alice",
            message="签到成功。",
            ok=True,
            attempt=2,
            status=CheckinStatus.SUCCESS,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.attempt, 2)
        self.assertIs(result.status, CheckinStatus.SUCCESS)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            # 故意赋值以证明结果对象不可变；静态检查会因此报只读属性。
            result.ok = False  # type: ignore[misc]

    def test_checkin_result_status_defaults_to_none_for_unfinished_runs(self):
        result = models.CheckinResult(username="bob", message="达到最大重试轮数 3，未完成", ok=False)
        self.assertIsNone(result.status)
        self.assertEqual(result.attempt, 1)

    def test_notification_result_is_truthy_only_when_something_was_sent(self):
        self.assertTrue(models.NotificationResult(sent=True, channels=("Bark",)))
        self.assertFalse(models.NotificationResult(sent=False, failed=("Bark",)))
        self.assertFalse(bool(models.NotificationResult(sent=False)))


if __name__ == "__main__":
    unittest.main()
