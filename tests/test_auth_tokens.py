"""Token 提取、交换与缓存边界的离线回归测试。"""

import unittest
from unittest import mock

from swu_checkin.api import school as school_api
from swu_checkin.auth import tokens


class LoginTokenTests(unittest.TestCase):
    def test_initial_cas_service_query_is_not_a_login_redirect(self):
        cas_url = (
            "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL"
            "?service=https%3A%2F%2Fof.swu.edu.cn%2Fgateway%2Fresolve-cas-return"
        )

        class Page:
            url = cas_url

            def evaluate(self, _script):
                return "{}"

        page = Page()
        self.assertFalse(tokens._login_success_detected(page, cas_url))
        page.url = "https://of.swu.edu.cn/gateway/resolve-cas-return?ticket=one-time"
        self.assertTrue(tokens._login_success_detected(page, cas_url))

    def test_login_result_wait_uses_explicit_arg_keyword(self):
        class Page:
            url = "https://of.swu.edu.cn/cas/login"

            def __init__(self):
                self.calls = []

            def wait_for_function(self, expression, **kwargs):
                self.calls.append((expression, kwargs))
                self.url = "https://of.swu.edu.cn/gateway/resolve-cas-return?ticket=one-time"

            def evaluate(self, _script):
                return "{}"

        page = Page()
        self.assertTrue(tokens._wait_for_login_result(page, "https://of.swu.edu.cn/cas/login", 5))
        self.assertEqual(len(page.calls), 1)
        self.assertIn("arg", page.calls[0][1])
        self.assertNotIn("networkidle", page.calls[0][0])

    def test_local_storage_prefers_access_token_over_other_token_values(self):
        payload = {
            "refresh_token": "refresh-token",
            "auth": "auth-value",
            "access_token": "access-token",
        }
        self.assertEqual(tokens._token_from_local_storage(payload), "access-token")

    def test_invalid_new_token_is_not_cached(self):
        with (
            mock.patch.object(
                tokens,
                "get_student_id",
                side_effect=school_api.TokenInvalidError("invalid"),
            ),
            mock.patch.object(tokens, "save_cached_token") as save,
        ):
            with self.assertRaises(school_api.TokenInvalidError):
                tokens._validate_and_cache_token(
                    "student",
                    "candidate-token",
                    "/tmp/unused-token-cache.json",
                    5,
                    object(),
                    None,
                )
        save.assert_not_called()

    def test_ticket_exchange_has_abort_timeout_and_accepts_string_token(self):
        class Page:
            def __init__(self):
                self.calls = []

            def evaluate(self, script, argument):
                self.calls.append((script, argument))
                return {"ok": True, "status": 200, "data": "ticket-token", "text": "ok"}

        page = Page()
        with mock.patch.object(school_api.time, "monotonic", return_value=10.0):
            token = tokens.exchange_token_from_browser_page(page, "ticket-secret", timeout=15, deadline=20.0)
        self.assertEqual(token, "ticket-token")
        self.assertEqual(page.calls[0][1]["timeoutMs"], 10000)
        self.assertIn("AbortController", page.calls[0][0])


if __name__ == "__main__":
    unittest.main()
