import logging
import os
import unittest
from unittest import mock

import requests

from swu_checkin import config, telegram_control as control


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, get_results=(), post_results=()):
        self.get_results = iter(get_results)
        self.post_results = iter(post_results)
        self.calls = []
        self.closed = False
        self.trust_env = True
        self.proxies = {"https": "http://proxy.invalid"}

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        result = next(self.get_results)
        if isinstance(result, BaseException):
            raise result
        return result

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        result = next(self.post_results, FakeResponse({"ok": True}))
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        self.closed = True


class LoggingSession(FakeSession):
    def get(self, url, **kwargs):
        logging.getLogger("urllib3.connectionpool").warning("GET %s", url)
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        logging.getLogger("urllib3.connectionpool").warning("POST %s", url)
        return super().post(url, **kwargs)


class CollectHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


class FakeProcess:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


def update(*, chat_id=123, sender_id=123, text="/checkin", update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "from": {"id": sender_id},
            "text": text,
        },
    }


class TelegramControlTests(unittest.TestCase):
    def make_controller(self, *, session=None, popen=None, sleep=None):
        return control.TelegramController(
            "123456:bot-secret",
            "123",
            session=session or FakeSession(),
            popen_factory=popen or (lambda _argv: FakeProcess()),
            sleep_func=sleep or (lambda _seconds: None),
        )

    def test_authorized_private_checkin_starts_existing_cli_once(self):
        session = FakeSession()
        processes = []

        def popen(argv):
            processes.append(argv)
            return FakeProcess()

        controller = self.make_controller(session=session, popen=popen)
        controller.handle_update(update())

        self.assertEqual(processes, [[control.sys.executable, "-m", "swu_checkin"]])
        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][2]["json"]["text"], control.START_MESSAGE)

    def test_unrecognized_chat_does_not_start(self):
        popen = mock.Mock(return_value=FakeProcess())
        controller = self.make_controller(popen=popen)

        controller.handle_update(update(chat_id=999, sender_id=999))

        popen.assert_not_called()

    def test_group_message_does_not_start_even_when_chat_is_configured(self):
        popen = mock.Mock(return_value=FakeProcess())
        controller = self.make_controller(popen=popen)

        controller.handle_update(update(sender_id=456))

        popen.assert_not_called()

    def test_running_controller_process_does_not_start_second_process(self):
        popen = mock.Mock(return_value=FakeProcess())
        controller = self.make_controller(popen=popen)
        controller.handle_update(update())
        controller.handle_update(update(update_id=2))

        popen.assert_called_once_with([control.sys.executable, "-m", "swu_checkin"])
        self.assertEqual(session_message_texts(controller.session)[-1], control.ALREADY_RUNNING_MESSAGE)

    def test_non_checkin_message_does_not_start(self):
        popen = mock.Mock(return_value=FakeProcess())
        controller = self.make_controller(popen=popen)

        controller.handle_update(update(text="/checkin now"))
        controller.handle_update(update(text="/checkin@my_bot", update_id=2))
        controller.handle_update(update(text="hello", update_id=3))

        popen.assert_not_called()

    def test_startup_backlog_is_acked_and_not_executed(self):
        session = FakeSession(
            get_results=[
                FakeResponse({"ok": True, "result": [update(update_id=9), update(update_id=12)]}),
                FakeResponse({"ok": True, "result": []}),
            ]
        )
        popen = mock.Mock(return_value=FakeProcess())
        controller = self.make_controller(session=session, popen=popen)

        self.assertTrue(controller.discard_pending_updates())
        popen.assert_not_called()
        self.assertEqual(controller.offset, 13)
        self.assertEqual(session.calls[1][2]["params"]["offset"], 13)
        self.assertEqual(session.calls[0][2]["params"]["timeout"], 0)

    def test_poll_advances_offset_and_does_not_relaunch_same_update(self):
        old_command = update(update_id=21)
        session = FakeSession(
            get_results=[
                FakeResponse({"ok": True, "result": [old_command]}),
                # A real Telegram response would not repeat this after the
                # offset advance; retaining it here catches accidental
                # duplicate dispatch even with a simplistic fake API.
                FakeResponse({"ok": True, "result": [old_command]}),
            ]
        )
        popen = mock.Mock(return_value=FakeProcess())
        controller = self.make_controller(session=session, popen=popen)

        self.assertTrue(controller.poll_once())
        self.assertTrue(controller.poll_once())

        popen.assert_called_once_with([control.sys.executable, "-m", "swu_checkin"])
        self.assertEqual(controller.offset, 22)
        get_calls = [call for call in session.calls if call[0] == "GET"]
        self.assertEqual(get_calls[1][2]["params"]["offset"], 22)

    def test_poll_offset_ignores_boolean_ids_and_never_moves_backward(self):
        session = FakeSession(
            get_results=[
                FakeResponse({"ok": True, "result": [update(update_id=30), update(update_id=True)]}),
                FakeResponse({"ok": True, "result": [update(update_id=20)]}),
            ]
        )
        controller = self.make_controller(session=session)

        self.assertTrue(controller.poll_once())
        self.assertEqual(controller.offset, 31)
        self.assertTrue(controller.poll_once())
        self.assertEqual(controller.offset, 31)
        get_calls = [call for call in session.calls if call[0] == "GET"]
        self.assertEqual(get_calls[1][2]["params"]["offset"], 31)

    def test_temporary_network_error_returns_for_retry(self):
        session = FakeSession(
            get_results=[
                requests.ConnectionError("bot-secret in URL"),
                FakeResponse({"ok": True, "result": []}),
            ]
        )
        controller = self.make_controller(session=session)

        with self.assertLogs(control.logger, level=logging.WARNING) as logs:
            self.assertFalse(controller.poll_once())
        self.assertTrue(controller.poll_once())
        self.assertNotIn("bot-secret", "\n".join(logs.output))

    def test_get_updates_filters_debug_url_and_removes_filter_after_request(self):
        session = LoggingSession(get_results=[FakeResponse({"ok": True, "result": []})])
        controller = self.make_controller(session=session)
        target_logger = logging.getLogger("urllib3.connectionpool")
        handler = CollectHandler()
        records = handler.records
        old_level = target_logger.level
        old_propagate = target_logger.propagate
        target_logger.addHandler(handler)
        target_logger.setLevel(logging.DEBUG)
        target_logger.propagate = False
        try:
            self.assertEqual(controller._request_updates(timeout=0), [])
            self.assertEqual(records, [])
            target_logger.warning("outside request https://api.telegram.org/bot123456:bot-secret/getUpdates")
        finally:
            target_logger.removeHandler(handler)
            target_logger.setLevel(old_level)
            target_logger.propagate = old_propagate
        self.assertEqual(len(records), 1)
        self.assertIn("bot-secret", records[0])

    def test_send_message_filters_debug_url_and_removes_filter_after_request(self):
        session = LoggingSession(post_results=[FakeResponse({"ok": True})])
        controller = self.make_controller(session=session)
        target_logger = logging.getLogger("urllib3.connectionpool")
        handler = CollectHandler()
        records = handler.records
        old_level = target_logger.level
        old_propagate = target_logger.propagate
        target_logger.addHandler(handler)
        target_logger.setLevel(logging.DEBUG)
        target_logger.propagate = False
        try:
            self.assertTrue(controller._send_message(control.START_MESSAGE))
            self.assertEqual(records, [])
            target_logger.warning("outside request https://api.telegram.org/bot123456:bot-secret/sendMessage")
        finally:
            target_logger.removeHandler(handler)
            target_logger.setLevel(old_level)
            target_logger.propagate = old_propagate
        self.assertEqual(len(records), 1)
        self.assertIn("bot-secret", records[0])

    def test_process_exit_sends_short_status_without_output(self):
        session = FakeSession()
        controller = self.make_controller(session=session)
        controller.current_process = FakeProcess(0)

        self.assertTrue(controller._finish_process_if_done())
        self.assertIsNone(controller.current_process)
        self.assertEqual(session_message_texts(session), [control.SUCCESS_MESSAGE])

        controller.current_process = FakeProcess(1)
        self.assertTrue(controller._finish_process_if_done())
        self.assertEqual(session_message_texts(session)[-1], control.FAILURE_MESSAGE)

    def test_load_settings_uses_regular_config_directory_and_reports_missing_values(self):
        with (
            mock.patch.object(config, "load_dotenv_file") as load_dotenv,
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("sys.stderr.write") as stderr_write,
        ):
            self.assertIsNone(control._load_settings())
        load_dotenv.assert_called_once_with(config.CONFIG_DIR)
        stderr_write.assert_called()


def session_message_texts(session):
    return [call[2]["json"]["text"] for call in session.calls if call[0] == "POST"]


if __name__ == "__main__":
    unittest.main()
