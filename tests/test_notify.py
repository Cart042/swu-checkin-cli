import logging
import os
import unittest
from unittest import mock

from swu_checkin import notify


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False
        self.trust_env = True
        self.proxies = {"https": "http://proxy.invalid"}

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self, now=0.0):
        self.now = float(now)

    def __call__(self):
        return self.now


class NotifyOfflineTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(
            os.environ,
            {
                "PUSH_DINGTALK_TOKEN": "",
                "PUSH_DINGTALK_SECRET": "",
                "PUSH_QYWX_KEY": "",
                "PUSH_BARK_KEY": "",
                "PUSH_BARK_URL": "",
                "PUSH_SERVERCHAN_KEY": "",
                "PUSH_PUSHDEER_KEY": "",
                "PUSH_TELEGRAM_BOT_TOKEN": "",
                "PUSH_TELEGRAM_CHAT_ID": "",
                "SWU_PUSH_DEADLINE_SECONDS": "",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_telegram_success_uses_plain_json_and_direct_session(self):
        session = FakeSession([FakeResponse(200, {"ok": True, "result": {"message_id": 1}})])

        self.assertTrue(notify.send_telegram("123:secret", "-100123", "标题", "内容", session))
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {})
        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.telegram.org/bot123:secret/sendMessage")
        self.assertEqual(kwargs["json"], {"chat_id": "-100123", "text": "标题\n\n内容"})
        self.assertNotIn("parse_mode", kwargs["json"])
        self.assertEqual(kwargs["timeout"], notify.REQUEST_TIMEOUT_SECONDS)

    def test_telegram_requires_http_and_business_success(self):
        for response in (
            FakeResponse(500, {"ok": True}),
            FakeResponse(200, {"ok": False, "error_code": 400}),
            FakeResponse(200, ValueError("invalid json")),
        ):
            session = FakeSession([response])
            self.assertFalse(notify.send_telegram("token", "chat", "title", "body", session))
            self.assertEqual(len(session.calls), 1)

    def test_incomplete_telegram_configuration_warns_without_request(self):
        with (
            mock.patch.object(notify.requests, "Session") as session_factory,
            self.assertLogs(notify.logger, level=logging.WARNING) as logs,
        ):
            with mock.patch.dict(os.environ, {"PUSH_TELEGRAM_BOT_TOKEN": "token", "PUSH_TELEGRAM_CHAT_ID": ""}):
                self.assertFalse(notify.send_push("title", "body"))

        session_factory.assert_not_called()
        self.assertIn("PUSH_TELEGRAM_BOT_TOKEN", "\n".join(logs.output))
        self.assertIn("PUSH_TELEGRAM_CHAT_ID", "\n".join(logs.output))

    def test_long_chinese_and_emoji_message_is_split_without_loss(self):
        body = "中文🙂" * 2000
        chunks = notify._split_telegram_message(f"标题\n\n{body}")
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), f"标题\n\n{body}")
        self.assertTrue(all(len(chunk) <= notify.TELEGRAM_MAX_MESSAGE_LENGTH for chunk in chunks))
        self.assertTrue(all("\ud800" not in chunk and "\udc00" not in chunk for chunk in chunks))

        session = FakeSession([FakeResponse(200, {"ok": True}) for _ in chunks])
        self.assertTrue(notify.send_telegram("token", "@group", "标题", body, session))
        self.assertEqual([call[2]["json"]["text"] for call in session.calls], chunks)

    def test_429_retry_after_over_budget_fails_without_truncated_retry(self):
        response = FakeResponse(
            429,
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 999999}},
        )
        session = FakeSession([response])
        with mock.patch.object(notify.time, "sleep") as sleeper:
            self.assertFalse(notify.send_telegram("token", "chat", "title", "body", session))
        sleeper.assert_not_called()
        self.assertEqual(len(session.calls), 1)

    def test_telegram_failure_does_not_block_other_channels(self):
        session = FakeSession(
            [
                FakeResponse(200, {"errcode": 0}),
                FakeResponse(200, {"ok": False, "error_code": 400}),
            ]
        )
        with mock.patch.dict(
            os.environ,
            {
                "PUSH_TELEGRAM_BOT_TOKEN": "token",
                "PUSH_TELEGRAM_CHAT_ID": "chat",
                "PUSH_DINGTALK_TOKEN": "ding-token",
            },
        ):
            with self.assertLogs(notify.logger, level=logging.ERROR) as logs:
                self.assertTrue(notify.send_push("title", "body", session=session))
        self.assertEqual(len(session.calls), 2)
        self.assertIn("oapi.dingtalk.com", session.calls[0][1])
        self.assertIn("Telegram推送失败", "\n".join(logs.output))

    def test_send_push_uses_shared_channel_registration_for_all_channels(self):
        session = FakeSession(
            [
                FakeResponse(200, {"errcode": 0}),
                FakeResponse(200, {"errcode": 0}),
                FakeResponse(200),
                FakeResponse(200, {"code": 0}),
                FakeResponse(200, {"code": 0}),
                FakeResponse(200, {"ok": True}),
            ]
        )
        with mock.patch.dict(
            os.environ,
            {
                "PUSH_DINGTALK_TOKEN": "ding-token",
                "PUSH_DINGTALK_SECRET": "",
                "PUSH_QYWX_KEY": "qywx-key",
                "PUSH_BARK_KEY": "bark-key",
                "PUSH_BARK_URL": "https://bark.invalid",
                "PUSH_SERVERCHAN_KEY": "server-key",
                "PUSH_PUSHDEER_KEY": "deer-key",
                "PUSH_TELEGRAM_BOT_TOKEN": "telegram-token",
                "PUSH_TELEGRAM_CHAT_ID": "chat",
            },
        ):
            self.assertTrue(notify.send_push("title", "body", session=session))

        self.assertEqual(len(session.calls), len(notify.config.PUSH_CHANNELS))
        self.assertEqual(
            [method for method, _url, _kwargs in session.calls],
            ["POST", "POST", "GET", "POST", "POST", "POST"],
        )

    def test_send_push_closes_shared_session_and_returns_status(self):
        session = FakeSession([FakeResponse(200, {"ok": True})])
        with (
            mock.patch.object(notify.requests, "Session", return_value=session) as session_factory,
            mock.patch.dict(
                os.environ,
                {
                    "PUSH_TELEGRAM_BOT_TOKEN": "token",
                    "PUSH_TELEGRAM_CHAT_ID": "-100123",
                },
            ),
        ):
            self.assertTrue(notify.send_push("title", "body"))

        session_factory.assert_called_once_with()
        self.assertTrue(session.closed)

    def test_send_push_bounds_each_request_and_stops_after_shared_budget(self):
        clock = FakeClock()

        class SlowSession(FakeSession):
            def post(self, url, **kwargs):
                response = super().post(url, **kwargs)
                clock.now += 6
                return response

        session = SlowSession(
            [
                FakeResponse(200, {"errcode": 0}),
                FakeResponse(200, {"errcode": 0}),
            ]
        )
        with mock.patch.dict(
            os.environ,
            {
                "SWU_PUSH_DEADLINE_SECONDS": "10",
                "PUSH_DINGTALK_TOKEN": "ding-token",
                "PUSH_QYWX_KEY": "qywx-key",
                "PUSH_TELEGRAM_BOT_TOKEN": "telegram-token",
                "PUSH_TELEGRAM_CHAT_ID": "chat",
            },
        ):
            self.assertTrue(notify.send_push("title", "body", session, clock=clock))

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            [call[2]["timeout"] for call in session.calls],
            [10.0, 4.0],
        )
        self.assertIn("oapi.dingtalk.com", session.calls[0][1])
        self.assertIn("qyapi.weixin.qq.com", session.calls[1][1])

    def test_invalid_push_deadline_uses_bounded_default(self):
        for raw in ("0", "-1", "3601", "not-a-number"):
            with (
                self.subTest(raw=raw),
                mock.patch.dict(
                    os.environ,
                    {"SWU_PUSH_DEADLINE_SECONDS": raw},
                ),
            ):
                self.assertEqual(
                    notify._configured_push_deadline_seconds(),
                    notify.DEFAULT_PUSH_DEADLINE_SECONDS,
                )

    def test_telegram_retry_wait_is_skipped_when_budget_is_insufficient(self):
        clock = FakeClock()
        sleeps: list[float] = []
        session = FakeSession(
            [
                FakeResponse(
                    429,
                    {"ok": False, "error_code": 429, "parameters": {"retry_after": 4}},
                ),
            ]
        )
        with mock.patch.dict(
            os.environ,
            {
                "SWU_PUSH_DEADLINE_SECONDS": "3",
                "PUSH_TELEGRAM_BOT_TOKEN": "token",
                "PUSH_TELEGRAM_CHAT_ID": "chat",
            },
        ):
            self.assertFalse(
                notify.send_push(
                    "title",
                    "body",
                    session,
                    clock=clock,
                    sleep_func=sleeps.append,
                )
            )

        self.assertEqual(sleeps, [])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2]["timeout"], 3.0)

    def test_telegram_chunks_stop_when_shared_budget_expires(self):
        clock = FakeClock()

        class SlowSession(FakeSession):
            def post(self, url, **kwargs):
                response = super().post(url, **kwargs)
                clock.now += 1.1
                return response

        body = "x" * (notify.TELEGRAM_MAX_MESSAGE_LENGTH + 1)
        session = SlowSession([FakeResponse(200, {"ok": True})])
        with mock.patch.dict(
            os.environ,
            {
                "SWU_PUSH_DEADLINE_SECONDS": "1",
                "PUSH_TELEGRAM_BOT_TOKEN": "token",
                "PUSH_TELEGRAM_CHAT_ID": "chat",
            },
        ):
            self.assertFalse(notify.send_push("title", body, session, clock=clock))

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2]["timeout"], 1.0)

    def test_network_write_timeout_is_not_retried(self):
        session = FakeSession([TimeoutError("write timeout")])
        with mock.patch.object(notify.time, "sleep") as sleeper:
            self.assertFalse(notify.send_telegram("token", "chat", "title", "body", session))
        sleeper.assert_not_called()
        self.assertEqual(len(session.calls), 1)

    def test_telegram_logs_do_not_contain_secret_url_or_payload(self):
        token = "123456:super-secret-token"
        session = FakeSession([RuntimeError(f"POST https://api.telegram.org/bot{token}/sendMessage body=secret")])
        with self.assertLogs(notify.logger, level=logging.ERROR) as logs:
            self.assertFalse(notify.send_telegram(token, "chat", "title=secret", "body=secret", session))
        text = "\n".join(logs.output)
        self.assertNotIn(token, text)
        self.assertNotIn("api.telegram.org", text)
        self.assertNotIn("body=secret", text)

    def test_debug_http_path_is_suppressed_only_during_request(self):
        token = "123456:debug-secret"
        http_logger = logging.getLogger("urllib3.connectionpool")
        old_level = http_logger.level
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Handler()
        http_logger.setLevel(logging.DEBUG)
        http_logger.addHandler(handler)
        try:

            class LoggingSession(FakeSession):
                def post(self, url, **kwargs):
                    http_logger.debug("POST %s body=%s", url, kwargs.get("json"))
                    return super().post(url, **kwargs)

            session = LoggingSession([FakeResponse(200, {"ok": True})])
            self.assertTrue(notify.send_telegram(token, "chat", "title", "body", session))
            self.assertEqual(records, [])

            http_logger.debug("after request %s", token)
            self.assertEqual(len(records), 1)
            self.assertIn(token, records[0])
        finally:
            http_logger.removeHandler(handler)
            http_logger.setLevel(old_level)

    def test_debug_http_filter_is_removed_after_request_exception(self):
        token = "123456:exception-secret"
        http_logger = logging.getLogger("urllib3.connectionpool")
        old_level = http_logger.level
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Handler()
        http_logger.setLevel(logging.DEBUG)
        http_logger.addHandler(handler)
        try:

            class FailingSession(FakeSession):
                def post(self, url, **kwargs):
                    http_logger.debug("POST %s body=%s", url, kwargs.get("json"))
                    raise RuntimeError("offline")

            self.assertFalse(notify.send_telegram(token, "chat", "title", "body", FailingSession()))
            self.assertEqual(records, [])

            http_logger.debug("after exception %s", token)
            self.assertEqual(len(records), 1)
            self.assertIn(token, records[0])
        finally:
            http_logger.removeHandler(handler)
            http_logger.setLevel(old_level)


if __name__ == "__main__":
    unittest.main()
