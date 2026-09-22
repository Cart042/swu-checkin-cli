"""Telegram long-polling controller for manually starting a check-in.

This module is deliberately separate from the check-in runner.  It only
receives one allow-listed Telegram command and starts the existing CLI in a
child process; the CLI remains responsible for configuration, locking,
account handling, and result notifications.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

import requests

from . import config
from .notify import _suppress_http_debug_logging

logger = logging.getLogger("swu.telegram_control")

POLL_TIMEOUT_SECONDS = 25
HTTP_TIMEOUT_SECONDS = POLL_TIMEOUT_SECONDS + 10
RETRY_DELAY_SECONDS = 5
COMMAND = "/checkin"
START_MESSAGE = "已收到手动签到请求，开始执行。"
ALREADY_RUNNING_MESSAGE = "已有手动签到任务正在运行。"
SUCCESS_MESSAGE = "手动签到进程已结束，详细结果见签到通知。"
FAILURE_MESSAGE = "手动签到执行异常，请查看服务日志。"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{}"


def _direct_session(session):
    """Keep the controller usable with small test doubles as well as requests."""

    try:
        session.trust_env = False
    except Exception:
        pass
    try:
        session.proxies = {}
    except Exception:
        pass
    return session


def _as_update_list(body: Any) -> list[dict[str, Any]] | None:
    """Return Telegram updates from a successful response, if well formed."""

    if not isinstance(body, Mapping) or body.get("ok") is not True:
        return None
    result = body.get("result")
    if not isinstance(result, list):
        return []
    return [update for update in result if isinstance(update, dict)]


class TelegramController:
    """Small, single-threaded Telegram command controller."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        session=None,
        popen_factory: Callable[..., Any] | None = None,
        sleep_func: Callable[[float], Any] | None = None,
    ) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.session = _direct_session(session if session is not None else requests.Session())
        self._popen_factory = subprocess.Popen if popen_factory is None else popen_factory
        self._sleep = time.sleep if sleep_func is None else sleep_func
        self.offset: int | None = None
        self.current_process: Any | None = None

    @property
    def _api_url_base(self) -> str:
        # The token is required by Telegram in the path.  It is never included
        # in a log message, including when requests raises an exception.
        return TELEGRAM_API_BASE.format(self.bot_token)

    def _request_updates(self, *, timeout: int) -> list[dict[str, Any]] | None:
        """Fetch updates, returning ``None`` for a temporary API failure."""

        params: dict[str, Any] = {"timeout": timeout, "limit": 100}
        if self.offset is not None:
            params["offset"] = self.offset
        try:
            with _suppress_http_debug_logging():
                response = self.session.get(
                    f"{self._api_url_base}/getUpdates",
                    params=params,
                    timeout=HTTP_TIMEOUT_SECONDS if timeout else 10,
                )
                body = response.json()
        except Exception:
            # Exception text from requests can contain the complete URL, which
            # would disclose the Bot Token.  Keep this message intentionally
            # generic.
            logger.warning("Telegram 获取消息失败，稍后重试。")
            return None

        updates = _as_update_list(body)
        if updates is None:
            logger.warning("Telegram 获取消息返回异常，稍后重试。")
            return None
        return updates

    def _advance_offset(self, updates: list[dict[str, Any]]) -> None:
        """Mark a batch consumed without allowing an offset to move backward."""

        update_ids = [
            update["update_id"]
            for update in updates
            if isinstance(update.get("update_id"), int) and not isinstance(update.get("update_id"), bool)
        ]
        if not update_ids:
            return
        next_offset = max(update_ids) + 1
        if self.offset is None or next_offset > self.offset:
            self.offset = next_offset

    def discard_pending_updates(self) -> bool:
        """Acknowledge all updates already queued when the controller starts."""

        # Telegram returns at most 100 updates per call.  Keep making
        # non-blocking calls until the queue is empty so an old command beyond
        # the first page cannot be replayed after a restart.
        while True:
            updates = self._request_updates(timeout=0)
            if updates is None:
                return False
            if not updates:
                return True

            previous_offset = self.offset
            self._advance_offset(updates)
            if self.offset == previous_offset:
                # Valid Telegram updates always have an integer update_id.  Do
                # not spin forever if a malformed response is returned.
                return True

    def _send_message(self, text: str) -> bool:
        """Send one short controller response without exposing API details."""

        try:
            with _suppress_http_debug_logging():
                response = self.session.post(
                    f"{self._api_url_base}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text},
                    timeout=10,
                )
                body = response.json()
        except Exception:
            logger.warning("Telegram 回复消息失败。")
            return False
        if not isinstance(body, Mapping) or body.get("ok") is not True:
            logger.warning("Telegram 回复消息失败。")
            return False
        return True

    def _authorized_message(self, message: Any) -> bool:
        if not isinstance(message, Mapping):
            return False
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
            return False
        chat_value = chat.get("id")
        sender_value = sender.get("id")
        if chat_value is None or sender_value is None:
            return False
        # Telegram supplies numeric IDs in JSON, while the environment value
        # is text.  Normalize only for that representation difference; the
        # sender and chat IDs themselves must still be exactly equal.
        return sender_value == chat_value and str(chat_value) == self.chat_id

    def _finish_process_if_done(self) -> bool:
        process = self.current_process
        if process is None:
            return False
        try:
            exit_code = process.poll()
        except Exception:
            # Treat an unavailable status as still running.  This avoids
            # accidentally starting a second child while status is uncertain.
            return False
        if exit_code is None:
            return False

        self.current_process = None
        self._send_message(SUCCESS_MESSAGE if exit_code == 0 else FAILURE_MESSAGE)
        return True

    def _start_checkin(self) -> None:
        self._send_message(START_MESSAGE)
        try:
            self.current_process = self._popen_factory(
                [sys.executable, "-m", "swu_checkin"],
            )
        except OSError:
            logger.error("无法启动手动签到进程。")
            self._send_message(FAILURE_MESSAGE)

    def handle_update(self, update: Mapping[str, Any]) -> None:
        """Handle one update; only an exact, authorized ``/checkin`` runs."""

        message = update.get("message")
        if not isinstance(message, Mapping) or not self._authorized_message(message):
            return
        text = message.get("text")
        if not isinstance(text, str) or text.strip() != COMMAND:
            return

        if self.current_process is not None:
            if self._finish_process_if_done():
                # The previous task ended before this update arrived, so this
                # command can start the next task after its completion notice.
                pass
            else:
                self._send_message(ALREADY_RUNNING_MESSAGE)
                return
        self._start_checkin()

    def poll_once(self) -> bool:
        """Poll once and process its updates; ``False`` means retry later."""

        self._finish_process_if_done()
        updates = self._request_updates(timeout=POLL_TIMEOUT_SECONDS)
        if updates is None:
            return False
        # Advance before dispatching commands so every returned update is
        # consumed even if its message is unauthorized or not a command.
        self._advance_offset(updates)
        for update in updates:
            self._finish_process_if_done()
            self.handle_update(update)
        self._finish_process_if_done()
        return True

    def run(self) -> None:
        """Discard startup backlog, then continue long polling forever."""

        while not self.discard_pending_updates():
            self._sleep(RETRY_DELAY_SECONDS)

        try:
            while True:
                if not self.poll_once():
                    self._sleep(RETRY_DELAY_SECONDS)
        finally:
            try:
                self.session.close()
            except Exception:
                pass


def _load_settings() -> tuple[str, str] | None:
    """Load the regular config directory and validate shared Telegram values."""

    config.load_dotenv_file(config.CONFIG_DIR)
    token = os.getenv("PUSH_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("PUSH_TELEGRAM_CHAT_ID", "").strip()
    missing = []
    if not token:
        missing.append("PUSH_TELEGRAM_BOT_TOKEN")
    if not chat_id:
        missing.append("PUSH_TELEGRAM_CHAT_ID")
    if missing:
        print(f"Telegram Controller 配置缺失：{', '.join(missing)}。", file=sys.stderr)
        return None
    return token, chat_id


def main() -> int:
    settings = _load_settings()
    if settings is None:
        return 1
    controller = TelegramController(*settings)
    try:
        controller.run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
