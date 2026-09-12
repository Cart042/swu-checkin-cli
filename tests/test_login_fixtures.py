"""用脱敏历史样本回归登录链的解析与分类。

``tests/fixtures/login/`` 保存的是学校页面的**结构**样本（见该目录的 README）：
学校改版时先替换样本，再运行这些用例，就能立刻看出是选择器、提示文案、
跳转地址还是 Token 形态发生了变化。

这些用例完全不访问学校网络，也不需要 Playwright 或 ddddocr。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swu_checkin import status
from swu_checkin.auth import cookies, debug, errors, pages, tokens, urls

FIXTURES = Path(__file__).parent / "fixtures" / "login"


def load_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class LoginPageShapeTests(unittest.TestCase):
    def test_idm_login_form_is_recognized(self):
        html = load_text("idm_login_form.html")
        # 每个选择器依赖的页面标记；学校改字段名时这条用例会先失败。
        selector_markers = {
            pages.LOGIN_NAME_SELECTOR: "IDToken1",
            pages.PASSWORD_SELECTOR: "IDToken2",
            pages.CAPTCHA_INPUT_SELECTOR: "IDToken3",
            pages.CAPTCHA_IMAGE_SELECTOR: "kaptchaImage",
            pages.SUBMIT_BUTTON_SELECTOR: 'id="button"',
            pages.UNIFIED_LOGIN_BUTTON_SELECTOR: "unified_button",
            pages.USERNAME_PASSWORD_TAB_SELECTOR: "用户名密码",
        }
        for selector, marker in selector_markers.items():
            with self.subTest(selector=selector):
                self.assertIn(marker, html)
        self.assertTrue(pages.has_login_form(html))
        self.assertTrue(pages.has_captcha_image(html))

    def test_blocked_response_is_not_mistaken_for_a_login_form(self):
        html = load_text("waf_400.html")
        self.assertFalse(pages.has_login_form(html))
        self.assertFalse(pages.has_captcha_image(html))

    def test_device_verification_failure_page_is_recognized(self):
        html = load_text("device_verify_failed.html")
        self.assertTrue(pages.is_device_verification_failure(html))
        self.assertIn("返回至登录页面", html)
        for name in (
            "idm_login_form.html",
            "credential_error_popup.html",
            "captcha_error_popup.html",
        ):
            with self.subTest(fixture=name):
                self.assertFalse(pages.is_device_verification_failure(load_text(name)))

    def test_error_popups_are_classified_by_cause(self):
        credential = load_text("credential_error_popup.html")
        captcha = load_text("captcha_error_popup.html")

        self.assertTrue(pages.is_credential_error(credential))
        self.assertFalse(pages.is_captcha_error(credential))

        # 验证码错误的提示里同样可能出现“密码”字样，必须按验证码处理，
        # 否则脚本会把可重试的验证码错误当成凭据错误直接放弃。
        self.assertTrue(pages.is_captcha_error(captcha))
        self.assertFalse(pages.is_credential_error(captcha))

        # 这两个分类函数只接收弹窗文本（运行时的 ``.pop .ctnTxt``），不是整页
        # HTML：登录页本身就带“验证码”字样，不适合作为输入。WAF 页没有任何中文
        # 提示，所以两个分类都必须落空。
        waf = load_text("waf_400.html")
        self.assertFalse(pages.is_credential_error(waf))
        self.assertFalse(pages.is_captcha_error(waf))

        # “动态口令验证失败”整页由它自己的判定函数负责：``recover_from_idm_error_page``
        # 先按设备校验失败重新打开登录入口，不会把它当成普通凭据错误页。
        device_verify = load_text("device_verify_failed.html")
        self.assertTrue(pages.is_device_verification_failure(device_verify))
        self.assertFalse(pages.is_captcha_error(device_verify))


class LoginRedirectTests(unittest.TestCase):
    def test_ticket_is_read_from_query_and_fragment(self):
        redirects = load_json("cas_redirects.json")
        expected_ticket = redirects["expected"]["ticket"]
        for key in ("portal_callback", "portal_callback_hash"):
            with self.subTest(url=key):
                self.assertEqual(
                    urls._find_query_value_from_url(redirects[key], "ticket"),
                    expected_ticket,
                )

    def test_hosts_are_parsed_without_confusing_the_service_parameter(self):
        redirects = load_json("cas_redirects.json")
        expected = redirects["expected"]
        self.assertEqual(urls._url_hostname(redirects["cas_entry"]), expected["cas_entry_host"])
        self.assertEqual(urls._url_hostname(redirects["idm_form"]), expected["idm_form_host"])
        self.assertTrue(urls._is_school_host(redirects["cas_entry"]))
        self.assertTrue(urls._is_school_host(redirects["portal_callback"]))
        # CAS 入口把门户域名放在 service 参数里，必须按主机名判断而不是子串匹配。
        self.assertFalse(urls._is_school_host(redirects["idm_form"]))

    def test_insecure_oauth_hop_is_upgraded_and_replayed(self):
        redirects = load_json("cas_redirects.json")
        # 登录响应里的 ``Location`` 是明文 HTTP 形态，直接回放会丢掉已认证会话，
        # 所以先按响应自身的地址解析、再强制升级到 HTTPS。
        response = mock.Mock(url=redirects["idm_form"], status=302)
        target = urls._absolute_redirect_target(response, redirects["oauth_hop_insecure"])
        self.assertEqual(target, redirects["oauth_hop_secure"])

        page = mock.Mock()
        page.url = redirects["idm_form"]
        context = mock.Mock()
        context.cookies.return_value = []
        with (
            mock.patch("swu_checkin.auth.flow._drop_federation_cookies"),
            mock.patch("swu_checkin.auth.flow._wait_for_login_result", return_value=True),
        ):
            recovered = _recover_hop(page, context, target)
        self.assertTrue(recovered)
        self.assertEqual(page.goto.call_args[0][0], redirects["oauth_hop_secure"])

    def test_login_success_requires_a_ticket_or_token_on_a_school_host(self):
        redirects = load_json("cas_redirects.json")
        local_storage = json.dumps(load_json("portal_localstorage.json"), ensure_ascii=False)

        page = mock.Mock()
        page.url = redirects["cas_entry"]
        page.evaluate.return_value = "{}"
        self.assertFalse(tokens._login_success_detected(page, redirects["cas_entry"]))

        page.url = redirects["portal_callback"]
        self.assertTrue(tokens._login_success_detected(page, redirects["cas_entry"]))

        page.url = redirects["idm_form"]
        page.evaluate.return_value = local_storage
        self.assertFalse(tokens._login_success_detected(page, redirects["cas_entry"]))


class LoginTokenPayloadTests(unittest.TestCase):
    def test_exchange_token_payload_shapes(self):
        success = load_json("exchange_token_success.json")
        failure = load_json("exchange_token_failure.json")
        expected = success["expected_token"]
        for key in ("bare_string", "data_wrapper", "loose_key"):
            with self.subTest(shape=key):
                self.assertEqual(tokens.token_from_exchange_payload(success[key]), expected)
        for key in ("blocked", "unexpected_shape"):
            with self.subTest(shape=key):
                self.assertIsNone(tokens.token_from_exchange_payload(failure[key]))

    def test_local_storage_prefers_the_access_token(self):
        fixture = load_json("portal_localstorage.json")
        self.assertEqual(tokens._token_from_local_storage(fixture), fixture["expected_token"])
        # 同样的导出经过 JSON.stringify 往返后仍然解析正确。
        self.assertEqual(
            tokens._token_from_local_storage(json.dumps(fixture, ensure_ascii=False)),
            fixture["expected_token"],
        )


class LoginCookieFixtureTests(unittest.TestCase):
    def test_only_login_host_device_cookies_are_dropped(self):
        fixture = load_json("federation_cookies.json")
        context = mock.Mock()
        context.cookies.return_value = fixture["cookies"]

        self.assertTrue(cookies._drop_federation_cookies(context, fixture["login_host"], "example"))

        # 清除是按 name+domain 精确下发的，而不是重建整个 Cookie 罐：任何一次删除
        # 失败都不会让浏览器丢掉会话 Cookie。
        dropped = sorted((call.kwargs["domain"], call.kwargs["name"]) for call in context.clear_cookies.call_args_list)
        self.assertEqual(
            dropped,
            [
                ("idm.swu.edu.cn", "0000deviceA00"),
                ("idm.swu.edu.cn", "0000deviceB00"),
            ],
        )
        kept_on_login_host = {
            cookie["name"]
            for cookie in fixture["cookies"]
            if cookie["domain"] == fixture["login_host"] and (cookie["domain"], cookie["name"]) not in set(dropped)
        }
        self.assertEqual(sorted(kept_on_login_host), sorted(fixture["expected_kept_on_login_host"]))
        # 另一个主机上的同名 Cookie 也必须保留。
        self.assertNotIn(("uaaap.swu.edu.cn", "0000deviceA00"), set(dropped))
        context.add_cookies.assert_not_called()

    def test_device_cookie_name_shape_matches_the_fixture(self):
        fixture = load_json("federation_cookies.json")
        dropped = [
            cookie
            for cookie in fixture["cookies"]
            if cookie["domain"] == fixture["login_host"] and cookies._OPAQUE_COOKIE_NAME.match(cookie["name"])
        ]
        self.assertEqual(
            [cookie["name"] for cookie in dropped],
            fixture["expected_dropped"],
        )


class LoginRedactionTests(unittest.TestCase):
    def test_debug_artifact_never_contains_fixture_secrets(self):
        redirects = load_json("cas_redirects.json")
        ticket = redirects["expected"]["ticket"]
        token = load_json("exchange_token_success.json")["expected_token"]

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {"SWU_DEBUG_DIR": directory}, clear=False),
        ):

            class Page:
                url = redirects["portal_callback"]

                def title(self):
                    return token

                def locator(self, _selector):
                    class Locator:
                        def count(self):
                            return 1

                    return Locator()

            debug.save_login_debug_artifacts(
                Page(),
                "example",
                "credential",
                RuntimeError(f"ticket={ticket} access_token={token}"),
            )

            files = list(Path(directory).iterdir())
            self.assertEqual([path.suffix for path in files], [".txt"])
            text = files[0].read_text(encoding="utf-8")

        self.assertNotIn(ticket, text)
        self.assertNotIn(token, text)
        self.assertIn("credential", text)


class LoginReasonStatusTests(unittest.TestCase):
    def test_every_reason_maps_to_a_documented_status(self):
        expected = {
            errors.FailureReason.CREDENTIAL: 3,
            errors.FailureReason.WAF_BLOCKED: 6,
            errors.FailureReason.PAGE_LOAD: 6,
            errors.FailureReason.CAPTCHA: 7,
            errors.FailureReason.TOKEN_EXTRACT: 8,
            errors.FailureReason.LOGIN_PAGE_CHANGED: 9,
        }
        for reason, code in expected.items():
            with self.subTest(reason=reason):
                self.assertEqual(status.LOGIN_REASON_STATUS[reason], code)
                self.assertIn(code, status.STATUS_MESSAGES)

        # StrEnum 的成员可以继续按字符串查询，历史调用方不需要修改。
        self.assertEqual(status.LOGIN_REASON_STATUS["captcha"], 7)
        self.assertEqual(status.LOGIN_REASON_STATUS.get("unknown", 11), 11)


def _recover_hop(page, context, redirect_target):
    """调用登录流的 OAuth 补跳函数，保持用例只依赖 fixture 数据。"""

    from swu_checkin.auth.flow import _recover_blocked_oauth_hop

    return _recover_blocked_oauth_hop(
        page,
        context,
        "example",
        "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL",
        redirect_target,
        5,
    )


if __name__ == "__main__":
    unittest.main()
