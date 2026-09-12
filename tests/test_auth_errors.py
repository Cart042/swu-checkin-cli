"""登录失败分类（HTTP 状态、原因枚举与可重试错误）的离线回归测试。"""

import unittest
from unittest import mock

from swu_checkin.auth import errors


class LoginFailureClassificationTests(unittest.TestCase):
    def test_http_status_helpers_track_and_classify(self):
        self.assertEqual(errors._remember_http_status(503, None), 503)
        self.assertEqual(errors._remember_http_status(400, 503), 400)
        self.assertEqual(errors._remember_http_status(None, 503), 503)
        self.assertIsNone(errors._remember_http_status(200, None))
        self.assertIsNone(errors._remember_http_status(mock.Mock(), None))
        self.assertEqual(errors._login_failure_reason(400), "waf_blocked")
        self.assertEqual(errors._login_failure_reason(200), "captcha")
        self.assertIsNone(errors._http_status(mock.Mock()))
        self.assertEqual(errors._http_status(mock.Mock(status=200)), 200)

    def test_require_ok_http_status_marks_errors_as_retryable(self):
        errors._require_ok_http_status(None)
        errors._require_ok_http_status(200)
        with self.assertRaises(errors._RetryableHttpError) as caught:
            errors._require_ok_http_status(503)
        self.assertEqual(caught.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
