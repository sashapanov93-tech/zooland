import atexit
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock


TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="zooland-registration-consent-")
atexit.register(TEST_DIRECTORY.cleanup)
if "app" not in sys.modules:
    os.environ["ZOOLAND_DB_PATH"] = os.path.join(TEST_DIRECTORY.name, "zooland-test.db")
    os.environ["ZOOLAND_ENV"] = "development"
    os.environ["ZOOLAND_SECRET_KEY"] = "test-only-secret-key-that-is-long-enough-123456"
    os.environ["ZOOLAND_PUBLIC_BASE_URL"] = "http://127.0.0.1:5000"
    os.environ["ZOOLAND_TRUSTED_HOSTS"] = ""
    os.environ["ZOOLAND_REQUIRE_ANTIVIRUS"] = "0"

import app as zooland


zooland.init_db()


class RegistrationConsentTests(unittest.TestCase):
    def setUp(self):
        zooland._request_log.clear()
        zooland._ban_count.clear()
        zooland._banned_until.clear()
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        for table in (
            "email_verification_codes",
            "rate_limit_events",
            "listing_revisions",
            "animals",
            "users",
        ):
            connection.execute(f"DELETE FROM {table}")
        connection.commit()
        connection.close()
        self.client = zooland.app.test_client()

    def csrf_token(self):
        self.client.get("/register")
        with self.client.session_transaction() as session_data:
            return session_data["csrf_token"]

    def registration_data(self, *, email="consent-check@gmail.com", **overrides):
        data = {
            "csrf_token": self.csrf_token(),
            "name": "Новый пользователь",
            "phone_country": "+7",
            "phone_country_custom": "",
            "phone": "999 123 45 67",
            "email": email,
            "password": "Strong-Pass-2026!",
            "accept_terms": "1",
            "accept_privacy": "1",
        }
        data.update(overrides)
        return data

    def user_count(self):
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        connection.close()
        return count

    def test_registration_page_has_two_separate_unchecked_required_consents(self):
        html = self.client.get("/register").get_data(as_text=True)
        for name in ("accept_terms", "accept_privacy"):
            match = re.search(
                rf'<input(?=[^>]*\bname="{name}")(?=[^>]*\brequired\b)[^>]*>',
                html,
            )
            self.assertIsNotNone(match, name)
            self.assertNotIn("checked", match.group(0))
        self.assertIn('href="/terms"', html)
        self.assertIn('href="/privacy"', html)

        login_html = self.client.get("/login").get_data(as_text=True)
        self.assertNotIn('name="accept_terms"', login_html)
        self.assertNotIn('name="accept_privacy"', login_html)

    def test_server_rejects_registration_when_either_consent_is_missing(self):
        cases = (
            {"accept_terms": "", "accept_privacy": ""},
            {"accept_terms": "1", "accept_privacy": ""},
            {"accept_terms": "", "accept_privacy": "1"},
        )
        for index, missing in enumerate(cases):
            with self.subTest(missing=missing):
                response = self.client.post(
                    "/register",
                    data=self.registration_data(
                        email=f"missing-consent-{index}@gmail.com",
                        **missing,
                    ),
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "Для регистрации отдельно примите условия".encode(),
                    response.data,
                )
                self.assertEqual(self.user_count(), 0)

    def test_successful_registration_stores_server_versions_and_timestamps(self):
        with mock.patch.object(zooland, "create_email_verification_code", return_value=True):
            response = self.client.post(
                "/register",
                data=self.registration_data(),
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/verify-email"))
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.row_factory = zooland.sqlite3.Row
        user = connection.execute(
            """SELECT terms_accepted_at, terms_version,
                      privacy_accepted_at, privacy_version
                 FROM users WHERE email=?""",
            ("consent-check@gmail.com",),
        ).fetchone()
        connection.close()

        self.assertIsNotNone(user)
        self.assertEqual(user["terms_version"], zooland.TERMS_VERSION)
        self.assertEqual(user["privacy_version"], zooland.PRIVACY_VERSION)
        self.assertEqual(user["terms_accepted_at"], user["privacy_accepted_at"])
        self.assertIsNotNone(datetime.fromisoformat(user["terms_accepted_at"]).tzinfo)

    def test_legal_pages_and_footer_links_are_public(self):
        for path, title, version in (
            ("/terms", "Условия пользования сайтом", zooland.TERMS_VERSION),
            ("/privacy", "Политика конфиденциальности", zooland.PRIVACY_VERSION),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn(title, html)
                self.assertIn(version, html)
                self.assertIn('href="/terms"', html)
                self.assertIn('href="/privacy"', html)
                if path == "/privacy":
                    self.assertIn("IP-адрес последней авторизации", html)
                    self.assertIn(
                        f"не более {zooland.AUTH_IP_RETENTION_DAYS} дней", html
                    )
                    self.assertIn("Отдельная история IP-адресов не создаётся", html)

    def test_registration_post_without_csrf_or_same_origin_is_rejected(self):
        response = self.client.post(
            "/register",
            data={
                "name": "Без CSRF",
                "phone_country": "+7",
                "phone": "9991234567",
                "email": "no-csrf@gmail.com",
                "password": "Strong-Pass-2026!",
                "accept_terms": "1",
                "accept_privacy": "1",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.user_count(), 0)


if __name__ == "__main__":
    unittest.main()
