"""Offline boundary tests for the profile-login measurement harness."""

import argparse
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

PROFILE_LOGIN_PATH = Path(__file__).resolve().parents[1] / "scripts" / "profile_login.py"
_SPEC = importlib.util.spec_from_file_location("profile_login", PROFILE_LOGIN_PATH)
assert _SPEC is not None and _SPEC.loader is not None
profile_login = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(profile_login)


class _FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ProfileLoginBoundaryTests(unittest.TestCase):
    def test_terminate_group_cleans_child_after_root_exit_before_stdout_drain(self):
        """A child holding stdout must not survive a completed worker root."""
        if not hasattr(os, "killpg") or sys.platform == "win32":
            self.skipTest("requires POSIX process groups")

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready"
            child_source = (
                "import pathlib, signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text('ready'); "
                "time.sleep(60)"
            )
            root_source = (
                "import pathlib, subprocess, sys, time\n"
                "marker, child = sys.argv[1:]\n"
                "subprocess.Popen([sys.executable, '-c', child, marker])\n"
                "deadline = time.time() + 5\n"
                "while not pathlib.Path(marker).exists() and time.time() < deadline:\n"
                "    time.sleep(0.01)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", root_source, str(marker), child_source],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            try:
                process.wait(timeout=3)
                self.assertTrue(marker.exists())

                started = time.monotonic()
                profile_login._terminate_process_group(process)
                self.assertLess(time.monotonic() - started, 5)

                # This would block forever while the old early-return cleanup
                # left the SIGTERM-ignoring child holding the pipe.
                process.communicate(timeout=3)
                self.assertFalse(profile_login._process_group_exists(process.pid))
            finally:
                # Keep a failed regression isolated from the test runner.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                if process.stdout is not None:
                    process.stdout.close()

    def test_warm_cache_miss_forbids_browser_fallback(self):
        fake_get_info = types.ModuleType("get_info")
        fake_school_api = types.ModuleType("school_api")
        session = _FakeSession()
        fake_get_info._browser_login_slot = object()
        fake_get_info._browser_playwright = object()

        def fake_get_token(*_args, **_kwargs):
            # _worker replaces this slot for warm measurements.  Entering it
            # proves that a cache miss is rejected before browser login.
            with fake_get_info._browser_login_slot():
                raise AssertionError("browser login should be forbidden in warm mode")

        fake_get_info.get_token = fake_get_token
        fake_school_api.create_school_session = lambda: session
        fake_school_api.get_student_id = lambda *_args, **_kwargs: "unused"
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as config_dir,
            mock.patch.dict(sys.modules, {"get_info": fake_get_info, "school_api": fake_school_api}),
            mock.patch.object(
                profile_login.sys,
                "stdin",
                io.StringIO(json.dumps({"username": "offline", "password": "secret"})),
            ),
            mock.patch.object(profile_login.sys, "stdout", output),
        ):
            status = profile_login._worker("warm", config_dir, 1.0)

        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "WarmCacheMissBrowserError")
        self.assertTrue(session.closed)

    def test_timeout_value_rejects_invalid_values(self):
        for value in ("0", "-1", "nan", "inf", "301"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    profile_login._timeout_value(value)
        self.assertEqual(profile_login._timeout_value("30"), 30.0)

    def test_worker_preserves_only_allowlisted_login_reason(self):
        class LoginFailure(Exception):
            def __init__(self, reason):
                super().__init__("sensitive response text")
                self.reason = reason

        for reason in ("page_load", "sensitive response text", ["not a string"]):
            with self.subTest(reason=reason):
                fake_login = types.ModuleType("get_info")
                fake_login.get_token = mock.Mock(side_effect=LoginFailure(reason))
                fake_api = types.ModuleType("school_api")
                session = _FakeSession()
                fake_api.create_school_session = mock.Mock(return_value=session)
                fake_api.get_student_id = mock.Mock()
                output = io.StringIO()
                with (
                    tempfile.TemporaryDirectory() as directory,
                    mock.patch.dict(sys.modules, {"get_info": fake_login, "school_api": fake_api}),
                    mock.patch.dict(os.environ),
                    mock.patch.object(
                        profile_login.sys,
                        "stdin",
                        io.StringIO(json.dumps({"username": "offline", "password": "fixture"})),
                    ),
                    mock.patch.object(profile_login.sys, "stdout", output),
                ):
                    code = profile_login._worker("cold", directory, 1.0)
                result = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(result.get("error_reason"), "page_load" if reason == "page_load" else None)
                self.assertNotIn("sensitive response text", output.getvalue())
                fake_api.get_student_id.assert_not_called()
                self.assertTrue(session.closed)

    def test_non_linux_main_stops_before_credentials_prompt(self):
        with (
            mock.patch.object(profile_login.sys, "platform", "darwin"),
            mock.patch.object(profile_login.getpass, "getpass", side_effect=AssertionError("prompted")),
        ):
            with self.assertRaisesRegex(SystemExit, "Linux"):
                profile_login.main()


if __name__ == "__main__":
    unittest.main()
