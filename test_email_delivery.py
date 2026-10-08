import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock


if "app" not in sys.modules:
    os.environ["ZOOLAND_DB_PATH"] = os.path.join(tempfile.gettempdir(), "zooland-email-test.db")
    os.environ["ZOOLAND_ENV"] = "development"
    os.environ["ZOOLAND_SECRET_KEY"] = "test-only-secret-key-that-is-long-enough-123456"
    os.environ["ZOOLAND_PUBLIC_BASE_URL"] = "http://127.0.0.1:5000"
    os.environ["ZOOLAND_TRUSTED_HOSTS"] = ""
    os.environ["ZOOLAND_REQUIRE_ANTIVIRUS"] = "0"

import app as zooland


def successful_response(status=201):
    response = mock.MagicMock()
    response.getcode.return_value = status
    response.__enter__.return_value = response
    return response


def http_error(url, status):
    return urllib.error.HTTPError(url, status, "error", {}, None)


class EmailDeliveryTests(unittest.TestCase):
    def brevo_env(self, sender="ZooLand <sender@gmail.com>"):
        return {
            "ZOOLAND_BREVO_API_KEY": "test-brevo-key",
            "ZOOLAND_BREVO_FROM": sender,
        }

    def resend_env(self):
        return {
            "ZOOLAND_RESEND_API_KEY": "test-resend-key",
            "ZOOLAND_RESEND_FROM": "ZooLand <onboarding@resend.dev>",
        }

    def captured_request(self, urlopen_mock):
        request = urlopen_mock.call_args[0][0]
        self.assertIsInstance(request, urllib.request.Request)
        return request, json.loads(request.data.decode("utf-8"))

    def test_brevo_sends_without_own_domain(self):
        with mock.patch.dict(os.environ, self.brevo_env(), clear=True):
            with mock.patch("urllib.request.urlopen",
                            return_value=successful_response(201)) as urlopen_mock:
                sent = zooland.send_email("user@gmail.com", "Код", "123456",
                                          reply_to="support@gmail.com")
        self.assertTrue(sent)
        request, payload = self.captured_request(urlopen_mock)
        self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(request.get_header("Api-key"), "test-brevo-key")
        self.assertEqual(payload["sender"], {"name": "ZooLand", "email": "sender@gmail.com"})
        self.assertEqual(payload["to"], [{"email": "user@gmail.com"}])
        self.assertEqual(payload["subject"], "Код")
        self.assertEqual(payload["textContent"], "123456")
        self.assertEqual(payload["replyTo"], {"email": "support@gmail.com"})

    def test_brevo_bare_email_sender_gets_default_name(self):
        with mock.patch.dict(os.environ, self.brevo_env("sender@gmail.com"), clear=True):
            with mock.patch("urllib.request.urlopen",
                            return_value=successful_response(201)) as urlopen_mock:
                self.assertTrue(zooland.send_email("user@gmail.com", "Тема", "Текст"))
        _, payload = self.captured_request(urlopen_mock)
        self.assertEqual(payload["sender"], {"name": "ZooLand", "email": "sender@gmail.com"})
        self.assertNotIn("replyTo", payload)

    def test_brevo_failure_falls_back_to_resend(self):
        env = {**self.brevo_env(), **self.resend_env()}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("urllib.request.urlopen") as urlopen_mock:
                urlopen_mock.side_effect = [
                    http_error("https://api.brevo.com/v3/smtp/email", 401),
                    successful_response(202),
                ]
                sent = zooland.send_email("user@gmail.com", "Тема", "Текст")
        self.assertTrue(sent)
        self.assertEqual(urlopen_mock.call_count, 2)
        fallback_request, fallback_payload = self.captured_request(urlopen_mock)
        self.assertEqual(fallback_request.full_url, "https://api.resend.com/emails")
        self.assertEqual(fallback_request.get_header("Authorization"), "Bearer test-resend-key")
        self.assertEqual(fallback_payload["to"], ["user@gmail.com"])

    def test_no_provider_configured_returns_false(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("urllib.request.urlopen") as urlopen_mock:
                self.assertFalse(zooland.send_email("user@gmail.com", "Тема", "Текст"))
        urlopen_mock.assert_not_called()

    def test_invalid_recipient_is_rejected_without_network(self):
        with mock.patch.dict(os.environ, {**self.brevo_env(), **self.resend_env()},
                             clear=True):
            with mock.patch("urllib.request.urlopen") as urlopen_mock:
                self.assertFalse(zooland.send_email("not-an-email", "Тема", "Текст"))
        urlopen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
