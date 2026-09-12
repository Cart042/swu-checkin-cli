"""学校接口传输层（代理、重试、身份缓存）的离线回归测试。"""

import os
import unittest
from unittest import mock

from swu_checkin.api import school as school_api


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class SchoolApiTests(unittest.TestCase):
    def test_school_session_ignores_proxy_environment_and_has_no_proxy_map(self):
        class Session:
            def __init__(self):
                self.trust_env = True
                self.proxies = {"https": "http://must-not-be-used.invalid"}

        session = Session()
        with (
            mock.patch.object(school_api.requests, "Session", return_value=session),
            mock.patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://must-not-be-used.invalid", "ALL_PROXY": "http://must-not-be-used.invalid"},
                clear=False,
            ),
        ):
            direct = school_api.create_school_session()
        self.assertIs(direct, session)
        self.assertFalse(direct.trust_env)
        self.assertEqual(direct.proxies, {})

    def test_write_request_is_never_retried(self):
        session = FakeSession([FakeResponse(500), FakeResponse(200)])
        with mock.patch.object(school_api.time, "sleep", side_effect=AssertionError("write request slept")):
            with self.assertRaises(school_api.SwuRequestError) as caught:
                school_api.request_with_retry(
                    "POST",
                    "https://school.invalid/write",
                    session=session,
                    max_retries=5,
                )
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(len(session.calls), 1)

    def test_idempotent_request_retries_transient_http_status(self):
        session = FakeSession([FakeResponse(503), FakeResponse(200, {"ok": True})])
        with mock.patch.object(school_api.time, "sleep", return_value=None):
            response = school_api.request_with_retry("GET", "https://school.invalid/read", session=session)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.calls), 2)

    def test_http_401_is_token_invalid(self):
        session = FakeSession([FakeResponse(401, {"message": "expired"})])
        with self.assertRaises(school_api.TokenInvalidError) as caught:
            school_api.request_with_retry("GET", "https://school.invalid/user", session=session)
        self.assertEqual(caught.exception.status_code, 401)

    def test_student_id_is_reused_for_one_session(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"code": 200, "data": {"subject": {"username": "student-id"}}},
                )
            ]
        )
        self.assertEqual(school_api.get_student_id("candidate-token", session=session), "student-id")
        self.assertEqual(school_api.get_student_id("candidate-token", session=session), "student-id")
        self.assertEqual(len(session.calls), 1)

    def test_student_id_cache_is_scoped_to_session(self):
        response = FakeResponse(
            200,
            {"code": 200, "data": {"subject": {"username": "student-id"}}},
        )
        first = FakeSession([response])
        second = FakeSession([response])
        self.assertEqual(school_api.get_student_id("candidate-token", session=first), "student-id")
        self.assertEqual(school_api.get_student_id("candidate-token", session=second), "student-id")
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)

    def test_student_id_cache_is_keyed_by_token(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"code": 200, "data": {"subject": {"username": "student-a"}}},
                ),
                FakeResponse(
                    200,
                    {"code": 200, "data": {"subject": {"username": "student-b"}}},
                ),
            ]
        )
        self.assertEqual(school_api.get_student_id("token-a", session=session), "student-a")
        self.assertEqual(school_api.get_student_id("token-b", session=session), "student-b")
        self.assertEqual(len(session.calls), 2)

    def test_failed_student_id_response_is_not_cached(self):
        valid_response = FakeResponse(
            200,
            {"code": 200, "data": {"subject": {"username": "student-id"}}},
        )
        cases = (
            (FakeResponse(401, {"message": "expired"}), school_api.TokenInvalidError),
            (FakeResponse(200, {"code": 500, "message": "temporary failure"}), school_api.SwuBusinessError),
        )
        for failed_response, error_type in cases:
            with self.subTest(error_type=error_type.__name__):
                session = FakeSession([failed_response, valid_response])
                with self.assertRaises(error_type):
                    school_api.get_student_id("token", session=session)
                self.assertEqual(school_api.get_student_id("token", session=session), "student-id")
                self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
