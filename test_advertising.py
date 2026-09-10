import atexit
import json
import os
import tempfile
import unittest
from datetime import date, timedelta


TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="zooland-advertising-")
atexit.register(TEST_DIRECTORY.cleanup)
os.environ["ZOOLAND_DB_PATH"] = os.path.join(TEST_DIRECTORY.name, "zooland-test.db")
os.environ["ZOOLAND_ENV"] = "development"
os.environ["ZOOLAND_SECRET_KEY"] = "test-only-secret-key-that-is-long-enough-123456"
os.environ["ZOOLAND_PUBLIC_BASE_URL"] = "http://127.0.0.1:5000"
os.environ["ZOOLAND_TRUSTED_HOSTS"] = ""
os.environ["ZOOLAND_REQUIRE_ANTIVIRUS"] = "0"

import app as zooland


zooland.init_db()


class AdvertisingPublicationTests(unittest.TestCase):
    def setUp(self):
        zooland._next_housekeeping_at = 0.0
        zooland._request_log.clear()
        zooland._ban_count.clear()
        zooland._banned_until.clear()
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        for table in (
            "advertising_revisions", "advertising_daily_metrics", "advertising_requests", "rate_limit_events",
            "services", "users",
        ):
            connection.execute(f"DELETE FROM {table}")
        connection.commit()
        connection.close()
        self.client = zooland.app.test_client()

    def create_user(self, email="owner@gmail.com", name="Владелец", is_admin=0):
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO users
               (name, email, password_hash, created_at, email_verified, is_admin, session_version)
               VALUES (?, ?, 'test-password-hash', ?, 1, ?, 1)""",
            (name, email, zooland.utcnow(), is_admin),
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

    def endpoint_url(self, endpoint, **values):
        with zooland.app.test_request_context():
            return zooland.url_for(endpoint, **values)

    def valid_advertising_form(self, **overrides):
        values = {
            "csrf_token": "test-csrf-token",
            "format": "homepage-banner",
            "contact_name": "Алексей Рекламодатель",
            "company": "Добрый бренд",
            "inn": "1234567890",
            "email": "forged-form@example.com",
            "phone": "+79990000000",
            "telegram": "kind_brand",
            "target_url": "https://example.com/offer",
            "category": "all",
            "cities": "Москва",
            "preferred_dates": "1–15 сентября",
            "budget": "До 10 000 ₽",
            "comment": "Хотим рассказать о полезном предложении владельцам питомцев.",
            "personal_data_consent": "yes",
        }
        values.update(overrides)
        return values

    def advertisement_row(self, request_id):
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.row_factory = zooland.sqlite3.Row
        row = connection.execute(
            "SELECT * FROM advertising_requests WHERE id=?", (request_id,)
        ).fetchone()
        connection.close()
        return dict(row) if row else None

    def advertising_revision_row(self, request_id):
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.row_factory = zooland.sqlite3.Row
        row = connection.execute(
            "SELECT * FROM advertising_revisions WHERE request_id=?", (request_id,)
        ).fetchone()
        connection.close()
        return dict(row) if row else None

    def advertising_metric_values(self, request_id):
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        values = dict(connection.execute(
            """SELECT metric, value FROM advertising_daily_metrics
                WHERE request_id=? ORDER BY metric""",
            (request_id,),
        ).fetchall())
        connection.close()
        return values

    def seed_advertising_metrics(self, request_id, impressions=7, clicks=2):
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.executemany(
            """INSERT INTO advertising_daily_metrics
               (request_id, metric_day, metric, value) VALUES (?, ?, ?, ?)""",
            (
                (request_id, zooland.advertising_today(), "impression", impressions),
                (request_id, zooland.advertising_today(), "click", clicks),
            ),
        )
        connection.commit()
        connection.close()

    def advertising_revision_values(self, **overrides):
        values = {
            "format": "homepage-banner",
            "contact_name": "Новое контактное лицо",
            "company": "Новый согласуемый бренд",
            "inn": "0987654321",
            "phone": "+78880000000",
            "telegram": "new_brand",
            "target_url": "https://example.org/new-offer",
            "category": "all",
            "cities": "",
            "preferred_dates": "Сентябрь и октябрь 2026",
            "budget": "10 000–30 000 ₽",
            "comment": "Обновлённая заявка владельца для проверки администратором.",
            "headline": "Новый заголовок после проверки",
            "ad_text": "Новый рекламный текст должен появиться только после одобрения.",
            "button_label": "Открыть новое",
        }
        values.update(overrides)
        return values

    def active_advertising_schedule(self):
        return {
            "start_date": (date.today() - timedelta(days=1)).isoformat(),
            "end_date": (date.today() + timedelta(days=30)).isoformat(),
        }

    def active_revision_form(self, request_id, **overrides):
        values = self.advertising_revision_values()
        values.update({
            "base_updated_at": self.advertisement_row(request_id)["updated_at"],
            "user_id": "999999",
            "email": "forged-owner@example.net",
            "status": "completed",
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
            "admin_note": "Владелец не должен менять эту заметку",
            "impressions": "999999",
            "clicks": "999999",
        })
        values.update(overrides)
        return self.valid_advertising_form(**values)

    def admin_advertising_form(self, request_id, **overrides):
        advertisement = self.advertisement_row(request_id)
        values = {
            "csrf_token": "test-csrf-token",
            "status": advertisement["status"],
            "format": advertisement["format"],
            "category": advertisement["category"],
            "target_url": advertisement["target_url"],
            "cities": advertisement["cities"] or "",
            "headline": advertisement["headline"] or "",
            "ad_text": advertisement["ad_text"] or "",
            "button_label": advertisement["button_label"] or "Подробнее",
            "start_date": advertisement["start_date"] or "",
            "end_date": advertisement["end_date"] or "",
            "admin_note": advertisement["admin_note"] or "",
        }
        values.update(overrides)
        return values

    def create_advertisement(self, user_id=None, **overrides):
        now = zooland.utcnow()
        values = {
            "format": "homepage-banner",
            "contact_name": "PRIVATE-CONTACT-XYZ",
            "company": "Добрый бренд",
            "inn": "1234567890",
            "email": "private@example.com",
            "phone": "+79990000000",
            "telegram": "private_contact",
            "target_url": "https://example.com/offer",
            "category": "all",
            "cities": "",
            "preferred_dates": "PRIVATE-DATE-XYZ",
            "budget": "PRIVATE-BUDGET-XYZ",
            "comment": "PRIVATE-COMMENT-XYZ",
            "status": "active",
            "admin_note": "Скрытая заметка",
            "consent_at": now,
            "created_at": now,
            "updated_at": now,
            "headline": "Полезное предложение",
            "ad_text": "Всё необходимое для заботы о питомце.",
            "button_label": "Узнать больше",
            "start_date": None,
            "end_date": None,
        }
        if user_id is not None:
            values["user_id"] = user_id
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            f"INSERT INTO advertising_requests ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        connection.commit()
        request_id = cursor.lastrowid
        connection.close()
        return request_id

    def test_only_registered_users_can_open_and_submit_advertising_form(self):
        response = self.client.get("/advertising")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/login', html)
        self.assertNotIn('class="advertising-form"', html)

        with self.client.session_transaction() as session_data:
            session_data["csrf_token"] = "test-csrf-token"
        response = self.client.post("/advertising", data=self.valid_advertising_form())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.endpoint_url("login"))
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        request_count = connection.execute("SELECT COUNT(*) FROM advertising_requests").fetchone()[0]
        connection.close()
        self.assertEqual(request_count, 0)

    def test_intentionally_termless_animal_survives_database_initialization(self):
        owner_id = self.create_user(email="termless-owner@gmail.com")
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO animals
               (type, breed, status, user_id, deal_status, created_at, expires_at)
               VALUES ('Кот', 'Тестовая порода', 'active', ?, 'open', ?, NULL)""",
            (owner_id, zooland.utcnow()),
        )
        animal_id = cursor.lastrowid
        connection.commit()
        connection.close()

        zooland.init_db()

        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        expires_at = connection.execute(
            "SELECT expires_at FROM animals WHERE id=?", (animal_id,)
        ).fetchone()[0]
        connection.close()
        self.assertIsNone(expires_at)

    def test_authenticated_submission_uses_session_owner_and_profile_email(self):
        owner_id = self.create_user(email="profile-owner@gmail.com")
        other_id = self.create_user(email="other@gmail.com", name="Другой пользователь")
        self.login_as(owner_id)
        response = self.client.post(
            "/advertising",
            data=self.valid_advertising_form(
                user_id=str(other_id),
                email="forged-contact@example.net",
                company="Компания владельца",
            ),
        )
        self.assertEqual(response.status_code, 302)
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.row_factory = zooland.sqlite3.Row
        row = connection.execute(
            "SELECT user_id, email, company FROM advertising_requests"
        ).fetchone()
        connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], owner_id)
        self.assertEqual(row["email"], "profile-owner@gmail.com")
        self.assertEqual(row["company"], "Компания владельца")

    def test_owner_dashboard_contains_only_owned_advertisements(self):
        owner_id = self.create_user(email="owner@gmail.com")
        other_id = self.create_user(email="other@gmail.com", name="Другой пользователь")
        self.create_advertisement(user_id=owner_id, company="МОЯ-КАМПАНИЯ-XYZ")
        self.create_advertisement(user_id=other_id, company="ЧУЖАЯ-КАМПАНИЯ-XYZ")
        self.create_advertisement(company="СТАРАЯ-КАМПАНИЯ-БЕЗ-ВЛАДЕЛЬЦА-XYZ")
        self.login_as(owner_id)

        response = self.client.get(self.endpoint_url("account_advertising"))
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertIn("МОЯ-КАМПАНИЯ-XYZ", html)
        self.assertNotIn("ЧУЖАЯ-КАМПАНИЯ-XYZ", html)
        self.assertNotIn("СТАРАЯ-КАМПАНИЯ-БЕЗ-ВЛАДЕЛЬЦА-XYZ", html)

    def test_cross_owner_advertising_actions_return_404_without_changes(self):
        owner_id = self.create_user(email="owner@gmail.com")
        attacker_id = self.create_user(email="attacker@gmail.com", name="Другой пользователь")
        request_id = self.create_advertisement(
            user_id=owner_id,
            status="completed",
            admin_note="Только для администратора",
        )
        before = self.advertisement_row(request_id)
        self.login_as(attacker_id)

        response = self.client.get(
            self.endpoint_url("account_advertising_edit", request_id=request_id)
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.advertisement_row(request_id), before)

        actions = (
            ("account_advertising_edit", self.valid_advertising_form(company="Попытка подмены")),
            ("account_advertising_finish", {"csrf_token": "test-csrf-token"}),
            ("account_advertising_resubmit", {"csrf_token": "test-csrf-token"}),
        )
        for endpoint, data in actions:
            with self.subTest(endpoint=endpoint):
                response = self.client.post(
                    self.endpoint_url(endpoint, request_id=request_id), data=data
                )
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self.advertisement_row(request_id), before)

    def test_active_owner_edit_stages_only_whitelisted_revision_and_keeps_live_version(self):
        owner_id = self.create_user(email="owner@gmail.com")
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Текущий опубликованный бренд",
            target_url="https://example.com/current-offer",
            headline="Текущий опубликованный заголовок",
            ad_text="Текущий опубликованный рекламный текст.",
            button_label="Текущая кнопка",
            admin_note="Служебная заметка администратора",
            **self.active_advertising_schedule(),
        )
        self.seed_advertising_metrics(request_id)
        live_before = self.advertisement_row(request_id)
        metrics_before = self.advertising_metric_values(request_id)
        expected_payload = self.advertising_revision_values()
        self.login_as(owner_id)

        response = self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.endpoint_url("account_advertising"))
        self.assertEqual(self.advertisement_row(request_id), live_before)
        self.assertEqual(self.advertising_metric_values(request_id), metrics_before)

        revision = self.advertising_revision_row(request_id)
        self.assertIsNotNone(revision)
        self.assertEqual(revision["request_id"], request_id)
        self.assertEqual(revision["owner_id"], owner_id)
        self.assertEqual(revision["status"], "pending")
        self.assertIsNone(revision["moderation_reason"])
        self.assertEqual(revision["base_updated_at"], live_before["updated_at"])
        payload = json.loads(revision["payload_json"])
        self.assertEqual(payload, expected_payload)
        for protected_field in (
            "user_id", "email", "status", "start_date", "end_date", "admin_note",
            "impressions", "clicks", "metrics",
        ):
            self.assertNotIn(protected_field, payload)

        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Текущий опубликованный бренд", html)
        self.assertIn("Текущий опубликованный заголовок", html)
        self.assertIn("Текущий опубликованный рекламный текст.", html)
        self.assertNotIn(expected_payload["company"], html)
        self.assertNotIn(expected_payload["headline"], html)
        self.assertNotIn(expected_payload["ad_text"], html)
        click_response = self.client.get(f"/advertising/click/{request_id}")
        self.assertEqual(click_response.headers["Location"], "https://example.com/current-offer")

    def test_active_owner_edit_rejects_stale_get_version_without_creating_revision(self):
        owner_id = self.create_user(email="owner@gmail.com")
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Версия на момент открытия формы",
            **self.active_advertising_schedule(),
        )
        self.login_as(owner_id)
        live_at_get = self.advertisement_row(request_id)
        response = self.client.get(
            self.endpoint_url("account_advertising_edit", request_id=request_id)
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="base_updated_at"', html)
        self.assertIn(f'value="{live_at_get["updated_at"]}"', html)

        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.execute(
            """UPDATE advertising_requests
                  SET company=?, updated_at=? WHERE id=?""",
            ("Версия, изменённая администратором", "2099-01-01T00:00:00+00:00", request_id),
        )
        connection.commit()
        connection.close()
        live_after_admin = self.advertisement_row(request_id)

        response = self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(
                request_id,
                base_updated_at=live_at_get["updated_at"],
                company="Устаревшая версия владельца",
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            self.endpoint_url("account_advertising_edit", request_id=request_id),
        )
        self.assertEqual(self.advertisement_row(request_id), live_after_admin)
        self.assertIsNone(self.advertising_revision_row(request_id))

    def test_active_owner_edit_rejects_stale_get_version_without_overwriting_revision(self):
        owner_id = self.create_user(email="owner@gmail.com")
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Опубликованная версия с существующей правкой",
            **self.active_advertising_schedule(),
        )
        self.login_as(owner_id)
        self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id, company="Первая сохранённая правка"),
        )
        revision_before = self.advertising_revision_row(request_id)
        self.assertIsNotNone(revision_before)
        live_at_get = self.advertisement_row(request_id)
        response = self.client.get(
            self.endpoint_url("account_advertising_edit", request_id=request_id)
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="base_updated_at"', html)
        self.assertIn(f'value="{live_at_get["updated_at"]}"', html)

        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.execute(
            """UPDATE advertising_requests
                  SET company=?, updated_at=? WHERE id=?""",
            ("Новая live-версия администратора", "2099-01-02T00:00:00+00:00", request_id),
        )
        connection.commit()
        connection.close()
        live_after_admin = self.advertisement_row(request_id)

        response = self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(
                request_id,
                base_updated_at=live_at_get["updated_at"],
                company="Вторая устаревшая правка владельца",
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            self.endpoint_url("account_advertising_edit", request_id=request_id),
        )
        self.assertEqual(self.advertisement_row(request_id), live_after_admin)
        self.assertEqual(self.advertising_revision_row(request_id), revision_before)

    def test_admin_approval_atomically_publishes_revision_and_preserves_protected_fields(self):
        owner_id = self.create_user(email="owner@gmail.com")
        admin_id = self.create_user(email="admin@gmail.com", name="Администратор", is_admin=1)
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Старый бренд до одобрения",
            target_url="https://example.com/current-offer",
            headline="Старый заголовок до одобрения",
            ad_text="Старый рекламный текст до одобрения.",
            button_label="Старая кнопка",
            admin_note="Служебное решение администратора",
            **self.active_advertising_schedule(),
        )
        self.seed_advertising_metrics(request_id, impressions=11, clicks=3)
        live_before = self.advertisement_row(request_id)
        metrics_before = self.advertising_metric_values(request_id)
        expected_payload = self.advertising_revision_values()
        self.login_as(owner_id)
        self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id),
        )
        revision_before = self.advertising_revision_row(request_id)
        self.assertIsNotNone(revision_before)

        response = self.client.post(
            self.endpoint_url("admin_approve_advertising_revision", request_id=request_id),
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.advertisement_row(request_id), live_before)
        self.assertEqual(self.advertising_revision_row(request_id), revision_before)

        self.login_as(admin_id)
        response = self.client.post(
            self.endpoint_url("admin_approve_advertising_revision", request_id=request_id),
            data={
                "csrf_token": "test-csrf-token",
                "user_id": "999999",
                "email": "forged@example.net",
                "status": "completed",
                "start_date": "2027-01-01",
                "end_date": "2027-12-31",
                "admin_note": "Попытка подмены через approve",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.advertising_revision_row(request_id))
        live_after = self.advertisement_row(request_id)
        for field, expected in expected_payload.items():
            self.assertEqual(live_after[field], expected, field)
        for protected_field in (
            "user_id", "email", "status", "start_date", "end_date", "admin_note",
            "consent_at", "created_at",
        ):
            self.assertEqual(live_after[protected_field], live_before[protected_field], protected_field)
        self.assertEqual(self.advertising_metric_values(request_id), metrics_before)

        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(expected_payload["company"], html)
        self.assertIn(expected_payload["headline"], html)
        self.assertIn(expected_payload["ad_text"], html)
        self.assertNotIn("Старый бренд до одобрения", html)
        self.assertNotIn("Старый заголовок до одобрения", html)
        click_response = self.client.get(f"/advertising/click/{request_id}")
        self.assertEqual(click_response.headers["Location"], expected_payload["target_url"])

    def test_admin_schedule_note_save_rebases_pending_revision_and_allows_approval(self):
        owner_id = self.create_user(email="owner@gmail.com")
        admin_id = self.create_user(email="admin@gmail.com", name="Администратор", is_admin=1)
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Live-версия до безопасной настройки",
            admin_note="Старая заметка",
            **self.active_advertising_schedule(),
        )
        expected_payload = self.advertising_revision_values()
        self.login_as(owner_id)
        self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id),
        )
        revision_before = self.advertising_revision_row(request_id)
        self.assertIsNotNone(revision_before)
        new_start_date = (date.today() - timedelta(days=2)).isoformat()
        new_end_date = (date.today() + timedelta(days=60)).isoformat()

        self.login_as(admin_id)
        response = self.client.post(
            self.endpoint_url("update_advertising_request", request_id=request_id),
            data=self.admin_advertising_form(
                request_id,
                admin_note="Новая служебная заметка без пересечения",
                start_date=new_start_date,
                end_date=new_end_date,
            ),
        )
        self.assertEqual(response.status_code, 302)
        live_after_admin_save = self.advertisement_row(request_id)
        revision_after_admin_save = self.advertising_revision_row(request_id)
        self.assertEqual(live_after_admin_save["status"], "active")
        self.assertEqual(live_after_admin_save["admin_note"], "Новая служебная заметка без пересечения")
        self.assertEqual(live_after_admin_save["start_date"], new_start_date)
        self.assertEqual(live_after_admin_save["end_date"], new_end_date)
        self.assertEqual(revision_after_admin_save["id"], revision_before["id"])
        self.assertEqual(revision_after_admin_save["status"], "pending")
        self.assertEqual(revision_after_admin_save["payload_json"], revision_before["payload_json"])
        self.assertEqual(
            revision_after_admin_save["base_updated_at"], live_after_admin_save["updated_at"]
        )

        response = self.client.post(
            self.endpoint_url("admin_approve_advertising_revision", request_id=request_id),
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.advertising_revision_row(request_id))
        published = self.advertisement_row(request_id)
        for field, expected in expected_payload.items():
            self.assertEqual(published[field], expected, field)
        self.assertEqual(published["status"], "active")
        self.assertEqual(published["admin_note"], "Новая служебная заметка без пересечения")
        self.assertEqual(published["start_date"], new_start_date)
        self.assertEqual(published["end_date"], new_end_date)

    def test_admin_overlap_save_does_not_rebase_and_approval_refuses_stale_revision(self):
        owner_id = self.create_user(email="owner@gmail.com")
        admin_id = self.create_user(email="admin@gmail.com", name="Администратор", is_admin=1)
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Live-версия до пересекающейся настройки",
            headline="Исходный live-заголовок",
            **self.active_advertising_schedule(),
        )
        self.login_as(owner_id)
        self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id),
        )
        revision_before = self.advertising_revision_row(request_id)
        self.assertIsNotNone(revision_before)

        self.login_as(admin_id)
        response = self.client.post(
            self.endpoint_url("update_advertising_request", request_id=request_id),
            data=self.admin_advertising_form(
                request_id, headline="Новый live-заголовок администратора"
            ),
        )
        self.assertEqual(response.status_code, 302)
        live_after_admin_save = self.advertisement_row(request_id)
        revision_after_admin_save = self.advertising_revision_row(request_id)
        self.assertEqual(live_after_admin_save["headline"], "Новый live-заголовок администратора")
        self.assertEqual(revision_after_admin_save, revision_before)
        self.assertNotEqual(live_after_admin_save["updated_at"], revision_before["base_updated_at"])

        response = self.client.post(
            self.endpoint_url("admin_approve_advertising_revision", request_id=request_id),
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.endpoint_url("admin_advertising"))
        self.assertEqual(self.advertisement_row(request_id), live_after_admin_save)
        self.assertEqual(self.advertising_revision_row(request_id), revision_after_admin_save)

    def test_admin_rejection_preserves_live_ad_and_owner_resubmits_same_revision(self):
        owner_id = self.create_user(email="owner@gmail.com")
        admin_id = self.create_user(email="admin@gmail.com", name="Администратор", is_admin=1)
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Опубликованный бренд при отклонении",
            target_url="https://example.com/current-offer",
            headline="Опубликованный заголовок при отклонении",
            ad_text="Опубликованный текст при отклонении.",
            **self.active_advertising_schedule(),
        )
        live_before = self.advertisement_row(request_id)
        self.login_as(owner_id)
        self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id),
        )
        pending = self.advertising_revision_row(request_id)
        self.assertIsNotNone(pending)

        self.login_as(admin_id)
        response = self.client.post(
            self.endpoint_url("admin_reject_advertising_revision", request_id=request_id),
            data={"csrf_token": "test-csrf-token", "reason": "Некорректная категория"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.advertisement_row(request_id), live_before)
        rejected = self.advertising_revision_row(request_id)
        self.assertEqual(rejected["id"], pending["id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["moderation_reason"], "Некорректная категория")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Опубликованный бренд при отклонении", html)
        self.assertNotIn(self.advertising_revision_values()["company"], html)

        resubmitted_values = self.advertising_revision_values(
            company="Исправленный бренд после замечания",
            headline="Исправленный заголовок после замечания",
        )
        self.login_as(owner_id)
        response = self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id, **resubmitted_values),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.advertisement_row(request_id), live_before)
        resubmitted = self.advertising_revision_row(request_id)
        self.assertEqual(resubmitted["id"], pending["id"])
        self.assertEqual(resubmitted["submitted_at"], pending["submitted_at"])
        self.assertEqual(resubmitted["status"], "pending")
        self.assertIsNone(resubmitted["moderation_reason"])
        self.assertEqual(json.loads(resubmitted["payload_json"]), resubmitted_values)
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        revision_count = connection.execute(
            "SELECT COUNT(*) FROM advertising_revisions WHERE request_id=?", (request_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(revision_count, 1)

    def test_cross_owner_cannot_read_or_replace_revision_and_admin_actions_are_admin_only(self):
        owner_id = self.create_user(email="owner@gmail.com")
        attacker_id = self.create_user(email="attacker@gmail.com", name="Другой пользователь")
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            **self.active_advertising_schedule(),
        )
        self.login_as(owner_id)
        self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id),
        )
        live_before = self.advertisement_row(request_id)
        revision_before = self.advertising_revision_row(request_id)
        self.assertIsNotNone(revision_before)
        self.login_as(attacker_id)

        response = self.client.get(
            self.endpoint_url("account_advertising_edit", request_id=request_id)
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id, company="Чужая попытка подмены"),
        )
        self.assertEqual(response.status_code, 404)
        for endpoint, data in (
            ("admin_approve_advertising_revision", {"csrf_token": "test-csrf-token"}),
            ("admin_reject_advertising_revision", {
                "csrf_token": "test-csrf-token", "reason": "Некорректная категория",
            }),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(
                    self.endpoint_url(endpoint, request_id=request_id), data=data
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.advertisement_row(request_id), live_before)
        self.assertEqual(self.advertising_revision_row(request_id), revision_before)

    def test_admin_approval_rejects_noncanonical_revision_without_partial_update(self):
        owner_id = self.create_user(email="owner@gmail.com")
        admin_id = self.create_user(email="admin@gmail.com", name="Администратор", is_admin=1)
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Живая версия при ошибочном черновике",
            **self.active_advertising_schedule(),
        )
        self.login_as(owner_id)
        self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id),
        )
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.execute(
            "UPDATE advertising_revisions SET payload_json=? WHERE request_id=?",
            (json.dumps({"company": "Неполный и неканонический черновик"}), request_id),
        )
        connection.commit()
        connection.close()
        live_before = self.advertisement_row(request_id)
        revision_before = self.advertising_revision_row(request_id)

        self.login_as(admin_id)
        response = self.client.post(
            self.endpoint_url("admin_approve_advertising_revision", request_id=request_id),
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.endpoint_url("admin_advertising"))
        self.assertEqual(self.advertisement_row(request_id), live_before)
        self.assertEqual(self.advertising_revision_row(request_id), revision_before)

    def test_admin_approval_rejects_stale_revision_after_live_version_changed(self):
        owner_id = self.create_user(email="owner@gmail.com")
        admin_id = self.create_user(email="admin@gmail.com", name="Администратор", is_admin=1)
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Живая версия до параллельного изменения",
            **self.active_advertising_schedule(),
        )
        self.login_as(owner_id)
        self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id),
        )
        revision_before = self.advertising_revision_row(request_id)
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.execute(
            """UPDATE advertising_requests
                  SET admin_note=?, updated_at=? WHERE id=?""",
            ("Параллельное изменение администратора", "2099-01-01T00:00:00+00:00", request_id),
        )
        connection.commit()
        connection.close()
        live_after_concurrent_change = self.advertisement_row(request_id)

        self.login_as(admin_id)
        response = self.client.post(
            self.endpoint_url("admin_approve_advertising_revision", request_id=request_id),
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.endpoint_url("admin_advertising"))
        self.assertEqual(self.advertisement_row(request_id), live_after_concurrent_change)
        self.assertEqual(self.advertising_revision_row(request_id), revision_before)

    def test_non_active_owner_edit_updates_allowed_fields_but_preserves_protected_fields(self):
        owner_id = self.create_user(email="profile-owner@gmail.com")
        other_id = self.create_user(email="other@gmail.com", name="Другой пользователь")
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="profile-owner@gmail.com",
            status="rejected",
            admin_note="Замечание администратора",
            headline="Проверенный заголовок",
            ad_text="Проверенный рекламный текст",
            button_label="Проверенная кнопка",
            start_date="2026-09-01",
            end_date="2026-09-30",
        )
        self.login_as(owner_id)
        response = self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.valid_advertising_form(
                format="category-banner",
                contact_name="Новое контактное лицо",
                company="Обновлённый бренд",
                inn="0987654321",
                email="forged@example.net",
                phone="+78880000000",
                telegram="updated_brand",
                target_url="https://example.org/new-offer",
                category="services",
                cities="Москва, Казань",
                preferred_dates="Октябрь 2026",
                budget="10 000–30 000 ₽",
                comment="Обновлённая информация для повторной проверки.",
                user_id=str(other_id),
                status="active",
                admin_note="Подменённая заметка",
                headline="Обновлённый заголовок владельца",
                ad_text="Обновлённый рекламный текст владельца",
                button_label="Обновлённая кнопка",
                start_date="2027-01-01",
                end_date="2027-12-31",
            ),
        )
        self.assertEqual(response.status_code, 302)
        row = self.advertisement_row(request_id)
        self.assertEqual(row["user_id"], owner_id)
        self.assertEqual(row["email"], "profile-owner@gmail.com")
        self.assertEqual(row["status"], "new")
        self.assertEqual(row["format"], "category-banner")
        self.assertEqual(row["contact_name"], "Новое контактное лицо")
        self.assertEqual(row["company"], "Обновлённый бренд")
        self.assertEqual(row["inn"], "0987654321")
        self.assertEqual(row["phone"], "+78880000000")
        self.assertEqual(row["telegram"], "updated_brand")
        self.assertEqual(row["target_url"], "https://example.org/new-offer")
        self.assertEqual(row["category"], "services")
        self.assertEqual(row["cities"], "Москва, Казань")
        self.assertEqual(row["preferred_dates"], "Октябрь 2026")
        self.assertEqual(row["budget"], "10 000–30 000 ₽")
        self.assertEqual(row["comment"], "Обновлённая информация для повторной проверки.")
        self.assertEqual(row["admin_note"], "Замечание администратора")
        self.assertEqual(row["headline"], "Обновлённый заголовок владельца")
        self.assertEqual(row["ad_text"], "Обновлённый рекламный текст владельца")
        self.assertEqual(row["button_label"], "Обновлённая кнопка")
        self.assertEqual(row["start_date"], "2026-09-01")
        self.assertEqual(row["end_date"], "2026-09-30")

    def test_finishing_active_advertisement_discards_pending_revision(self):
        owner_id = self.create_user(email="owner@gmail.com")
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="active",
            company="Опубликованный бренд",
        )
        self.login_as(owner_id)
        response = self.client.post(
            self.endpoint_url("account_advertising_edit", request_id=request_id),
            data=self.active_revision_form(request_id, company="Изменение перед остановкой"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.endpoint_url("account_advertising"))
        self.assertEqual(self.advertisement_row(request_id)["company"], "Опубликованный бренд")
        self.assertEqual(self.advertisement_row(request_id)["status"], "active")
        self.assertIsNotNone(self.advertising_revision_row(request_id))

        response = self.client.post(
            self.endpoint_url("account_advertising_finish", request_id=request_id),
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.advertisement_row(request_id)["status"], "completed")
        self.assertIsNone(self.advertising_revision_row(request_id))

    def test_completed_advertisement_can_be_resubmitted_for_moderation(self):
        owner_id = self.create_user(email="owner@gmail.com")
        request_id = self.create_advertisement(
            user_id=owner_id,
            email="owner@gmail.com",
            status="completed",
            admin_note="Прошлая служебная заметка",
        )
        self.login_as(owner_id)
        response = self.client.post(
            self.endpoint_url("account_advertising_resubmit", request_id=request_id),
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        row = self.advertisement_row(request_id)
        self.assertEqual(row["status"], "new")

    def test_account_tabs_link_to_my_advertising(self):
        owner_id = self.create_user(email="owner@gmail.com")
        self.login_as(owner_id)
        advertising_path = self.endpoint_url("account_advertising")
        account_paths = (
            self.endpoint_url("account"),
            self.endpoint_url("messages_list"),
            self.endpoint_url("favorites"),
            self.endpoint_url("settings"),
            advertising_path,
        )
        for path in account_paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Моя реклама", html)
                self.assertIn(f'href="{advertising_path}"', html)

    def test_active_homepage_banner_is_public_and_private_fields_are_hidden(self):
        request_id = self.create_advertisement()
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count(f'data-advertisement-id="{request_id}"'), 1)
        self.assertIn("Полезное предложение", html)
        self.assertIn("Реклама", html)
        for private_value in (
            "private@example.com", "+79990000000", "private_contact", "1234567890",
            "PRIVATE-CONTACT-XYZ", "PRIVATE-DATE-XYZ", "PRIVATE-BUDGET-XYZ",
            "PRIVATE-COMMENT-XYZ", "Скрытая заметка",
        ):
            self.assertNotIn(private_value, html)

    def test_click_is_counted_and_redirects_to_stored_target(self):
        request_id = self.create_advertisement()
        response = self.client.get(f"/advertising/click/{request_id}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "https://example.com/offer")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        count = connection.execute(
            "SELECT value FROM advertising_daily_metrics WHERE request_id=? AND metric='click'",
            (request_id,),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_admin_activation_publishes_configured_banner(self):
        request_id = self.create_advertisement(status="ready", headline="", ad_text="")
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO users
               (name, email, password_hash, created_at, email_verified, is_admin, session_version)
               VALUES ('Администратор', 'admin@example.com', 'test', ?, 1, 1, 1)""",
            (zooland.utcnow(),),
        )
        admin_id = cursor.lastrowid
        connection.commit()
        connection.close()
        with self.client.session_transaction() as session_data:
            session_data["user_id"] = admin_id
            session_data["session_version"] = 1
            session_data["csrf_token"] = "test-csrf-token"
        response = self.client.post(
            f"/admin/advertising/{request_id}",
            data={
                "csrf_token": "test-csrf-token",
                "status": "active",
                "format": "homepage-banner",
                "category": "all",
                "target_url": "https://example.com/offer",
                "cities": "",
                "headline": "Новый баннер",
                "ad_text": "Публичный текст баннера",
                "button_label": "Открыть",
                "start_date": "",
                "end_date": "",
                "admin_note": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(f'data-advertisement-id="{request_id}"', html)
        self.assertIn("Новый баннер", html)

    def test_inactive_and_expired_advertisements_are_hidden(self):
        inactive_id = self.create_advertisement(status="ready", headline="Неактивная реклама")
        expired_id = self.create_advertisement(
            headline="Просроченная реклама",
            end_date=(date.today() - timedelta(days=1)).isoformat(),
        )
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertNotIn(f'data-advertisement-id="{inactive_id}"', html)
        self.assertNotIn(f'data-advertisement-id="{expired_id}"', html)
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        status = connection.execute("SELECT status FROM advertising_requests WHERE id=?", (expired_id,)).fetchone()[0]
        connection.close()
        self.assertEqual(status, "completed")

    def test_cleanup_expired_discards_only_revisions_of_expired_active_advertisements(self):
        owner_id = self.create_user(email="owner@gmail.com")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        expired_pending_id = self.create_advertisement(
            user_id=owner_id, email="owner@gmail.com", status="active", end_date=yesterday,
            company="Истёкшая реклама с pending-правкой",
        )
        expired_rejected_id = self.create_advertisement(
            user_id=owner_id, email="owner@gmail.com", status="active", end_date=yesterday,
            company="Истёкшая реклама с rejected-правкой",
        )
        live_pending_id = self.create_advertisement(
            user_id=owner_id, email="owner@gmail.com", status="active", end_date=tomorrow,
            company="Действующая реклама с pending-правкой",
        )
        live_rejected_id = self.create_advertisement(
            user_id=owner_id, email="owner@gmail.com", status="active", end_date=tomorrow,
            company="Действующая реклама с rejected-правкой",
        )
        request_statuses = (
            (expired_pending_id, "pending", None),
            (expired_rejected_id, "rejected", "Некорректная категория"),
            (live_pending_id, "pending", None),
            (live_rejected_id, "rejected", "Некорректная категория"),
        )
        now = zooland.utcnow()
        payload_json = json.dumps(
            self.advertising_revision_values(), ensure_ascii=False, separators=(",", ":")
        )
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        for request_id, revision_status, reason in request_statuses:
            base_updated_at = connection.execute(
                "SELECT updated_at FROM advertising_requests WHERE id=?", (request_id,)
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO advertising_revisions
                   (request_id, owner_id, payload_json, status, moderation_reason,
                    base_updated_at, submitted_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, owner_id, payload_json, revision_status, reason,
                 base_updated_at, now, now),
            )
        connection.commit()
        connection.close()
        live_pending_before = self.advertising_revision_row(live_pending_id)
        live_rejected_before = self.advertising_revision_row(live_rejected_id)
        live_pending_ad_before = self.advertisement_row(live_pending_id)
        live_rejected_ad_before = self.advertisement_row(live_rejected_id)

        with zooland.app.app_context():
            zooland.cleanup_expired()

        self.assertEqual(self.advertisement_row(expired_pending_id)["status"], "completed")
        self.assertEqual(self.advertisement_row(expired_rejected_id)["status"], "completed")
        self.assertIsNone(self.advertising_revision_row(expired_pending_id))
        self.assertIsNone(self.advertising_revision_row(expired_rejected_id))
        self.assertEqual(self.advertisement_row(live_pending_id), live_pending_ad_before)
        self.assertEqual(self.advertisement_row(live_rejected_id), live_rejected_ad_before)
        self.assertEqual(self.advertising_revision_row(live_pending_id), live_pending_before)
        self.assertEqual(self.advertising_revision_row(live_rejected_id), live_rejected_before)

    def test_category_banner_appears_only_in_its_catalog(self):
        request_id = self.create_advertisement(format="category-banner", category="services")
        services_html = self.client.get("/services").get_data(as_text=True)
        food_html = self.client.get("/food").get_data(as_text=True)
        self.assertIn(f'data-advertisement-id="{request_id}"', services_html)
        self.assertNotIn(f'data-advertisement-id="{request_id}"', food_html)

    def test_promoted_listing_is_lifted_and_marked(self):
        now = zooland.utcnow()
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO services
               (type, title, pet_types, city, price, description, status, created_at, expires_at, deal_status)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, 'open')""",
            ("Груминг", "Продвигаемая услуга", "Собаки", "Москва", 1000, "Описание", now,
             (date.today() + timedelta(days=30)).isoformat()),
        )
        listing_id = cursor.lastrowid
        connection.commit()
        connection.close()
        request_id = self.create_advertisement(
            format="promoted-listing", category="services", target_url=f"/service/{listing_id}"
        )
        html = self.client.get("/services").get_data(as_text=True)
        self.assertIn(f'data-advertisement-id="{request_id}"', html)
        self.assertIn("Реклама · Продвигается", html)
        self.assertIn(f'/advertising/click/{request_id}', html)

    def test_irrelevant_new_promotions_do_not_starve_matching_listing(self):
        now = zooland.utcnow()
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO services
               (type, title, pet_types, city, price, description, status, created_at, expires_at, deal_status)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, 'open')""",
            ("Передержка", "Нужная услуга", "Кошки", "Москва", 800, "Описание", now,
             (date.today() + timedelta(days=30)).isoformat()),
        )
        listing_id = cursor.lastrowid
        connection.commit()
        connection.close()
        matching_id = self.create_advertisement(
            format="promoted-listing", category="all", target_url=f"/service/{listing_id}"
        )
        for number in range(6):
            self.create_advertisement(
                format="promoted-listing", category="all", target_url=f"/animal/{9000 + number}"
            )
        html = self.client.get("/services").get_data(as_text=True)
        self.assertIn(f'data-advertisement-id="{matching_id}"', html)

    def test_unsafe_targets_are_rejected(self):
        for target in ("javascript:alert(1)", "//evil.example", "/\\evil.example", "https://user:pass@example.com", "http://["):
            with self.subTest(target=target):
                self.assertIsNone(zooland.valid_advertising_target(target))
        self.assertFalse(zooland.advertising_date_is_valid("2026-1-2"))
        self.assertTrue(zooland.advertising_date_is_valid("2026-01-02"))

    def test_representative_public_routes_still_render(self):
        for path in ("/", "/services", "/food", "/accessories", "/advertising", "/login", "/vacancies", "/smartphone-app"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_smartphone_app_page_is_public_linked_and_active_for_users(self):
        response = self.client.get("/smartphone-app")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("<title>Приложение на смартфон — ZooLand</title>", html)
        self.assertIn('id="smartphone-app-title">Приложение на смартфон</h1>', html)
        self.assertIn('name="viewport"', html)
        self.assertIn('src="/static/smartphone-app-dog.webp"', html)
        self.assertIn(
            'alt="Бежево-коричневая собака показывает приложение ZooLand на смартфоне"',
            html,
        )
        self.assertIn('href="/"', html)
        self.assertNotIn('aria-current="page">Приложение на смартфон', html)

        image_response = self.client.get("/static/smartphone-app-dog.webp")
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response.mimetype, "image/webp")
        image_response.close()

        for path in ("/", "/services", "/vacancies"):
            with self.subTest(footer_path=path):
                page = self.client.get(path).get_data(as_text=True)
                self.assertIn('href="/smartphone-app">Приложение на смартфон</a>', page)

        user_id = self.create_user(email="mobile-user@example.com", name="Мобильный пользователь")
        self.login_as(user_id)
        account_html = self.client.get("/smartphone-app").get_data(as_text=True)
        self.assertIn(
            'class="account-tab-wide-mobile active" aria-current="page">Приложение на смартфон</a>',
            account_html,
        )


if __name__ == "__main__":
    unittest.main()
