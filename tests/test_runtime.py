"""Offline tests for bounded runtime resources."""

import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

from swu_checkin import runner
from swu_checkin.auth import browser, captcha
from swu_checkin.status import CheckinStatus


class RuntimeResourceTests(unittest.TestCase):
    def test_ocr_is_initialized_once_and_classification_is_mutually_exclusive(self):
        state = {"initializations": 0, "active": 0, "max_active": 0}
        state_lock = threading.Lock()

        class FakeOcr:
            def __init__(self, *, show_ad):
                self.show_ad = show_ad
                with state_lock:
                    state["initializations"] += 1

            def classification(self, image_bytes):
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                try:
                    time.sleep(0.01)
                    return image_bytes.decode()
                finally:
                    with state_lock:
                        state["active"] -= 1

        fake_module = types.SimpleNamespace(DdddOcr=FakeOcr)
        original_ocr = captcha._ocr_instance
        try:
            captcha._ocr_instance = None
            with mock.patch.dict(sys.modules, {"ddddocr": fake_module}):
                results = []
                threads = [
                    threading.Thread(target=lambda: results.append(captcha.classify_captcha(b"ABCD"))) for _ in range(8)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(state["initializations"], 1)
            self.assertEqual(state["max_active"], 1)
            self.assertEqual(results, ["ABCD"] * 8)
        finally:
            captcha._ocr_instance = original_ocr

    def test_browser_login_slot_releases_on_error(self):
        semaphore = threading.BoundedSemaphore(1)
        with mock.patch.object(browser, "_browser_login_semaphore", semaphore):
            with self.assertRaises(RuntimeError):
                with browser._browser_login_slot():
                    raise RuntimeError("login failed")
            acquired = semaphore.acquire(blocking=False)
            self.assertTrue(acquired)
            semaphore.release()

    def test_browser_login_acquire_passes_deadline_and_does_not_bypass_it(self):
        class NeverAvailableSemaphore:
            def __init__(self):
                self.timeout = None

            def acquire(self, *, timeout=None):
                self.timeout = timeout
                return False

            def release(self):
                raise AssertionError("a failed acquire must not release")

        semaphore = NeverAvailableSemaphore()
        with (
            mock.patch.object(browser, "_browser_login_semaphore", semaphore),
            mock.patch.object(browser, "_remaining_seconds", return_value=0.25),
        ):
            with self.assertRaises(browser.DeadlineExceeded):
                browser._acquire_browser_login(deadline=123.0)
        self.assertEqual(semaphore.timeout, 0.25)

    def test_retry_rounds_reuse_one_executor(self):
        calls = []

        def validate(accounts):
            return accounts

        def checkin(username, password, **_kwargs):
            calls.append(username)
            return 10 if calls.count(username) == 1 else 0

        with (
            mock.patch.dict(
                os.environ,
                {
                    "SWU_MAX_WORKERS": "2",
                    "SWU_MAX_ROUNDS": "2",
                    "SWU_RETRY_INTERVAL_SECONDS": "1",
                    "SWU_RUN_DEADLINE_SECONDS": "30",
                },
                clear=False,
            ),
            mock.patch.object(runner, "ThreadPoolExecutor", wraps=runner.ThreadPoolExecutor) as executor,
        ):
            summary, exit_code, results = runner.run_accounts(
                [{"username": "alice", "password": "pw"}],
                checkin_func=checkin,
                validate_accounts=validate,
                status_messages={
                    CheckinStatus.NO_TASK: "ok",
                    CheckinStatus.SCHOOL_API_ERROR: "temporary",
                },
                retryable_statuses={CheckinStatus.SCHOOL_API_ERROR},
                terminal_success_statuses={CheckinStatus.NO_TASK},
                sleep_func=lambda _seconds: None,
            )

        self.assertEqual(executor.call_count, 1)
        self.assertEqual(exit_code, 0)
        self.assertTrue(results["alice"].ok)
        self.assertIn("总轮次: 2 轮", summary)


if __name__ == "__main__":
    unittest.main()
