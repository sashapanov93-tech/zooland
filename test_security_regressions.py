import atexit
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from werkzeug.security import generate_password_hash


TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="zooland-security-regressions-")
atexit.register(TEST_DIRECTORY.cleanup)
TEST_DB_PATH = os.path.join(TEST_DIRECTORY.name, "zooland-test.db")

# These values must be set before app.py is imported.  The explicit database
# path is also checked below so a test can never erase the real zooland.db.
if "app" not in sys.modules:
    os.environ["ZOOLAND_DB_PATH"] = TEST_DB_PATH
    os.environ["ZOOLAND_ENV"] = "development"
    os.environ["ZOOLAND_SECRET_KEY"] = "test-only-secret-key-that-is-long-enough-123456"
    os.environ["ZOOLAND_PUBLIC_BASE_URL"] = "http://127.0.0.1:5000"
    os.environ["ZOOLAND_TRUSTED_HOSTS"] = ""
    os.environ["ZOOLAND_REQUIRE_ANTIVIRUS"] = "0"

import app as zooland


REAL_DB_PATH = os.path.realpath(os.path.join(zooland.PROJECT_ROOT, "zooland.db"))
ACTIVE_DB_PATH = os.path.realpath(zooland.DB_NAME)
SYSTEM_TEMP_PATH = os.path.realpath(tempfile.gettempdir())
if (
    ACTIVE_DB_PATH == REAL_DB_PATH
    or os.path.commonpath((ACTIVE_DB_PATH, SYSTEM_TEMP_PATH)) != SYSTEM_TEMP_PATH
):
    raise RuntimeError("Security regression tests require a temporary database")

zooland.init_db()


class SecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        zooland._request_log.clear()
        zooland._ban_count.clear()
        zooland._banned_until.clear()
        zooland._next_rate_cleanup = 0.0
        # Housekeeping is tested elsewhere.  Keeping it dormant here makes the
        # health endpoint's read-only assertion deterministic.
        zooland._next_housekeeping_at = float("inf")

        connection = sqlite3.connect(zooland.DB_NAME)
        table_names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table_name,) in table_names:
            connection.execute(f'DELETE FROM "{table_name}"')
        connection.commit()
        connection.close()
        self.client = zooland.app.test_client()

    def csrf_token(self, client, path="/register"):
        client.get(path)
        with client.session_transaction() as session_data:
            return session_data["csrf_token"]

    def registration_data(self, client, **overrides):
        data = {
            "csrf_token": self.csrf_token(client),
            "name": "Безопасный пользователь",
            "phone_country": "+7",
            "phone_country_custom": "",
            "phone": "999 123 45 67",
            "email": "security-check@gmail.com",
            "password": "Strong-Pass-2026!",
            "accept_terms": "1",
            "accept_privacy": "1",
        }
        data.update(overrides)
        return data

    def scalar(self, query, parameters=()):
        connection = sqlite3.connect(zooland.DB_NAME)
        value = connection.execute(query, parameters).fetchone()[0]
        connection.close()
        return value

    def row(self, query, parameters=()):
        connection = sqlite3.connect(zooland.DB_NAME)
        connection.row_factory = sqlite3.Row
        value = connection.execute(query, parameters).fetchone()
        connection.close()
        return value

    def create_user(
        self,
        *,
        email="victim@gmail.com",
        password="Strong-Pass-2026!",
        phone="+79991234567",
    ):
        connection = sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO users
                   (name, email, phone, password_hash, created_at,
                    email_verified, is_admin, session_version)
               VALUES (?, ?, ?, ?, ?, 1, 0, 1)""",
            (
                "Проверяемый пользователь",
                email,
                phone,
                generate_password_hash(password),
                zooland.utcnow(),
            ),
        )
        connection.commit()
        user_id = cursor.lastrowid
        connection.close()
        return user_id

    def login_as(self, user_id):
        with self.client.session_transaction() as session_data:
            session_data.clear()
            session_data["user_id"] = user_id
            session_data["session_version"] = 1
            session_data["csrf_token"] = "test-csrf-token"

    def rate_limit_buckets(self):
        connection = sqlite3.connect(zooland.DB_NAME)
        values = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT bucket FROM rate_limit_events ORDER BY bucket"
            )
        }
        connection.close()
        return values

    def test_registration_rejects_email_over_254_before_password_hashing(self):
        email = "a" * (255 - len("@gmail.com")) + "@gmail.com"
        self.assertEqual(len(email), 255)

        with (
            mock.patch.object(zooland, "generate_password_hash", return_value="unused") as hasher,
            mock.patch.object(zooland, "create_email_verification_code", return_value=True),
        ):
            response = self.client.post(
                "/register",
                data=self.registration_data(self.client, email=email),
            )

        self.assertEqual(response.status_code, 200)
        hasher.assert_not_called()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM users"), 0)

    def test_registration_rejects_password_over_128_before_hashing(self):
        password = "Xy9!" * 32 + "Z"
        self.assertEqual(len(password), 129)

        with (
            mock.patch.object(zooland, "generate_password_hash", return_value="unused") as hasher,
            mock.patch.object(zooland, "create_email_verification_code", return_value=True),
        ):
            response = self.client.post(
                "/register",
                data=self.registration_data(self.client, password=password),
            )

        self.assertEqual(response.status_code, 200)
        hasher.assert_not_called()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM users"), 0)

    def test_login_rejects_password_over_128_before_hash_check(self):
        self.create_user()
        token = self.csrf_token(self.client, "/login")

        with mock.patch.object(zooland, "check_password_hash", return_value=False) as checker:
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "email": "victim@gmail.com",
                    "password": "Xy9!" * 32 + "Z",
                },
            )

        self.assertEqual(response.status_code, 200)
        checker.assert_not_called()

    def test_registration_rate_limit_cannot_be_used_to_block_another_ip_email(self):
        attacker = zooland.app.test_client()
        victim = zooland.app.test_client()
        attacker_token = self.csrf_token(attacker)

        for _ in range(zooland.ACTION_RATE_LIMITS["register"][0]):
            response = attacker.post(
                "/register",
                data={
                    **self.registration_data(attacker),
                    "csrf_token": attacker_token,
                    "email": "target@gmail.com",
                    "accept_privacy": "",
                },
                environ_overrides={"REMOTE_ADDR": "198.51.100.10"},
            )
            self.assertEqual(response.status_code, 200)

        with mock.patch.object(zooland, "create_email_verification_code", return_value=True):
            response = victim.post(
                "/register",
                data=self.registration_data(
                    victim,
                    email="target@gmail.com",
                    phone="999 765 43 21",
                ),
                environ_overrides={"REMOTE_ADDR": "203.0.113.20"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/verify-email"))
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM users WHERE email=?", ("target@gmail.com",)),
            1,
        )
        self.assertEqual(self.rate_limit_buckets(), {"register:ip"})

    def test_login_rate_limit_cannot_be_used_to_block_another_ip_email(self):
        password = "Strong-Pass-2026!"
        self.create_user(password=password)
        attacker = zooland.app.test_client()
        victim = zooland.app.test_client()
        attacker_token = self.csrf_token(attacker, "/login")

        for _ in range(zooland.ACTION_RATE_LIMITS["login"][0]):
            response = attacker.post(
                "/login",
                data={
                    "csrf_token": attacker_token,
                    "email": "victim@gmail.com",
                    "password": "Wrong-Pass-2026!",
                },
                environ_overrides={"REMOTE_ADDR": "198.51.100.30"},
            )
            self.assertEqual(response.status_code, 200)

        response = victim.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(victim, "/login"),
                "email": "victim@gmail.com",
                "password": password,
            },
            environ_overrides={"REMOTE_ADDR": "203.0.113.40"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/account"))
        self.assertEqual(self.rate_limit_buckets(), {"login:ip"})

    def test_successful_login_stores_only_latest_canonical_client_ip(self):
        password = "Strong-Pass-2026!"
        user_id = self.create_user(password=password)

        response = self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(self.client, "/login"),
                "email": "victim@gmail.com",
                "password": password,
            },
            headers={"X-Forwarded-For": "198.51.100.200"},
            environ_overrides={"REMOTE_ADDR": "2001:0db8:0:0:0:0:0:42"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/account"))
        first_auth = self.row(
            "SELECT last_auth_ip, last_auth_at FROM users WHERE id=?", (user_id,)
        )
        self.assertEqual(first_auth["last_auth_ip"], "2001:db8::42")
        self.assertIsNotNone(datetime.fromisoformat(first_auth["last_auth_at"]).tzinfo)

        failed_client = zooland.app.test_client()
        failed_client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(failed_client, "/login"),
                "email": "victim@gmail.com",
                "password": "Wrong-Pass-2026!",
            },
            environ_overrides={"REMOTE_ADDR": "198.51.100.99"},
        )
        self.assertEqual(
            self.scalar("SELECT last_auth_ip FROM users WHERE id=?", (user_id,)),
            "2001:db8::42",
        )

        next_client = zooland.app.test_client()
        next_client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(next_client, "/login"),
                "email": "victim@gmail.com",
                "password": password,
            },
            environ_overrides={"REMOTE_ADDR": "203.0.113.41"},
        )
        self.assertEqual(
            self.scalar("SELECT last_auth_ip FROM users WHERE id=?", (user_id,)),
            "203.0.113.41",
        )

        invalid_client = zooland.app.test_client()
        invalid_client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(invalid_client, "/login"),
                "email": "victim@gmail.com",
                "password": password,
            },
            environ_overrides={"REMOTE_ADDR": "not-an-ip"},
        )
        self.assertEqual(
            self.scalar("SELECT last_auth_ip FROM users WHERE id=?", (user_id,)),
            "203.0.113.41",
        )

    def test_admin_ip_is_recorded_only_after_successful_2fa(self):
        password = "Strong-Pass-2026!"
        admin_id = self.create_user(
            email="admin@gmail.com", password=password, phone="+79990000001"
        )
        connection = sqlite3.connect(zooland.DB_NAME)
        connection.execute("UPDATE users SET is_admin=1 WHERE id=?", (admin_id,))
        connection.commit()
        connection.close()

        with mock.patch.object(zooland, "create_admin_login_code", return_value=True):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": self.csrf_token(self.client, "/login"),
                    "email": "admin@gmail.com",
                    "password": password,
                },
                environ_overrides={"REMOTE_ADDR": "198.51.100.70"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/verify-login"))
        self.assertIsNone(
            self.scalar("SELECT last_auth_ip FROM users WHERE id=?", (admin_id,))
        )

        connection = sqlite3.connect(zooland.DB_NAME)
        connection.execute(
            """INSERT INTO admin_login_codes
                   (user_id, code_hash, expires_at, attempts, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (
                admin_id,
                zooland.short_code_hash("123456"),
                zooland.email_code_expires_at(),
                zooland.utcnow(),
            ),
        )
        connection.commit()
        connection.close()
        with self.client.session_transaction() as session_data:
            csrf_token = session_data["csrf_token"]

        response = self.client.post(
            "/admin/verify-login",
            data={"csrf_token": csrf_token, "code": "123456"},
            environ_overrides={"REMOTE_ADDR": "203.0.113.70"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/account"))
        self.assertEqual(
            self.scalar("SELECT last_auth_ip FROM users WHERE id=?", (admin_id,)),
            "203.0.113.70",
        )

    def test_admin_users_is_the_only_user_facing_ip_surface(self):
        target_id = self.create_user()
        admin_id = self.create_user(
            email="admin@gmail.com", phone="+79990000001"
        )
        auth_at = "2026-09-10T12:34:56+00:00"
        connection = sqlite3.connect(zooland.DB_NAME)
        connection.execute(
            "UPDATE users SET last_auth_ip=?, last_auth_at=? WHERE id=?",
            ("203.0.113.99", auth_at, target_id),
        )
        connection.execute("UPDATE users SET is_admin=1 WHERE id=?", (admin_id,))
        connection.commit()
        connection.close()

        guest_response = zooland.app.test_client().get("/admin/users")
        self.assertEqual(guest_response.status_code, 302)
        self.assertNotIn(b"203.0.113.99", guest_response.data)

        self.login_as(target_id)
        ordinary_response = self.client.get("/admin/users")
        self.assertEqual(ordinary_response.status_code, 403)
        self.assertNotIn(b"203.0.113.99", ordinary_response.data)
        self.assertNotIn("203.0.113.99", self.client.get("/account").get_data(as_text=True))
        self.assertNotIn(
            "203.0.113.99", self.client.get(f"/user/{target_id}").get_data(as_text=True)
        )

        self.login_as(admin_id)
        admin_response = self.client.get("/admin/users")
        admin_html = admin_response.get_data(as_text=True)
        self.assertEqual(admin_response.status_code, 200)
        self.assertIn("IP последней авторизации", admin_html)
        self.assertIn("203.0.113.99", admin_html)
        self.assertIn("2026-09-10 12:34 UTC", admin_html)
        self.assertIn("no-store", admin_response.headers.get("Cache-Control", ""))

    def test_expired_authentication_ips_are_cleared_without_history(self):
        old_user_id = self.create_user()
        recent_user_id = self.create_user(
            email="recent@gmail.com", phone="+79990000001"
        )
        old_at = (
            datetime.now(timezone.utc)
            - timedelta(days=zooland.AUTH_IP_RETENTION_DAYS, seconds=1)
        ).isoformat()
        recent_at = (
            datetime.now(timezone.utc)
            - timedelta(days=zooland.AUTH_IP_RETENTION_DAYS - 1)
        ).isoformat()
        connection = sqlite3.connect(zooland.DB_NAME)
        connection.execute(
            "UPDATE users SET last_auth_ip='198.51.100.10', last_auth_at=? WHERE id=?",
            (old_at, old_user_id),
        )
        connection.execute(
            "UPDATE users SET last_auth_ip='198.51.100.11', last_auth_at=? WHERE id=?",
            (recent_at, recent_user_id),
        )
        connection.commit()
        connection.close()

        with zooland.app.test_request_context("/"):
            zooland.cleanup_expired()

        self.assertIsNone(
            self.scalar("SELECT last_auth_ip FROM users WHERE id=?", (old_user_id,))
        )
        self.assertEqual(
            self.scalar("SELECT last_auth_ip FROM users WHERE id=?", (recent_user_id,)),
            "198.51.100.11",
        )

    def test_init_db_migrates_legacy_users_without_losing_accounts(self):
        file_descriptor, legacy_db_path = tempfile.mkstemp(
            prefix="zooland-legacy-users-", suffix=".db", dir=TEST_DIRECTORY.name
        )
        os.close(file_descriptor)
        connection = sqlite3.connect(legacy_db_path)
        connection.execute(
            """CREATE TABLE users (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT NOT NULL,
                   email TEXT NOT NULL UNIQUE,
                   password_hash TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )"""
        )
        connection.execute(
            """INSERT INTO users (name, email, password_hash, created_at)
               VALUES ('Старый пользователь', 'legacy@gmail.com', 'hash', ?)""",
            (zooland.utcnow(),),
        )
        connection.commit()
        connection.close()

        with mock.patch.object(zooland, "DB_NAME", legacy_db_path):
            zooland.init_db()
            zooland.init_db()

        connection = sqlite3.connect(legacy_db_path)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        legacy_user = connection.execute(
            "SELECT name, last_auth_ip, last_auth_at FROM users WHERE email='legacy@gmail.com'"
        ).fetchone()
        connection.close()

        self.assertTrue({"last_auth_ip", "last_auth_at"}.issubset(columns))
        self.assertEqual(legacy_user, ("Старый пользователь", None, None))

    def test_saved_search_rejects_oversized_filters_without_large_location(self):
        user_id = self.create_user()
        self.login_as(user_id)

        response = self.client.post(
            "/searches/save",
            data={
                "csrf_token": "test-csrf-token",
                "q": "x" * 50_000,
                "city": "",
                "type": "",
                "kind": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM saved_searches"), 0)
        self.assertLessEqual(len(response.headers.get("Location", "")), 2_048)

    def test_saved_search_has_hard_quota_of_20_per_user(self):
        user_id = self.create_user()
        connection = sqlite3.connect(zooland.DB_NAME)
        connection.executemany(
            """INSERT INTO saved_searches (user_id, name, filters_json, created_at)
               VALUES (?, ?, ?, ?)""",
            [
                (user_id, f"Поиск {index}", f'{{"q":"питомец-{index}"}}', zooland.utcnow())
                for index in range(20)
            ],
        )
        connection.commit()
        connection.close()
        self.login_as(user_id)

        response = self.client.post(
            "/searches/save",
            data={"csrf_token": "test-csrf-token", "q": "двадцать первый поиск"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM saved_searches WHERE user_id=?", (user_id,)),
            20,
        )
        self.assertLessEqual(len(response.headers.get("Location", "")), 2_048)

    def test_phone_change_requires_current_password(self):
        password = "Strong-Pass-2026!"
        user_id = self.create_user(password=password)
        self.login_as(user_id)

        response = self.client.post(
            "/settings/phone",
            data={
                "csrf_token": "test-csrf-token",
                "phone_country": "+7",
                "phone": "999 765 43 21",
                "current_password": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.scalar("SELECT phone FROM users WHERE id=?", (user_id,)),
            "+79991234567",
        )

        with mock.patch.object(zooland, "send_email", return_value=True):
            response = self.client.post(
                "/settings/phone",
                data={
                    "csrf_token": "test-csrf-token",
                    "phone_country": "+7",
                    "phone": "999 765 43 21",
                    "current_password": password,
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.scalar("SELECT phone FROM users WHERE id=?", (user_id,)),
            "+79997654321",
        )

    def test_saved_search_email_is_queued_outside_http_worker(self):
        owner_id = self.create_user(email="owner@gmail.com", phone="+79990000001")
        watcher_id = self.create_user(email="watcher@gmail.com", phone="+79990000002")
        connection = sqlite3.connect(zooland.DB_NAME)
        listing_id = connection.execute(
            """INSERT INTO animals
                   (type, breed, age, price, city, description, photo, user_id,
                    status, created_at, deal_status)
               VALUES ('Кот', 'Домашняя', 2, 1000, 'Москва', '', '', ?,
                       'active', ?, 'open')""",
            (owner_id, zooland.utcnow()),
        ).lastrowid
        connection.execute(
            """INSERT INTO saved_searches (user_id, name, filters_json, created_at)
               VALUES (?, 'Ищу кота', '{"kind":"animals","q":"кот"}', ?)""",
            (watcher_id, zooland.utcnow()),
        )
        connection.commit()
        connection.close()

        with zooland.app.test_request_context("/"):
            with mock.patch.object(zooland, "send_email", return_value=True) as sender:
                self.assertTrue(zooland.notify_saved_searches("animal", listing_id))
                sender.assert_not_called()
                completed, retrying = zooland.process_saved_search_jobs(10)

        self.assertEqual((completed, retrying), (1, 0))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM search_notifications"), 1)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM saved_search_jobs WHERE processed_at IS NOT NULL"),
            1,
        )

    def test_healthz_is_get_only_json_and_does_not_mutate_database(self):
        user_id = self.create_user()
        connection = sqlite3.connect(zooland.DB_NAME)
        connection.execute(
            """INSERT INTO saved_searches (user_id, name, filters_json, created_at)
               VALUES (?, 'Контрольная строка', '{"q":"кот"}', ?)""",
            (user_id, zooland.utcnow()),
        )
        connection.commit()
        before = connection.execute(
            "SELECT COUNT(*), MIN(name), MAX(filters_json) FROM saved_searches"
        ).fetchone()
        connection.close()

        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        self.assertEqual(response.get_json().get("status"), "ok")
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

        connection = sqlite3.connect(zooland.DB_NAME)
        after = connection.execute(
            "SELECT COUNT(*), MIN(name), MAX(filters_json) FROM saved_searches"
        ).fetchone()
        connection.close()
        self.assertEqual(after, before)

        # Health checks intentionally do not create a session/CSRF cookie.
        token = self.csrf_token(self.client, "/register")
        post_response = self.client.post("/healthz", data={"csrf_token": token})
        self.assertEqual(post_response.status_code, 405)


class ProductionConfigurationTests(unittest.TestCase):
    def production_env(self, **overrides):
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("ZOOLAND_")
        }
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "ZOOLAND_ENV": "production",
                "ZOOLAND_DB_PATH": os.path.join(TEST_DIRECTORY.name, "production-test.db"),
                "ZOOLAND_UPLOAD_FOLDER": os.path.join(TEST_DIRECTORY.name, "production-uploads"),
                "ZOOLAND_PUBLIC_BASE_URL": "https://zooland.example",
                "ZOOLAND_TRUSTED_HOSTS": "zooland.example",
                "ZOOLAND_TRUST_PROXY_HOPS": "1",
                "ZOOLAND_SECRET_KEY": "9f" * 48,
                "ZOOLAND_REQUIRE_ANTIVIRUS": "1",
                "ZOOLAND_CLAMAV_HOST": "127.0.0.1",
                "ZOOLAND_OPERATOR_NAME": "Тестовый оператор",
                "ZOOLAND_OPERATOR_ADDRESS": "Тестовый адрес",
                "ZOOLAND_PRIVACY_EMAIL": "privacy@zooland.example",
                "ZOOLAND_SUPPORT_EMAIL": "support@zooland.example",
                "ZOOLAND_SMTP_HOST": "smtp.zooland.example",
                "ZOOLAND_SMTP_PORT": "587",
                "ZOOLAND_SMTP_FROM": "support@zooland.example",
                "ZOOLAND_SMTP_USERNAME": "",
                "ZOOLAND_SMTP_PASSWORD": "",
                "ZOOLAND_SMTP_TLS": "1",
            }
        )
        environment.update(overrides)
        return environment

    def import_app(self, **environment_overrides):
        return subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=zooland.PROJECT_ROOT,
            env=self.production_env(**environment_overrides),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_production_rejects_known_sample_secret(self):
        result = self.import_app(
            ZOOLAND_SECRET_KEY="generate_a_unique_96_character_hex_secret"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ZOOLAND_SECRET_KEY", result.stderr)

    def test_production_rejects_missing_operator_identity(self):
        result = self.import_app(ZOOLAND_OPERATOR_NAME="", ZOOLAND_OPERATOR_ADDRESS="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ZOOLAND_OPERATOR_NAME", result.stderr)

    def test_production_rejects_missing_smtp_configuration(self):
        result = self.import_app(
            ZOOLAND_SMTP_HOST="",
            ZOOLAND_SMTP_FROM="",
            ZOOLAND_SMTP_USERNAME="",
            ZOOLAND_SMTP_PASSWORD="",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ZOOLAND_SMTP_HOST", result.stderr)


if __name__ == "__main__":
    unittest.main()
