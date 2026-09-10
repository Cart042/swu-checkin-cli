import os
import stat
import tempfile
import unittest
from unittest import mock

import check_in
from dotenv import dotenv_values


class CheckInOfflineTests(unittest.TestCase):
    def test_validate_accounts_deduplicates_and_preserves_password_spaces(self):
        accounts = check_in.validate_accounts([
            {"username": "  alice ", "password": "  keep spaces  "},
            {"username": "alice", "password": "different"},
        ])
        self.assertEqual(accounts, [{"username": "alice", "password": "  keep spaces  "}])

    def test_unknown_leave_response_is_business_failure(self):
        response = mock.Mock()
        response.json.return_value = {"code": 500, "msg": "请假服务暂时不可用"}
        with mock.patch.object(check_in, "request_with_retry", return_value=response):
            with self.assertRaises(check_in.SwuBusinessError):
                check_in.vacation_enable("token", 1, object())

    def test_submit_requires_post_verification(self):
        transition = {"formId": "form", "id": "record", "qdzt": "待签到"}
        response = mock.Mock()
        response.json.return_value = {"code": 200, "data": False}
        with mock.patch.object(check_in, "_checkin_payload", return_value=({}, "form")), \
             mock.patch.object(check_in, "request_with_retry", return_value=response), \
             mock.patch.object(check_in, "get_transition_today", return_value=transition):
            with self.assertRaises(check_in.SwuBusinessError):
                check_in.checkin_post("token", 1, object(), transition)

    def test_submit_success_must_match_submitted_record(self):
        transition = {"formId": "form", "id": "record", "qdzt": "待签到"}
        response = mock.Mock()
        response.json.return_value = {"code": 200, "data": None}
        wrong_record = {"formId": "other", "id": "other-record", "qdzt": "已签到"}
        with mock.patch.object(check_in, "_checkin_payload", return_value=({}, "form")), \
             mock.patch.object(check_in, "request_with_retry", return_value=response), \
             mock.patch.object(check_in, "get_transition_today", return_value=wrong_record):
            with self.assertRaises(check_in.SwuBusinessError):
                check_in.checkin_post("token", 1, object(), transition)

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

        with mock.patch.object(check_in, "requests", fake_requests), \
             mock.patch.object(check_in, "_checkin_payload", return_value=({}, "form")), \
             mock.patch.object(check_in, "request_with_retry", side_effect=request), \
             mock.patch.object(check_in, "get_transition_today", return_value={"formId": "form", "id": "record", "qdzt": "已签到"}):
            self.assertEqual(check_in.checkin_post("token", 1, object(), transition), 1)
        self.assertEqual(len(calls), 1)

    def test_runner_retries_transient_failure_and_returns_zero_when_recovered(self):
        calls = []
        original = {key: os.environ.get(key) for key in (
            "SWU_MAX_WORKERS", "SWU_MAX_ROUNDS", "SWU_RETRY_INTERVAL_SECONDS", "SWU_RUN_DEADLINE_SECONDS"
        )}
        try:
            os.environ.update({
                "SWU_MAX_WORKERS": "2",
                "SWU_MAX_ROUNDS": "2",
                "SWU_RETRY_INTERVAL_SECONDS": "1",
                "SWU_RUN_DEADLINE_SECONDS": "30",
            })

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
        with mock.patch.dict(os.environ, {
            "SWU_MAX_ROUNDS": "3",
            "SWU_RETRY_INTERVAL_SECONDS": "1",
            "SWU_RUN_DEADLINE_SECONDS": "30",
        }):
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
        with mock.patch.dict(os.environ, {
            "SWU_MAX_WORKERS": "2",
            "SWU_MAX_ROUNDS": "2",
            "SWU_RETRY_INTERVAL_SECONDS": "1",
            "SWU_RUN_DEADLINE_SECONDS": "30",
        }):
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

        with mock.patch.dict(os.environ, {
            "SWU_MAX_ROUNDS": "3",
            "SWU_RUN_DEADLINE_SECONDS": "1",
        }):
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
            check_in._atomic_write_text(path, "secret\n")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "secret\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_env_values_round_trip_special_characters(self):
        value = " leading # hash + dollar $x ${HOME} 'quote' \\tail "
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            check_in, "CONFIG_DIR", directory
        ), mock.patch.dict(os.environ, {}, clear=False):
            check_in.set_env_value("SWU_PROXY_PASSWORD", value)
            env_path = os.path.join(directory, ".env")
            self.assertEqual(dotenv_values(env_path, interpolate=False)["SWU_PROXY_PASSWORD"], value)
            self.assertEqual(os.environ["SWU_PROXY_PASSWORD"], value)

            check_in.unset_env_value("SWU_PROXY_PASSWORD")
            self.assertNotIn("SWU_PROXY_PASSWORD", dotenv_values(env_path, interpolate=False))

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
