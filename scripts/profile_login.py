#!/usr/bin/env python3
"""Measure the read-only login and token-cache path.

The benchmark deliberately does not import the check-in or notification
modules.  It runs one cold login followed by one cache hit for the same
account, and records aggregate timing and process-tree memory data only.
Credentials are entered interactively and sent to the worker over stdin;
they are never command-line arguments, written to a file, or printed.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import getpass
import io
import json
import math
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TypedDict

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_INTERVAL_SECONDS = 0.05
MAX_TIMEOUT_SECONDS = 300.0


class WarmCacheMissBrowserError(RuntimeError):
    """Raised when a warm measurement would otherwise start a browser."""


def _timeout_value(value: str) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("超时必须是有限的正数") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(f"超时必须大于 0 且不超过 {MAX_TIMEOUT_SECONDS:g} 秒")
    return timeout


def _proc_status(pid: int) -> tuple[int | None, int | None]:
    """Return ``(parent_pid, rss_kib)`` using Linux procfs."""
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        return None, None
    parent_pid = None
    rss_kib = None
    for line in text.splitlines():
        if line.startswith("PPid:"):
            try:
                parent_pid = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("VmRSS:"):
            try:
                rss_kib = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return parent_pid, rss_kib


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace")


def _descendants(root_pid: int) -> set[int]:
    children: dict[int, set[int]] = {}
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return {root_pid}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        parent_pid, _ = _proc_status(pid)
        if parent_pid is not None:
            children.setdefault(parent_pid, set()).add(pid)

    found = {root_pid}
    pending = [root_pid]
    while pending:
        current = pending.pop()
        for child in children.get(current, ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


class _PeakRecord(TypedDict):
    """Sampled process-tree measurements for one scenario."""

    peak_tree_rss_kib: int
    peak_process_rss_kib: int
    sample_count: int
    browser_pids: set[int]
    observed_pids: set[int]


def _sample_process_tree(root_pid: int, peak: _PeakRecord) -> None:
    pids = _descendants(root_pid)
    tree_rss_kib = 0
    process_rss_kib = 0
    browser_pids = peak["browser_pids"]
    observed_pids = peak["observed_pids"]
    for pid in pids:
        _, rss_kib = _proc_status(pid)
        if rss_kib is not None:
            observed_pids.add(pid)
            tree_rss_kib += rss_kib
            process_rss_kib = max(process_rss_kib, rss_kib)
        command = _proc_cmdline(pid).lower()
        executable = Path(command.split(" ", 1)[0]).name if command else ""
        if (
            "chromium" in executable
            or "chrome" in executable
            or "headless_shell" in executable
            or "playwright chromium" in command
        ):
            browser_pids.add(pid)

    peak["peak_tree_rss_kib"] = max(peak["peak_tree_rss_kib"], tree_rss_kib)
    peak["peak_process_rss_kib"] = max(peak["peak_process_rss_kib"], process_rss_kib)
    peak["sample_count"] = peak["sample_count"] + 1


def _process_group_exists(pgid: int) -> bool:
    """Return whether a process group is still present.

    The worker can exit while a browser descendant keeps the group's stdout
    pipe open, so checking the worker's return code is not enough.  ``killpg``
    with signal 0 checks the group itself without sending a signal.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        # EPERM means the group exists but cannot be inspected with the
        # current permissions.  Treat other errors conservatively as present
        # so cleanup still attempts to terminate it.
        return exc.errno != errno.ESRCH
    return True


def _wait_for_process_group_exit(pgid: int, timeout: float) -> bool:
    """Wait briefly for ``pgid`` to disappear and report the final state."""
    deadline = time.monotonic() + max(0.0, timeout)
    while _process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def _terminate_process_group(process: subprocess.Popen[str], grace_seconds: float = 2.0) -> None:
    """Stop only this benchmark worker and descendants in its new process group."""
    pgid = process.pid
    grace_seconds = max(0.0, grace_seconds)

    # The worker may already have exited while a browser descendant remains.
    # Always inspect and signal the process group; process.poll() describes
    # only the root and must not be used as a group-liveness test.
    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass

    # Re-check the group after the root has been reaped.  A child which
    # ignores SIGTERM is still enough to keep the output pipe open.
    if _wait_for_process_group_exit(pgid, grace_seconds):
        return

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        # This fallback handles a worker that changed its process group; the
        # normal path above has already terminated the group created for it.
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    _wait_for_process_group_exit(pgid, grace_seconds)


def _read_worker_stdout(process: subprocess.Popen[str], timeout: float = 5.0) -> str:
    """Drain worker output without waiting forever on an inherited pipe."""
    if process.stdout is None:
        return ""
    # stdin was closed before the sampling loop.  Detach the closed handle so
    # communicate() does not try to flush it again.
    process.stdin = None
    try:
        stdout, _ = process.communicate(timeout=max(0.0, timeout))
        return stdout or ""
    except subprocess.TimeoutExpired as exc:
        # This is a last-resort bound for a descendant that escaped the
        # session.  Group cleanup has already happened before this call.
        _terminate_process_group(process, grace_seconds=min(1.0, timeout))
        try:
            stdout, _ = process.communicate(timeout=max(0.0, timeout))
            return stdout or ""
        except subprocess.TimeoutExpired:
            partial = exc.output
            return partial if isinstance(partial, str) else ""


def _worker(scenario: str, config_dir: str, timeout: float) -> int:
    """Run one scenario; credentials arrive through stdin only."""
    username: str = ""
    password: str = ""
    try:
        credentials = json.loads(sys.stdin.read())
        username = str(credentials["username"])
        password = str(credentials["password"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"scenario": scenario, "ok": False, "error_type": "invalid_input"}))
        return 2

    result: dict[str, object] = {"scenario": scenario, "ok": False}
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    session = None
    try:
        os.environ["SWU_CONFIG_DIR"] = config_dir
        os.environ["SWU_LOG_LEVEL"] = "CRITICAL"
        sys.path.insert(0, str(REPO_ROOT))
        # Import after SWU_CONFIG_DIR is set: get_info resolves its cache path
        # when imported.  No check-in, leave/change, or notification module is
        # imported by this worker.
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            import get_info
            from school_api import create_school_session, get_student_id

            if scenario == "warm":

                @contextlib.contextmanager
                def forbid_browser_login(*_args: object, **_kwargs: object):
                    raise WarmCacheMissBrowserError("warm cache miss: browser login forbidden")

                # A cache miss must fail this warm measurement instead of
                # silently turning it into another real login attempt.
                get_info._browser_login_slot = forbid_browser_login
                get_info._browser_playwright = forbid_browser_login

            session = create_school_session()
            started = time.perf_counter()
            token = get_info.get_token(
                username,
                password,
                timeout=timeout,
                session=session,
                force_login=(scenario == "cold"),
            )
            token_elapsed = time.perf_counter() - started
            student_id = get_student_id(token, timeout=min(timeout, 10), session=session)
            total_elapsed = time.perf_counter() - started
            result.update(
                {
                    "ok": True,
                    "token_elapsed_seconds": round(token_elapsed, 4),
                    "total_elapsed_seconds": round(total_elapsed, 4),
                    "student_id_valid": isinstance(student_id, str) and bool(student_id),
                }
            )
            del token, student_id
    except Exception as exc:
        # Error text can contain URLs, account data, or response text.  Return
        # its class and an allowlisted category; never forward captured logs.
        result["error_type"] = type(exc).__name__
        # Preserve only fixed diagnostic categories, never arbitrary exception
        # messages, URLs, or response content.
        reason = getattr(exc, "reason", None)
        if isinstance(reason, str) and reason in {
            "credential",
            "page_load",
            "waf_blocked",
            "captcha",
            "token_extract",
            "login_page_changed",
            "unknown",
        }:
            result["error_reason"] = reason
    finally:
        if session is not None:
            close = getattr(session, "close", None)
            if close:
                close()
        username = password = ""
        credentials = None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


def _run_scenario(
    scenario: str,
    config_dir: str,
    username: str,
    password: str,
    timeout: float,
) -> dict[str, object]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        scenario,
        config_dir,
        "--timeout",
        str(timeout),
    ]
    child_env = os.environ.copy()
    # Do not accidentally forward similarly named credential variables from
    # a caller; this harness has one explicit stdin-only credential channel.
    for key in (
        "SWU_PERF_USERNAME",
        "SWU_PERF_PASSWORD",
        "SWU_PERF_CREDENTIALS_FILE",
        "SWU_USERNAME",
        "SWU_PASSWORD",
        "SWU_USERS",
        "SWU_DEBUG_DIR",
    ):
        child_env.pop(key, None)
    child_env.update({"SWU_CONFIG_DIR": config_dir, "SWU_LOG_LEVEL": "CRITICAL"})
    process = subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # Worker diagnostics are intentionally discarded.  The worker returns
        # only an exception class, and this also prevents a browser driver
        # stderr burst from blocking the measurement pipe.
        stderr=subprocess.DEVNULL,
        text=True,
        env=child_env,
        # Give the worker and any browser children a private process group so
        # timeout/interrupt cleanup cannot leave an orphaned browser behind.
        start_new_session=True,
    )
    payload = json.dumps({"username": username, "password": password}, ensure_ascii=False)
    peak: _PeakRecord = {
        "peak_tree_rss_kib": 0,
        "peak_process_rss_kib": 0,
        "sample_count": 0,
        "browser_pids": set(),
        "observed_pids": set(),
    }
    started = time.perf_counter()
    hard_deadline = started + max(60.0, timeout * 3.0)
    stdout = ""
    try:
        if process.stdin is not None:
            process.stdin.write(payload)
            process.stdin.close()
            # _read_worker_stdout uses communicate() after sampling.  The
            # handle is already closed, so avoid a second flush attempt there.
            process.stdin = None
        while process.poll() is None:
            _sample_process_tree(process.pid, peak)
            if time.perf_counter() >= hard_deadline:
                _terminate_process_group(process)
                break
            time.sleep(SAMPLE_INTERVAL_SECONDS)
        # A normal root exit does not prove that browser descendants exited.
        # Clean the session before draining stdout so an inherited pipe cannot
        # make this measurement harness hang.
        _terminate_process_group(process)
        stdout = _read_worker_stdout(process)
    finally:
        # Also cover KeyboardInterrupt and failures while writing/reading.
        _terminate_process_group(process)
    elapsed = time.perf_counter() - started
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (AttributeError, IndexError, ValueError):
        result = {"scenario": scenario, "ok": False, "error_type": "worker_no_result"}

    # Do not attach resource/timing numbers to a failed or interrupted login.
    # That keeps an unavailable test from looking like a valid measurement.
    if result.get("ok"):
        result.update(
            {
                "wall_clock_seconds": round(elapsed, 4),
                "peak_tree_rss_kib": peak["peak_tree_rss_kib"],
                "peak_process_rss_kib": peak["peak_process_rss_kib"],
                "browser_process_count": len(peak["browser_pids"]),
                "sample_interval_ms": int(SAMPLE_INTERVAL_SECONDS * 1000),
                "sample_count": peak["sample_count"],
                "observed_process_count": len(peak["observed_pids"]),
            }
        )
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("cold", "warm"), help=argparse.SUPPRESS)
    parser.add_argument("config_dir", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument(
        "--timeout",
        type=_timeout_value,
        default=30.0,
        help=f"每个只读阶段的请求超时（秒，范围 0<{MAX_TIMEOUT_SECONDS:g}）",
    )
    return parser.parse_args()


def main() -> int:
    if sys.platform != "linux" or not Path("/proc").is_dir():
        raise SystemExit("profile_login.py 需要 Linux /proc 才能测量进程树 RSS")
    args = _parse_args()
    if args.worker:
        if not args.config_dir:
            raise SystemExit("worker mode requires an internal temporary directory")
        return _worker(args.worker, args.config_dir, args.timeout)

    # Prompts are the only place account input is accepted.  In particular,
    # there are intentionally no --username/--password options.
    username = getpass.getpass("SWU username: ").strip()
    password = getpass.getpass("SWU password: ")
    if not username or not password:
        raise SystemExit("用户名和密码不能为空")

    temporary_dir = tempfile.TemporaryDirectory(prefix=".swu-profile-")
    config_dir = temporary_dir.name
    os.chmod(config_dir, stat.S_IRWXU)
    try:
        cold = _run_scenario("cold", config_dir, username, password, args.timeout)
        if cold.get("ok"):
            warm = _run_scenario("warm", config_dir, username, password, args.timeout)
        else:
            warm = {"scenario": "warm", "status": "skipped", "reason": "cold_failed"}
        report = {
            "method": "get_token + get_student_id",
            "read_only": True,
            "cold": cold,
            "warm": warm,
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if cold.get("ok") and warm.get("ok") else 1
    finally:
        # Drop references to credentials before the temporary cache directory
        # is removed.  No credential is ever written to that directory.
        username = password = None
        temporary_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
