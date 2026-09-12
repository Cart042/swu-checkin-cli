"""Offline regression tests for the browser login/API boundary."""

import ast
import json
import os
import stat
import sys
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
    def test_login_user_agent_hides_headless_marker(self):
        browser = mock.Mock(version="151.0.7922.34")
        user_agent = get_info._login_user_agent(browser)
        self.assertNotIn("Headless", user_agent)
        self.assertIn("Chrome/151.0.0.0", user_agent)
        # The platform token must agree with the host, because navigator.platform
        # and sec-ch-ua-platform keep reporting the real operating system.
        self.assertIn(get_info._platform_user_agent_token(), user_agent)
        self.assertEqual(
            get_info._platform_user_agent_token(),
            get_info._PLATFORM_UA_TOKENS.get(sys.platform, get_info._PLATFORM_UA_TOKENS["win32"]),
        )

    def test_login_user_agent_falls_back_instead_of_going_headless(self):
        environment = {key: value for key, value in os.environ.items() if key != "SWU_LOGIN_UA"}
        with mock.patch.dict(os.environ, environment, clear=True):
            user_agent = get_info._login_user_agent(mock.Mock(version="unknown"))
        # Falling back to the browser default would restore the HeadlessChrome
        # string that the school's front end rejects.
        self.assertEqual(user_agent, get_info._FALLBACK_USER_AGENT)
        self.assertNotIn("Headless", user_agent)

    def test_login_user_agent_honours_explicit_override(self):
        with mock.patch.dict(os.environ, {"SWU_LOGIN_UA": "pinned-agent/1.0"}):
            self.assertEqual(
                get_info._login_user_agent(mock.Mock(version="151.0.7922.34")),
                "pinned-agent/1.0",
            )

    def test_drop_federation_cookies_only_touches_login_host(self):
        context = mock.Mock()
        context.cookies.return_value = [
            {"name": "61zqTsrO93nzO", "value": "a", "domain": "idm.swu.edu.cn", "path": "/"},
            {"name": "61zqTsrO93nzP", "value": "b", "domain": "idm.swu.edu.cn", "path": "/"},
            {"name": "SESSION", "value": "c", "domain": "idm.swu.edu.cn", "path": "/am"},
            {"name": "61zqTsrO93nzO", "value": "d", "domain": "uaaap.swu.edu.cn", "path": "/"},
            {"name": "iPlanetDirectoryPro", "value": "e", "domain": "idm.swu.edu.cn", "path": "/"},
        ]
        dropped = get_info._drop_federation_cookies(context, "idm.swu.edu.cn", "offline")
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
        dropped = get_info._drop_federation_cookies(context, "idm.swu.edu.cn", "offline")
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
        self.assertFalse(get_info._drop_federation_cookies(context, "idm.swu.edu.cn", "offline"))
        context.add_cookies.assert_not_called()

    def test_drop_federation_cookies_ignores_other_hosts(self):
        context = mock.Mock()
        context.cookies.return_value = [
            {"name": "61zqTsrO93nzO", "value": "a", "domain": "uaaap.swu.edu.cn", "path": "/"},
        ]
        self.assertTrue(get_info._drop_federation_cookies(context, "idm.swu.edu.cn", "offline"))
        context.clear_cookies.assert_not_called()
        context.add_cookies.assert_not_called()

    def test_recovery_replays_redirect_over_https(self):
        page = mock.Mock()
        page.url = "https://idm.swu.edu.cn/am/UI/Login"
        page.goto.return_value = mock.Mock(status=200)
        context = mock.Mock()
        context.cookies.return_value = []
        with (
            mock.patch.object(get_info, "_drop_federation_cookies") as drop,
            mock.patch.object(get_info, "_wait_for_login_result", return_value=True),
        ):
            recovered = get_info._recover_blocked_oauth_hop(
                page,
                context,
                "offline",
                "https://uaaap.swu.edu.cn/cas/login",
                "https://idm.swu.edu.cn/am/oauth2/authorize?service=initService",
                5,
            )
        self.assertTrue(recovered)
        self.assertEqual(
            page.goto.call_args[0][0],
            "https://idm.swu.edu.cn/am/oauth2/authorize?service=initService",
        )
        drop.assert_called_once()

    def test_recover_blocked_hop_runs_without_a_location_target(self):
        page = mock.Mock()
        page.url = "https://idm.swu.edu.cn/am/UI/Login"
        page.goto.return_value = mock.Mock(status=200)
        context = mock.Mock()
        context.cookies.return_value = []
        with mock.patch.object(get_info, "_wait_for_login_result", return_value=True):
            recovered = get_info._recover_blocked_oauth_hop(
                page,
                context,
                "offline",
                "https://uaaap.swu.edu.cn/cas/login",
                None,
                5,
            )
        # A login response without a usable Location used to make this fallback
        # unreachable; it now re-drives the address the page already sits on.
        self.assertTrue(recovered)
        self.assertEqual(page.goto.call_args[0][0], "https://idm.swu.edu.cn/am/UI/Login")

    def test_absolute_redirect_target_resolves_and_upgrades(self):
        response = mock.Mock(url="https://idm.swu.edu.cn/am/UI/Login", status=302)
        self.assertEqual(
            get_info._absolute_redirect_target(
                response, "http://idm.swu.edu.cn/am/oauth2/authorize?service=initService"
            ),
            "https://idm.swu.edu.cn/am/oauth2/authorize?service=initService",
        )
        self.assertEqual(
            get_info._absolute_redirect_target(response, "/am/oauth2/authorize?service=initService"),
            "https://idm.swu.edu.cn/am/oauth2/authorize?service=initService",
        )
        self.assertEqual(
            get_info._absolute_redirect_target(response, "https://portal.swu.edu.cn/next"),
            "https://portal.swu.edu.cn/next",
        )
        self.assertIsNone(get_info._absolute_redirect_target(response, ""))
        self.assertIsNone(get_info._absolute_redirect_target(response, None))

    def test_real_idm_captcha_survives_resource_filter(self):
        for url in ("https://idm.swu.edu.cn/am/validate.code", "https://idm.swu.edu.cn/am/validate.code?t=42"):
            route = mock.Mock()
            route.request.resource_type = "image"
            route.request.url = url
            get_info.route_login_resource(route)
            route.continue_.assert_called_once()
            route.abort.assert_not_called()
        route = mock.Mock()
        route.request.resource_type = "image"
        route.request.url = "https://idm.swu.edu.cn/background.png"
        get_info.route_login_resource(route)
        route.abort.assert_called_once()

    def test_captcha_uses_rendered_image_without_regenerating_challenge(self):
        page = mock.Mock()
        captcha = mock.Mock()
        captcha.screenshot.return_value = b"rendered-image"
        result = get_info.get_captcha_image_bytes(page, captcha, 5)
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
        with self.assertRaises(get_info.LoginError) as caught:
            get_info.get_captcha_image_bytes(page, captcha, 5)
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
                    mock.patch.object(get_info, "recover_from_idm_error_page"),
                    mock.patch.object(get_info, "click_username_password_tab"),
                    mock.patch.object(get_info, "login_name_locator", return_value=login),
                    mock.patch.object(get_info, "password_locator", return_value=mock.Mock()),
                    mock.patch.object(get_info, "save_login_debug_artifacts"),
                ):
                    with self.assertRaises(get_info.LoginError) as caught:
                        get_info.ensure_login_form(page, "offline", 5)
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
            mock.patch.object(get_info, "recover_from_idm_error_page"),
            mock.patch.object(get_info, "click_username_password_tab"),
            mock.patch.object(get_info, "login_name_locator", return_value=login),
            mock.patch.object(get_info, "password_locator", return_value=password),
        ):
            get_info.ensure_login_form(page, "offline", 5)
        self.assertEqual(login.wait_for.call_count, 2)
        password.wait_for.assert_called_once()
        page.expect_navigation.assert_called_once_with(wait_until="domcontentloaded", timeout=5000)

    def test_recovery_reports_http_status_instead_of_aborting(self):
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = "验证失败"
        page.goto.return_value = mock.Mock(status=400)
        outcome = get_info.recover_from_idm_error_page(page, "offline", 5, recovery_url="https://school.invalid/login")
        # The caller keeps its retry budget; the HTTP classification is applied
        # only once the attempts are exhausted.
        self.assertEqual(outcome, 400)

    def test_recovery_http_status_becomes_page_load_after_retries(self):
        page = mock.MagicMock()
        page.locator.return_value.first.count.return_value = 0
        login = mock.Mock()
        login.wait_for.side_effect = TimeoutError("no visible form")
        with (
            mock.patch.object(get_info, "recover_from_idm_error_page", return_value=503) as recover,
            mock.patch.object(get_info, "click_username_password_tab"),
            mock.patch.object(get_info, "login_name_locator", return_value=login),
            mock.patch.object(get_info, "password_locator", return_value=mock.Mock()),
            mock.patch.object(get_info, "save_login_debug_artifacts"),
        ):
            with self.assertRaises(get_info.LoginError) as caught:
                get_info.ensure_login_form(page, "offline", 5)
        self.assertEqual(caught.exception.reason, "page_load")
        self.assertEqual(str(caught.exception), "认证页面返回 HTTP 503")
        self.assertGreaterEqual(recover.call_count, 3)

    def test_http_status_helpers_track_and_classify(self):
        self.assertEqual(get_info._remember_http_status(503, None), 503)
        self.assertEqual(get_info._remember_http_status(400, 503), 400)
        self.assertEqual(get_info._remember_http_status(None, 503), 503)
        self.assertIsNone(get_info._remember_http_status(200, None))
        self.assertIsNone(get_info._remember_http_status(mock.Mock(), None))
        self.assertEqual(get_info._login_failure_reason(400), "waf_blocked")
        self.assertEqual(get_info._login_failure_reason(200), "captcha")
        self.assertIsNone(get_info._http_status(mock.Mock()))
        self.assertEqual(get_info._http_status(mock.Mock(status=200)), 200)

    def test_require_ok_http_status_marks_errors_as_retryable(self):
        get_info._require_ok_http_status(None)
        get_info._require_ok_http_status(200)
        with self.assertRaises(get_info._RetryableHttpError) as caught:
            get_info._require_ok_http_status(503)
        self.assertEqual(caught.exception.status, 503)

    def test_login_entry_defaults_to_the_ywtb_portal(self):
        portal = "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL"
        environment = {key: value for key, value in os.environ.items() if key != "SWU_LOGIN_ENTRY"}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(get_info._login_entry_choice(), "ywtb")
            self.assertEqual(get_info._login_entry_url(portal), get_info._YWTB_ENTRY_URL)

    def test_login_entry_can_be_switched_back_to_the_portal(self):
        portal = "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL"
        with mock.patch.dict(os.environ, {"SWU_LOGIN_ENTRY": "portal"}):
            self.assertEqual(get_info._login_entry_choice(), "portal")
            self.assertEqual(get_info._login_entry_url(portal), portal)
        with mock.patch.dict(os.environ, {"SWU_LOGIN_ENTRY": "nonsense"}):
            self.assertEqual(get_info._login_entry_choice(), "ywtb")

    def test_entry_login_waits_until_the_credential_hosts_are_left(self):
        entry_page = mock.Mock()
        entry_page.url = "https://ywtb.swu.edu.cn/center-auth-server/index"
        self.assertTrue(get_info._login_left_credential_hosts(entry_page))
        self.assertTrue(get_info._wait_for_entry_login(entry_page, 5))

        idm_page = mock.Mock()
        idm_page.url = "https://idm.swu.edu.cn/am/UI/Login"
        idm_page.wait_for_function.side_effect = TimeoutError("still on idm")
        self.assertFalse(get_info._login_left_credential_hosts(idm_page))
        self.assertFalse(get_info._wait_for_entry_login(idm_page, 5))

        cas_page = mock.Mock()
        cas_page.url = "https://uaaap.swu.edu.cn/cas/login"
        self.assertFalse(get_info._login_left_credential_hosts(cas_page))

    def test_load_login_entry_retries_a_transient_error_page(self):
        page = mock.Mock()
        page.goto.side_effect = [mock.Mock(status=400), mock.Mock(status=200)]
        get_info._load_login_entry(page, "offline", "https://idm.swu.edu.cn/am/UI/Login", 5)
        self.assertEqual(page.goto.call_count, 2)

    def test_load_login_entry_reports_page_load_when_retries_run_out(self):
        page = mock.Mock()
        page.goto.return_value = mock.Mock(status=503)
        with self.assertRaises(get_info.LoginError) as caught:
            get_info._load_login_entry(page, "offline", "https://idm.swu.edu.cn/am/UI/Login", 5)
        self.assertEqual(caught.exception.reason, "page_load")
        self.assertEqual(str(caught.exception), "认证页面返回 HTTP 503")

    def test_login_error_message_reader_skips_dismissed_dialogs(self):
        # The IDM page only fades its dialog out, so the reader has to filter
        # on visibility instead of trusting whatever text is still in the DOM.
        self.assertIn("getClientRects", get_info._LOGIN_ERROR_MESSAGE_JS)
        page = mock.Mock()
        page.evaluate.return_value = "验证失败。动态口令验证失败"
        text = get_info.read_login_error_message(page, 5)
        self.assertIn("动态口令", text)
        page.wait_for_selector.assert_called_once_with(
            get_info._LOGIN_ERROR_MESSAGE_SELECTOR, state="visible", timeout=2000
        )
        empty = mock.Mock()
        empty.evaluate.return_value = ""
        # The static "用户名密码" tab must not be mistaken for a failure hint.
        empty.locator.return_value.inner_text.return_value = "用户名密码 登录"
        self.assertEqual(get_info.read_login_error_message(empty, 5), "")

    def test_login_error_message_reader_falls_back_to_page_text(self):
        # Some rejections are plain page text; the reader must still surface a
        # bounded excerpt instead of reporting "no error at all".
        page = mock.Mock()
        page.evaluate.return_value = ""
        page.locator.return_value.inner_text.return_value = "统一认证 验证失败。动态口令验证失败 返回至登录页面"
        text = get_info.read_login_error_message(page, 5)
        self.assertIn("验证失败", text)
        self.assertLess(len(text), 120)

        quiet = mock.Mock()
        quiet.evaluate.return_value = ""
        quiet.locator.return_value.inner_text.return_value = "用户名密码 登录"
        self.assertEqual(get_info.read_login_error_message(quiet, 5), "")

    def test_direct_login_surface_and_configuration_are_removed(self):
        source = Path(get_info.__file__).read_text(encoding="utf-8")
        self.assertFalse(hasattr(get_info, "get_token_direct"))
        self.assertNotIn("SWU_LOGIN_METHOD", source)
        self.assertNotIn("from des import", source)
        self.assertNotIn("_transform_ticket", source)
        self.assertNotIn("_".join(("SWU", "PROXY")), source)
        self.assertNotIn('launch_options["proxy"]', source)
        self.assertNotIn('wait_until="networkidle"', source)
        tree = ast.parse(source)
        top_level_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertFalse(
            any(
                (node.module or "").startswith("playwright")
                for node in top_level_imports
                if isinstance(node, ast.ImportFrom)
            )
        )
        self.assertFalse(
            any(
                alias.name.startswith("playwright")
                for node in top_level_imports
                if isinstance(node, ast.Import)
                for alias in node.names
            )
        )

    def test_school_session_ignores_proxy_environment_and_has_no_proxy_map(self):
        class Session:
            def __init__(self):
                self.trust_env = True
                self.proxies = {"https": "http://must-not-be-used.invalid"}

        session = Session()
        with (
            mock.patch.object(get_info.requests, "Session", return_value=session),
            mock.patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://must-not-be-used.invalid", "ALL_PROXY": "http://must-not-be-used.invalid"},
                clear=False,
            ),
        ):
            direct = get_info.create_school_session()
        self.assertIs(direct, session)
        self.assertFalse(direct.trust_env)
        self.assertEqual(direct.proxies, {})

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
        self.assertFalse(get_info._login_success_detected(page, cas_url))
        page.url = "https://of.swu.edu.cn/gateway/resolve-cas-return?ticket=one-time"
        self.assertTrue(get_info._login_success_detected(page, cas_url))

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
        self.assertTrue(get_info._wait_for_login_result(page, "https://of.swu.edu.cn/cas/login", 5))
        self.assertEqual(len(page.calls), 1)
        self.assertIn("arg", page.calls[0][1])
        self.assertNotIn("networkidle", page.calls[0][0])

    def test_local_storage_prefers_access_token_over_other_token_values(self):
        payload = {
            "refresh_token": "refresh-token",
            "auth": "auth-value",
            "access_token": "access-token",
        }
        self.assertEqual(get_info._token_from_local_storage(payload), "access-token")

    def test_invalid_new_token_is_not_cached(self):
        with (
            mock.patch.object(
                get_info,
                "get_student_id",
                side_effect=get_info.TokenInvalidError("invalid"),
            ),
            mock.patch.object(get_info, "_save_cached_token") as save,
        ):
            with self.assertRaises(get_info.TokenInvalidError):
                get_info._validate_and_cache_token(
                    "student",
                    "candidate-token",
                    "/tmp/unused-token-cache.json",
                    5,
                    object(),
                    None,
                )
        save.assert_not_called()

    def test_write_request_is_never_retried(self):
        session = FakeSession([FakeResponse(500), FakeResponse(200)])
        with mock.patch.object(get_info.time, "sleep", side_effect=AssertionError("write request slept")):
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
            response = get_info.request_with_retry("GET", "https://school.invalid/read", session=session)
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
            token = get_info.exchange_token_from_browser_page(page, "ticket-secret", timeout=15, deadline=20.0)
        self.assertEqual(token, "ticket-token")
        self.assertEqual(page.calls[0][1]["timeoutMs"], 10000)
        self.assertIn("AbortController", page.calls[0][0])

    def test_http_401_is_token_invalid(self):
        session = FakeSession([FakeResponse(401, {"message": "expired"})])
        with self.assertRaises(get_info.TokenInvalidError) as caught:
            get_info.request_with_retry("GET", "https://school.invalid/user", session=session)
        self.assertEqual(caught.exception.status_code, 401)

    def test_cache_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "nested" / ".token_cache.json"
            get_info._save_cached_token("student", "secret-token", str(cache_path))

            self.assertEqual(get_info._load_cached_token("student", str(cache_path)), "secret-token")
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
            with (
                mock.patch.object(get_info, "CONFIG_DIR", directory),
                mock.patch.object(
                    get_info,
                    "get_student_id",
                    side_effect=get_info.requests.exceptions.ConnectionError("offline"),
                ),
                mock.patch.object(get_info, "_browser_login_slot") as browser_slot,
            ):
                with self.assertRaises(get_info.requests.exceptions.ConnectionError):
                    get_info.get_token("student", "password")
                browser_slot.assert_not_called()

    def test_student_id_is_reused_for_one_session(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"code": 200, "data": {"subject": {"username": "student-id"}}},
                )
            ]
        )
        self.assertEqual(get_info.get_student_id("candidate-token", session=session), "student-id")
        self.assertEqual(get_info.get_student_id("candidate-token", session=session), "student-id")
        self.assertEqual(len(session.calls), 1)

    def test_student_id_cache_is_scoped_to_session(self):
        response = FakeResponse(
            200,
            {"code": 200, "data": {"subject": {"username": "student-id"}}},
        )
        first = FakeSession([response])
        second = FakeSession([response])
        self.assertEqual(get_info.get_student_id("candidate-token", session=first), "student-id")
        self.assertEqual(get_info.get_student_id("candidate-token", session=second), "student-id")
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)

    def test_student_id_cache_is_keyed_by_token(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"code": 200, "data": {"subject": {"username": "student-a"}}},
                ),
                FakeResponse(
                    200,
                    {"code": 200, "data": {"subject": {"username": "student-b"}}},
                ),
            ]
        )
        self.assertEqual(get_info.get_student_id("token-a", session=session), "student-a")
        self.assertEqual(get_info.get_student_id("token-b", session=session), "student-b")
        self.assertEqual(len(session.calls), 2)

    def test_failed_student_id_response_is_not_cached(self):
        valid_response = FakeResponse(
            200,
            {"code": 200, "data": {"subject": {"username": "student-id"}}},
        )
        cases = (
            (FakeResponse(401, {"message": "expired"}), get_info.TokenInvalidError),
            (FakeResponse(200, {"code": 500, "message": "temporary failure"}), get_info.SwuBusinessError),
        )
        for failed_response, error_type in cases:
            with self.subTest(error_type=error_type.__name__):
                session = FakeSession([failed_response, valid_response])
                with self.assertRaises(error_type):
                    get_info.get_student_id("token", session=session)
                self.assertEqual(get_info.get_student_id("token", session=session), "student-id")
                self.assertEqual(len(session.calls), 2)

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
