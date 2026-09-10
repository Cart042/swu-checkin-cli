"""Offline regression tests for the browser login/API boundary."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import get_info


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class LoginApiOfflineTests(unittest.TestCase):
    def test_direct_login_surface_and_configuration_are_removed(self):
        source = Path(get_info.__file__).read_text(encoding="utf-8")
        self.assertFalse(hasattr(get_info, "get_token_direct"))
        self.assertNotIn("SWU_LOGIN_METHOD", source)
        self.assertNotIn("from des import", source)
        self.assertNotIn("_transform_ticket", source)

    def test_write_request_is_never_retried(self):
        session = FakeSession([FakeResponse(500), FakeResponse(200)])
        with mock.patch.object(
            get_info.time, "sleep", side_effect=AssertionError("write request slept")
        ):
            with self.assertRaises(get_info.SwuRequestError) as caught:
                get_info.request_with_retry(
                    "POST",
                    "https://school.invalid/write",
                    session=session,
                    max_retries=5,
                )
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(len(session.calls), 1)

    def test_idempotent_request_retries_transient_http_status(self):
        session = FakeSession([FakeResponse(503), FakeResponse(200, {"ok": True})])
        with mock.patch.object(get_info.time, "sleep", return_value=None):
            response = get_info.request_with_retry(
                "GET", "https://school.invalid/read", session=session
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.calls), 2)

    def test_ticket_exchange_has_abort_timeout_and_accepts_string_token(self):
        class Page:
            def __init__(self):
                self.calls = []

            def evaluate(self, script, argument):
                self.calls.append((script, argument))
                return {"ok": True, "status": 200, "data": "ticket-token", "text": "ok"}

        page = Page()
        with mock.patch.object(get_info.time, "monotonic", return_value=10.0):
            token = get_info.exchange_token_from_browser_page(
                page, "ticket-secret", timeout=15, deadline=20.0
            )
        self.assertEqual(token, "ticket-token")
        self.assertEqual(page.calls[0][1]["timeoutMs"], 10000)
        self.assertIn("AbortController", page.calls[0][0])

    def test_http_401_is_token_invalid(self):
        session = FakeSession([FakeResponse(401, {"message": "expired"})])
        with self.assertRaises(get_info.TokenInvalidError) as caught:
            get_info.request_with_retry(
                "GET", "https://school.invalid/user", session=session
            )
        self.assertEqual(caught.exception.status_code, 401)

    def test_cache_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "nested" / ".token_cache.json"
            get_info._save_cached_token("student", "secret-token", str(cache_path))

            self.assertEqual(
                get_info._load_cached_token("student", str(cache_path)), "secret-token"
            )
            self.assertEqual(stat.S_IMODE(cache_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(cache_path.parent.stat().st_mode), 0o700)
            self.assertEqual(list(cache_path.parent.glob(".token-cache-*")), [])
            self.assertEqual(
                json.loads(cache_path.read_text(encoding="utf-8")),
                {"student": "secret-token"},
            )

    def test_cache_network_failure_is_not_treated_as_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / ".token_cache.json"
            get_info._save_cached_token("student", "cached-token", str(cache_path))
            with mock.patch.object(get_info, "CONFIG_DIR", directory), mock.patch.object(
                get_info,
                "get_student_id",
                side_effect=get_info.requests.exceptions.ConnectionError("offline"),
            ), mock.patch.object(get_info, "sync_playwright") as browser:
                with self.assertRaises(get_info.requests.exceptions.ConnectionError):
                    get_info.get_token("student", "password")
                browser.assert_not_called()

    def test_debug_artifact_contains_no_page_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"SWU_DEBUG_DIR": directory}, clear=False
        ):

            class Page:
                url = "https://school.invalid/callback?ticket=ticket-secret&state=state-secret"

                def title(self):
                    return "student / password-secret"

                def locator(self, _selector):
                    class Locator:
                        def count(self):
                            return 1

                    return Locator()

                def content(self):
                    raise AssertionError("raw HTML must not be persisted")

                def screenshot(self, **_kwargs):
                    raise AssertionError("raw screenshot must not be persisted")

            get_info.save_login_debug_artifacts(
                Page(),
                "student",
                "login_failed",
                RuntimeError("password=secret-password"),
            )

            files = list(Path(directory).iterdir())
            self.assertEqual([path.suffix for path in files], [".txt"])
            text = files[0].read_text(encoding="utf-8")
            self.assertNotIn("ticket-secret", text)
            self.assertNotIn("state-secret", text)
            self.assertNotIn("secret-password", text)
            self.assertNotIn("student", text)


if __name__ == "__main__":
    unittest.main()
