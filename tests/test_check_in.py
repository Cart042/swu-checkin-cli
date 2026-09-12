import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dotenv import dotenv_values

import check_in
import checkin_service
import config
import menu
import school_api


class CheckInOfflineTests(unittest.TestCase):
    def test_school_session_is_created_by_direct_session_factory(self):
        session = mock.Mock()
        with (
            mock.patch.object(checkin_service, "create_school_session", return_value=session) as factory,
            mock.patch.object(checkin_service, "get_token", return_value="token"),
            mock.patch.object(checkin_service, "vacation_enable", return_value=False),
            mock.patch.object(checkin_service, "get_transition_today", return_value=None),
        ):
            self.assertEqual(checkin_service.check_in("alice", "password"), 0)
        factory.assert_called_once_with()
        session.close.assert_called_once_with()

    def test_check_in_has_no_legacy_school_proxy_surface(self):
        source = Path(check_in.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_".join(("SWU", "PROXY")), source)
        self.assertNotIn("_".join(("apply", "proxy", "to", "session")), source)

    def test_telegram_channel_requires_token_and_chat_id(self):
        with mock.patch.dict(
            os.environ,
            {"PUSH_TELEGRAM_BOT_TOKEN": "bot-token", "PUSH_TELEGRAM_CHAT_ID": "chat-id"},
            clear=True,
        ):
            self.assertIn("Telegram", config.configured_push_channels())
            self.assertEqual(config.push_configuration_errors(), [])

        with mock.patch.dict(os.environ, {"PUSH_TELEGRAM_BOT_TOKEN": "bot-token"}, clear=True):
            self.assertNotIn("Telegram", config.configured_push_channels())
            self.assertEqual(
                config.push_configuration_errors(),
                ["Telegram 推送缺少 PUSH_TELEGRAM_CHAT_ID。"],
            )

    def test_telegram_menu_sets_and_clears_both_values(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(menu, "prompt_password", return_value="bot-token"),
            mock.patch("builtins.input", side_effect=["1", "chat-id"]),
        ):
            menu.menu_set_telegram(directory)
            self.assertEqual(os.environ["PUSH_TELEGRAM_BOT_TOKEN"], "bot-token")
            self.assertEqual(os.environ["PUSH_TELEGRAM_CHAT_ID"], "chat-id")
            values = dotenv_values(os.path.join(directory, ".env"), interpolate=False)
            self.assertEqual(values["PUSH_TELEGRAM_BOT_TOKEN"], "bot-token")
            self.assertEqual(values["PUSH_TELEGRAM_CHAT_ID"], "chat-id")

            with mock.patch("builtins.input", side_effect=["2", "yes"]):
                menu.menu_set_telegram(directory)
            self.assertNotIn("PUSH_TELEGRAM_BOT_TOKEN", os.environ)
            self.assertNotIn("PUSH_TELEGRAM_CHAT_ID", os.environ)
            values = dotenv_values(os.path.join(directory, ".env"), interpolate=False)
            self.assertNotIn("PUSH_TELEGRAM_BOT_TOKEN", values)
            self.assertNotIn("PUSH_TELEGRAM_CHAT_ID", values)

    def test_dependency_check_uses_metadata_without_importing_module(self):
        sentinel = object()
        with (
            mock.patch.object(check_in.importlib.util, "find_spec", return_value=sentinel) as find_spec,
            mock.patch("builtins.__import__", side_effect=AssertionError("unexpected import")),
        ):
            self.assertEqual(check_in.check_dependency("ddddocr"), (True, None))
        find_spec.assert_called_once_with("ddddocr")

    def test_config_check_is_local_and_network_probe_is_opt_in(self):
        with mock.patch.object(
            school_api,
            "check_school_connectivity",
            side_effect=AssertionError("unexpected network probe"),
        ) as probe:
            check_in.run_config_check(config_dir=tempfile.gettempdir())
        probe.assert_not_called()

        with mock.patch.object(
            school_api,
            "check_school_connectivity",
            return_value=(True, "HTTP 204"),
        ) as probe:
            self.assertEqual(check_in.run_network_check(), 0)
        probe.assert_called_once_with(timeout=5)

    def test_config_check_displays_effective_runtime_values(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {
                    "SWU_MAX_WORKERS": "2",
                    "SWU_MAX_ROUNDS": "4",
                    "SWU_RETRY_INTERVAL_SECONDS": "7",
                    "SWU_RUN_DEADLINE_SECONDS": "11",
                    "SWU_PUSH_DEADLINE_SECONDS": "13",
                },
                clear=True,
            ),
            mock.patch.object(check_in, "check_dependency", return_value=(True, None)),
            mock.patch("builtins.print") as printer,
        ):
            self.assertEqual(
                check_in.run_config_check("alice", "password", config_dir=directory),
                0,
            )

        output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("SWU_MAX_WORKERS=2", output)
        self.assertIn("SWU_MAX_ROUNDS=4", output)
        self.assertIn("SWU_RETRY_INTERVAL_SECONDS=7", output)
        self.assertIn("SWU_RUN_DEADLINE_SECONDS=11", output)
        self.assertIn("SWU_PUSH_DEADLINE_SECONDS=13", output)

    def test_runner_uses_same_run_deadline_value_as_config_check(self):
        calls = []
        with mock.patch.dict(
            os.environ,
            {
                "SWU_MAX_WORKERS": "1",
                "SWU_MAX_ROUNDS": "1",
                "SWU_RETRY_INTERVAL_SECONDS": "1",
                "SWU_RUN_DEADLINE_SECONDS": "11",
            },
        ):

            def fake_checkin(username, password, **kwargs):
                calls.append(kwargs["deadline"])
                return 0

            summary, exit_code, _results = check_in.run_accounts(
                [{"username": "alice", "password": "password"}],
                checkin_func=fake_checkin,
                sleep_func=lambda _seconds: None,
                clock=lambda: 100.0,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("成功: 1 个", summary)
        self.assertEqual(calls, [111.0])

    def test_help_does_not_require_browser_dependencies(self):
        result = subprocess.run(
            [sys.executable, "-S", "check_in.py", "--help"],
            cwd=Path(check_in.__file__).parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check-network", result.stdout)

    def test_validate_accounts_deduplicates_and_preserves_password_spaces(self):
        accounts = config.validate_accounts(
            [
                {"username": "  alice ", "password": "  keep spaces  "},
                {"username": "alice", "password": "different"},
            ]
        )
        self.assertEqual(accounts, [{"username": "alice", "password": "  keep spaces  "}])

    def test_unknown_leave_response_is_business_failure(self):
        response = mock.Mock()
        response.json.return_value = {"code": 500, "msg": "请假服务暂时不可用"}
        with mock.patch.object(checkin_service, "request_with_retry", return_value=response):
            with self.assertRaises(checkin_service.SwuBusinessError):
                checkin_service.vacation_enable("token", 1, object())

    def test_submit_requires_post_verification(self):
        transition = {"formId": "form", "id": "record", "qdzt": "待签到"}
        response = mock.Mock()
        response.json.return_value = {"code": 200, "data": False}
        with (
            mock.patch.object(checkin_service, "_checkin_payload", return_value=({}, "form")),
            mock.patch.object(checkin_service, "request_with_retry", return_value=response),
            mock.patch.object(checkin_service, "get_transition_today", return_value=transition),
        ):
            with self.assertRaises(checkin_service.SwuBusinessError):
                checkin_service.checkin_post("token", 1, object(), transition)

    def test_submit_success_must_match_submitted_record(self):
        transition = {"formId": "form", "id": "record", "qdzt": "待签到"}
        response = mock.Mock()
        response.json.return_value = {"code": 200, "data": None}
        wrong_record = {"formId": "other", "id": "other-record", "qdzt": "已签到"}
        with (
            mock.patch.object(checkin_service, "_checkin_payload", return_value=({}, "form")),
            mock.patch.object(checkin_service, "request_with_retry", return_value=response),
            mock.patch.object(checkin_service, "get_transition_today", return_value=wrong_record),
        ):
            with self.assertRaises(checkin_service.SwuBusinessError):
                checkin_service.checkin_post("token", 1, object(), transition)

    def test_submit_timeout_queries_success_without_resubmitting(self):
        transition = {"formId": "form", "id": "record", "qdzt": "待签到"}
        calls = []

        class TimeoutError(Exception):
            pass

        fake_requests = mock.Mock()
        fake_requests.exceptions.Timeout = TimeoutError
        fake_requests.exceptions.ConnectionError = type("ConnectionError", (Exception,), {})
        response = mock.Mock()
        response.json.return_value = {"code": 200, "data": False}

        def request(*args, **kwargs):
            calls.append((args, kwargs))
            raise TimeoutError("write timeout")

        with (
            mock.patch.object(checkin_service, "requests", fake_requests),
            mock.patch.object(checkin_service, "_checkin_payload", return_value=({}, "form")),
            mock.patch.object(checkin_service, "request_with_retry", side_effect=request),
            mock.patch.object(
                checkin_service,
                "get_transition_today",
                return_value={"formId": "form", "id": "record", "qdzt": "已签到"},
            ),
        ):
            self.assertEqual(checkin_service.checkin_post("token", 1, object(), transition), 1)
        self.assertEqual(len(calls), 1)

    def test_runner_retries_transient_failure_and_returns_zero_when_recovered(self):
        calls = []
        original = {
            key: os.environ.get(key)
            for key in ("SWU_MAX_WORKERS", "SWU_MAX_ROUNDS", "SWU_RETRY_INTERVAL_SECONDS", "SWU_RUN_DEADLINE_SECONDS")
        }
        try:
            os.environ.update(
                {
                    "SWU_MAX_WORKERS": "2",
                    "SWU_MAX_ROUNDS": "2",
                    "SWU_RETRY_INTERVAL_SECONDS": "1",
                    "SWU_RUN_DEADLINE_SECONDS": "30",
                }
            )

            def fake_checkin(username, password, **kwargs):
                calls.append(username)
                return 10 if len(calls) == 1 else 0

            summary, exit_code, results = check_in.run_accounts(
                [{"username": "alice", "password": "pw"}],
                checkin_func=fake_checkin,
                sleep_func=lambda _: None,
            )
            self.assertEqual(exit_code, 0)
            self.assertIn("成功: 1 个，失败: 0 个", summary)
            self.assertEqual(results["alice"][1], True)
            self.assertEqual(len(calls), 2)
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_runner_stops_retry_for_bad_password(self):
        calls = []
        with mock.patch.dict(
            os.environ,
            {
                "SWU_MAX_ROUNDS": "3",
                "SWU_RETRY_INTERVAL_SECONDS": "1",
                "SWU_RUN_DEADLINE_SECONDS": "30",
            },
        ):

            def fake_checkin(username, password, **kwargs):
                calls.append(username)
                return 3

            summary, exit_code, _ = check_in.run_accounts(
                [{"username": "alice", "password": "pw"}],
                checkin_func=fake_checkin,
                sleep_func=lambda _: None,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("账号或密码验证失败", summary)

    def test_runner_only_retries_transient_accounts(self):
        calls = []
        with mock.patch.dict(
            os.environ,
            {
                "SWU_MAX_WORKERS": "2",
                "SWU_MAX_ROUNDS": "2",
                "SWU_RETRY_INTERVAL_SECONDS": "1",
                "SWU_RUN_DEADLINE_SECONDS": "30",
            },
        ):

            def fake_checkin(username, password, **kwargs):
                calls.append(username)
                return 10 if username == "temporary" and calls.count(username) == 1 else 0

            summary, exit_code, results = check_in.run_accounts(
                [
                    {"username": "temporary", "password": "pw"},
                    {"username": "stable", "password": "pw"},
                ],
                checkin_func=fake_checkin,
                sleep_func=lambda _: None,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(calls.count("temporary"), 2)
        self.assertEqual(calls.count("stable"), 1)
        self.assertIn("失败: 0 个", summary)
        self.assertTrue(results["temporary"][1])

    def test_runner_deadline_produces_nonzero_summary(self):
        ticks = [0]

        def clock():
            ticks[0] += 1
            return ticks[0]

        with mock.patch.dict(
            os.environ,
            {
                "SWU_MAX_ROUNDS": "3",
                "SWU_RUN_DEADLINE_SECONDS": "1",
            },
        ):
            summary, exit_code, results = check_in.run_accounts(
                [{"username": "late", "password": "pw"}],
                checkin_func=lambda *args, **kwargs: 0,
                sleep_func=lambda _: None,
                clock=clock,
            )
        self.assertEqual(exit_code, 1)
        self.assertFalse(results["late"][1])
        self.assertIn("deadline", summary)

    def test_atomic_write_uses_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "users.json")
            config._atomic_write_text(path, "secret\n")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "secret\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_env_values_round_trip_special_characters(self):
        value = " leading # hash + dollar $x ${HOME} 'quote' \\tail "
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=False):
            config.set_env_value("TEST_SECRET", value, config_dir=directory)
            env_path = os.path.join(directory, ".env")
            self.assertEqual(dotenv_values(env_path, interpolate=False)["TEST_SECRET"], value)
            self.assertEqual(os.environ["TEST_SECRET"], value)

            config.unset_env_value("TEST_SECRET", config_dir=directory)
            self.assertNotIn("TEST_SECRET", dotenv_values(env_path, interpolate=False))

    def test_os_lock_does_not_delete_or_age_out_active_lock(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(check_in, "CONFIG_DIR", directory):
            first = check_in.acquire_run_lock()
            self.assertIsNotNone(first)
            self.assertEqual(check_in.acquire_run_lock(), None)
            self.assertTrue(os.path.exists(first))
            check_in.release_run_lock(first)
            second = check_in.acquire_run_lock()
            self.assertIsNotNone(second)
            check_in.release_run_lock(second)


if __name__ == "__main__":
    unittest.main()
