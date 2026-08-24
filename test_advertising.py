import atexit
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


class AdvertisingPublicationTests(unittest.TestCase):
    def setUp(self):
        zooland._request_log.clear()
        zooland._ban_count.clear()
        zooland._banned_until.clear()
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        for table in (
            "advertising_daily_metrics", "advertising_requests", "rate_limit_events",
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

    def test_owner_edit_updates_request_but_preserves_moderated_fields(self):
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
                headline="Подменённый заголовок",
                ad_text="Подменённый рекламный текст",
                button_label="Подменённая кнопка",
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
        self.assertEqual(row["headline"], "Проверенный заголовок")
        self.assertEqual(row["ad_text"], "Проверенный рекламный текст")
        self.assertEqual(row["button_label"], "Проверенная кнопка")
        self.assertEqual(row["start_date"], "2026-09-01")
        self.assertEqual(row["end_date"], "2026-09-30")

    def test_active_advertisement_cannot_be_edited_but_can_be_finished(self):
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
            data=self.valid_advertising_form(company="Несогласованное изменение"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.endpoint_url("account_advertising"))
        self.assertEqual(self.advertisement_row(request_id)["company"], "Опубликованный бренд")
        self.assertEqual(self.advertisement_row(request_id)["status"], "active")

        response = self.client.post(
            self.endpoint_url("account_advertising_finish", request_id=request_id),
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.advertisement_row(request_id)["status"], "completed")

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
        for path in ("/", "/services", "/food", "/accessories", "/advertising", "/login", "/vacancies"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
