"""qBittorrent 5.2 HTTP 204 login regression coverage."""
import unittest
from unittest.mock import Mock

from guard import ApiError, Config, Qbit


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.qbit = object.__new__(Qbit)
        self.qbit.config = Config("http://qb:8080", "user", "password", {}, {})
        self.qbit.http = Mock()

    def test_new_qbittorrent_204_login_succeeds_when_session_valid(self):
        self.qbit.http.request.side_effect = [(204, ""), "v5.2.0"]
        self.qbit.login()
        calls = self.qbit.http.request.call_args_list
        self.assertEqual(calls[0].kwargs["include_status"], True)
        self.assertEqual(calls[0].kwargs["fields"], {"username": "user", "password": "password"})
        self.assertEqual(calls[1].args[1], "http://qb:8080/api/v2/app/version")

    def test_old_qbittorrent_200_login_succeeds_when_session_valid(self):
        self.qbit.http.request.side_effect = [(200, "Ok."), "v5.1.2"]
        self.qbit.login()

    def test_wrong_credentials_rejected(self):
        self.qbit.http.request.return_value = (200, "Fails.")
        with self.assertRaisesRegex(ApiError, "refused login"):
            self.qbit.login()
        self.assertEqual(self.qbit.http.request.call_count, 1)

    def test_http_200_empty_does_not_look_like_success(self):
        self.qbit.http.request.return_value = (200, "")
        with self.assertRaisesRegex(ApiError, "refused login"):
            self.qbit.login()
        self.assertEqual(self.qbit.http.request.call_count, 1)

    def test_204_without_valid_session_rejected(self):
        self.qbit.http.request.side_effect = [(204, ""), ApiError("HTTP 403", 403)]
        with self.assertRaisesRegex(ApiError, "session verification failed"):
            self.qbit.login()

    def test_malformed_204_with_body_rejected(self):
        self.qbit.http.request.return_value = (204, "bad")
        with self.assertRaisesRegex(ApiError, "refused login"):
            self.qbit.login()

    def test_204_version_missing_rejected(self):
        self.qbit.http.request.side_effect = [(204, ""), ""]
        with self.assertRaisesRegex(ApiError, "version verification"):
            self.qbit.login()


if __name__ == "__main__":
    unittest.main()
