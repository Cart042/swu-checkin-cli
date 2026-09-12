"""浏览器层（UA、定位器、登录表单与错误提示）的离线回归测试。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swu_checkin.auth import browser as login_browser


class LoginBrowserTests(unittest.TestCase):
    def test_login_user_agent_hides_headless_marker(self):
        browser = mock.Mock(version="151.0.7922.34")
        user_agent = login_browser._login_user_agent(browser)
        self.assertNotIn("Headless", user_agent)
        self.assertIn("Chrome/151.0.0.0", user_agent)
        # The platform token must agree with the host, because navigator.platform
        # and sec-ch-ua-platform keep reporting the real operating system.
        self.assertIn(login_browser._platform_user_agent_token(), user_agent)
        self.assertEqual(
            login_browser._platform_user_agent_token(),
            login_browser._PLATFORM_UA_TOKENS.get(sys.platform, login_browser._PLATFORM_UA_TOKENS["win32"]),
        )

    def test_login_user_agent_falls_back_instead_of_going_headless(self):
        environment = {key: value for key, value in os.environ.items() if key != "SWU_LOGIN_UA"}
        with mock.patch.dict(os.environ, environment, clear=True):
            user_agent = login_browser._login_user_agent(mock.Mock(version="unknown"))
        # Falling back to the browser default would restore the HeadlessChrome
        # string that the school's front end rejects.
        self.assertEqual(user_agent, login_browser._FALLBACK_USER_AGENT)
        self.assertNotIn("Headless", user_agent)

    def test_login_user_agent_honours_explicit_override(self):
        with mock.patch.dict(os.environ, {"SWU_LOGIN_UA": "pinned-agent/1.0"}):
            self.assertEqual(
                login_browser._login_user_agent(mock.Mock(version="151.0.7922.34")),
                "pinned-agent/1.0",
            )

    def test_real_idm_captcha_survives_resource_filter(self):
        for url in ("https://idm.swu.edu.cn/am/validate.code", "https://idm.swu.edu.cn/am/validate.code?t=42"):
            route = mock.Mock()
            route.request.resource_type = "image"
            route.request.url = url
            login_browser.route_login_resource(route)
            route.continue_.assert_called_once()
            route.abort.assert_not_called()
        route = mock.Mock()
        route.request.resource_type = "image"
        route.request.url = "https://idm.swu.edu.cn/background.png"
        login_browser.route_login_resource(route)
        route.abort.assert_called_once()

    def test_captcha_uses_rendered_image_without_regenerating_challenge(self):
        page = mock.Mock()
        captcha = mock.Mock()
        captcha.screenshot.return_value = b"rendered-image"
        result = login_browser.get_captcha_image_bytes(page, captcha, 5)
        self.assertEqual(result, b"rendered-image")
        page.request.get.assert_not_called()
        page.wait_for_function.assert_called_once_with(
            "img => img.complete && img.naturalWidth > 0",
            arg=captcha.element_handle.return_value,
            timeout=5000,
        )
        captcha.screenshot.assert_called_once_with(timeout=5000)

    def test_unloaded_captcha_is_reported_as_a_captcha_failure(self):
        page = mock.Mock()
        page.wait_for_function.side_effect = TimeoutError("image not loaded")
        captcha = mock.Mock()
        with self.assertRaises(login_browser.LoginError) as caught:
            login_browser.get_captcha_image_bytes(page, captcha, 5)
        # A captcha that never renders keeps the captcha diagnosis (exit code
        # 7) instead of being reported as an unknown browser failure.
        self.assertEqual(caught.exception.reason, "captcha")
        captcha.screenshot.assert_not_called()
        page.request.get.assert_not_called()

    def test_unified_navigation_http_error_is_retried_then_classified(self):
        for status in (400, 503):
            with self.subTest(status=status):
                page = mock.MagicMock()
                button = page.locator.return_value.first
                button.count.return_value = 1
                page.expect_navigation.return_value.__enter__.return_value.value = mock.Mock(status=status)
                login = mock.Mock()
                login.wait_for.side_effect = TimeoutError("no visible form")
                with (
                    mock.patch.object(login_browser, "recover_from_idm_error_page"),
                    mock.patch.object(login_browser, "click_username_password_tab"),
                    mock.patch.object(login_browser, "login_name_locator", return_value=login),
                    mock.patch.object(login_browser, "password_locator", return_value=mock.Mock()),
                    mock.patch.object(login_browser, "save_login_debug_artifacts"),
                ):
                    with self.assertRaises(login_browser.LoginError) as caught:
                        login_browser.ensure_login_form(page, "offline", 5)
                self.assertEqual(caught.exception.reason, "page_load")
                self.assertEqual(str(caught.exception), f"认证页面返回 HTTP {status}")
                # The front end answers intermittently, so every attempt is
                # used before the failure is reported.
                self.assertEqual(button.click.call_count, 3)
                self.assertEqual(page.expect_navigation.call_count, 3)

    def test_unified_navigation_success_resumes_form_detection(self):
        page = mock.MagicMock()
        page.locator.return_value.first.count.return_value = 1
        page.expect_navigation.return_value.__enter__.return_value.value = mock.Mock(status=200)
        login = mock.Mock()
        login.wait_for.side_effect = [TimeoutError("landing page"), None]
        password = mock.Mock()
        with (
            mock.patch.object(login_browser, "recover_from_idm_error_page"),
            mock.patch.object(login_browser, "click_username_password_tab"),
            mock.patch.object(login_browser, "login_name_locator", return_value=login),
            mock.patch.object(login_browser, "password_locator", return_value=password),
        ):
            login_browser.ensure_login_form(page, "offline", 5)
        self.assertEqual(login.wait_for.call_count, 2)
        password.wait_for.assert_called_once()
        page.expect_navigation.assert_called_once_with(wait_until="domcontentloaded", timeout=5000)

    def test_recovery_reports_http_status_instead_of_aborting(self):
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = "验证失败"
        page.goto.return_value = mock.Mock(status=400)
        outcome = login_browser.recover_from_idm_error_page(
            page, "offline", 5, recovery_url="https://school.invalid/login"
        )
        # The caller keeps its retry budget; the HTTP classification is applied
        # only once the attempts are exhausted.
        self.assertEqual(outcome, 400)

    def test_recovery_http_status_becomes_page_load_after_retries(self):
        page = mock.MagicMock()
        page.locator.return_value.first.count.return_value = 0
        login = mock.Mock()
        login.wait_for.side_effect = TimeoutError("no visible form")
        with (
            mock.patch.object(login_browser, "recover_from_idm_error_page", return_value=503) as recover,
            mock.patch.object(login_browser, "click_username_password_tab"),
            mock.patch.object(login_browser, "login_name_locator", return_value=login),
            mock.patch.object(login_browser, "password_locator", return_value=mock.Mock()),
            mock.patch.object(login_browser, "save_login_debug_artifacts"),
        ):
            with self.assertRaises(login_browser.LoginError) as caught:
                login_browser.ensure_login_form(page, "offline", 5)
        self.assertEqual(caught.exception.reason, "page_load")
        self.assertEqual(str(caught.exception), "认证页面返回 HTTP 503")
        self.assertGreaterEqual(recover.call_count, 3)

    def test_login_error_message_reader_skips_dismissed_dialogs(self):
        # The IDM page only fades its dialog out, so the reader has to filter
        # on visibility instead of trusting whatever text is still in the DOM.
        self.assertIn("getClientRects", login_browser._LOGIN_ERROR_MESSAGE_JS)
        page = mock.Mock()
        page.evaluate.return_value = "验证失败。动态口令验证失败"
        text = login_browser.read_login_error_message(page, 5)
        self.assertIn("动态口令", text)
        page.wait_for_selector.assert_called_once_with(
            login_browser._LOGIN_ERROR_MESSAGE_SELECTOR, state="visible", timeout=2000
        )
        empty = mock.Mock()
        empty.evaluate.return_value = ""
        # The static "用户名密码" tab must not be mistaken for a failure hint.
        empty.locator.return_value.inner_text.return_value = "用户名密码 登录"
        self.assertEqual(login_browser.read_login_error_message(empty, 5), "")

    def test_login_error_message_reader_falls_back_to_page_text(self):
        # Some rejections are plain page text; the reader must still surface a
        # bounded excerpt instead of reporting "no error at all".
        page = mock.Mock()
        page.evaluate.return_value = ""
        page.locator.return_value.inner_text.return_value = "统一认证 验证失败。动态口令验证失败 返回至登录页面"
        text = login_browser.read_login_error_message(page, 5)
        self.assertIn("验证失败", text)
        self.assertLess(len(text), 120)

        quiet = mock.Mock()
        quiet.evaluate.return_value = ""
        quiet.locator.return_value.inner_text.return_value = "用户名密码 登录"
        self.assertEqual(login_browser.read_login_error_message(quiet, 5), "")

    def test_debug_artifact_contains_no_page_or_credentials(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {"SWU_DEBUG_DIR": directory}, clear=False),
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

            login_browser.save_login_debug_artifacts(
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
