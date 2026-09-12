"""登录状态机（入口、跳转恢复与缓存回退）的离线回归测试。"""

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swu_checkin import cache, config
from swu_checkin.auth import flow

AUTH_DIR = Path(flow.__file__).parent


class LoginFlowTests(unittest.TestCase):
    def test_recovery_replays_redirect_over_https(self):
        page = mock.Mock()
        page.url = "https://idm.swu.edu.cn/am/UI/Login"
        page.goto.return_value = mock.Mock(status=200)
        context = mock.Mock()
        context.cookies.return_value = []
        with (
            mock.patch.object(flow, "_drop_federation_cookies") as drop,
            mock.patch.object(flow, "_wait_for_login_result", return_value=True),
        ):
            recovered = flow._recover_blocked_oauth_hop(
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
        with mock.patch.object(flow, "_wait_for_login_result", return_value=True):
            recovered = flow._recover_blocked_oauth_hop(
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

    def test_login_entry_defaults_to_the_ywtb_portal(self):
        portal = "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL"
        environment = {key: value for key, value in os.environ.items() if key != "SWU_LOGIN_ENTRY"}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(flow._login_entry_choice(), "ywtb")
            self.assertEqual(flow._login_entry_url(portal), flow._YWTB_ENTRY_URL)

    def test_login_entry_can_be_switched_back_to_the_portal(self):
        portal = "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL"
        with mock.patch.dict(os.environ, {"SWU_LOGIN_ENTRY": "portal"}):
            self.assertEqual(flow._login_entry_choice(), "portal")
            self.assertEqual(flow._login_entry_url(portal), portal)
        with mock.patch.dict(os.environ, {"SWU_LOGIN_ENTRY": "nonsense"}):
            self.assertEqual(flow._login_entry_choice(), "ywtb")

    def test_entry_login_waits_until_the_credential_hosts_are_left(self):
        entry_page = mock.Mock()
        entry_page.url = "https://ywtb.swu.edu.cn/center-auth-server/index"
        self.assertTrue(flow._login_left_credential_hosts(entry_page))
        self.assertTrue(flow._wait_for_entry_login(entry_page, 5))

        idm_page = mock.Mock()
        idm_page.url = "https://idm.swu.edu.cn/am/UI/Login"
        idm_page.wait_for_function.side_effect = TimeoutError("still on idm")
        self.assertFalse(flow._login_left_credential_hosts(idm_page))
        self.assertFalse(flow._wait_for_entry_login(idm_page, 5))

        cas_page = mock.Mock()
        cas_page.url = "https://uaaap.swu.edu.cn/cas/login"
        self.assertFalse(flow._login_left_credential_hosts(cas_page))

    def test_load_login_entry_retries_a_transient_error_page(self):
        page = mock.Mock()
        page.goto.side_effect = [mock.Mock(status=400), mock.Mock(status=200)]
        flow._load_login_entry(page, "offline", "https://idm.swu.edu.cn/am/UI/Login", 5)
        self.assertEqual(page.goto.call_count, 2)

    def test_load_login_entry_reports_page_load_when_retries_run_out(self):
        page = mock.Mock()
        page.goto.return_value = mock.Mock(status=503)
        with self.assertRaises(flow.LoginError) as caught:
            flow._load_login_entry(page, "offline", "https://idm.swu.edu.cn/am/UI/Login", 5)
        self.assertEqual(caught.exception.reason, "page_load")
        self.assertEqual(str(caught.exception), "认证页面返回 HTTP 503")

    def test_direct_login_surface_and_configuration_are_removed(self):
        sources = [path.read_text(encoding="utf-8") for path in sorted(AUTH_DIR.glob("*.py"))]
        self.assertFalse(any("get_token_direct" in source for source in sources))
        self.assertNotIn("SWU_LOGIN_METHOD", "\n".join(sources))
        self.assertNotIn("from des import", "\n".join(sources))
        self.assertNotIn("_transform_ticket", "\n".join(sources))
        self.assertNotIn("_".join(("SWU", "PROXY")), "\n".join(sources))
        self.assertNotIn('launch_options["proxy"]', "\n".join(sources))
        self.assertNotIn('wait_until="networkidle"', "\n".join(sources))
        # Playwright is heavy, so it must only ever be imported lazily inside a
        # cold-login slot; a top-level import would slow every CLI start.
        for source in sources:
            top_level_imports = [
                node for node in ast.parse(source).body if isinstance(node, (ast.Import, ast.ImportFrom))
            ]
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

    def test_cache_network_failure_is_not_treated_as_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / ".token_cache.json"
            cache.save_cached_token("student", "cached-token", str(cache_path))
            with (
                mock.patch.object(config, "get_config_dir", return_value=directory),
                mock.patch.object(
                    flow,
                    "get_student_id",
                    side_effect=flow.requests.exceptions.ConnectionError("offline"),
                ),
                mock.patch.object(flow, "_browser_login_slot") as browser_slot,
            ):
                with self.assertRaises(flow.requests.exceptions.ConnectionError):
                    flow.get_token("student", "password")
                browser_slot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
