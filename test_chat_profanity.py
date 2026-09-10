import atexit
import os
import sys
import tempfile
import unittest


# An isolated database is mandatory when this file is run directly.  During
# full discovery ``test_advertising`` may already have imported ``app`` with
# its own temporary database; in that case re-use that already-isolated app.
TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="zooland-chat-profanity-")
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


class ChatProfanityMaskTests(unittest.TestCase):
    def setUp(self):
        zooland._request_log.clear()
        zooland._ban_count.clear()
        zooland._banned_until.clear()
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        for table in (
            "messages",
            "dialog_states",
            "dialog_blocks",
            "rate_limit_events",
            "listing_revisions",
            "animals",
            "users",
        ):
            connection.execute(f"DELETE FROM {table}")
        connection.commit()
        connection.close()
        self.client = zooland.app.test_client()

        self.owner_id = self.create_user("owner@example.com", "Владелец")
        self.sender_id = self.create_user("sender@example.com", "Покупатель")
        self.stranger_id = self.create_user("stranger@example.com", "Посторонний")
        self.listing_id = self.create_animal(self.owner_id)

    def create_user(self, email, name):
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO users
               (name, email, password_hash, created_at, email_verified, is_admin, session_version)
               VALUES (?, ?, 'test-password-hash', ?, 1, 0, 1)""",
            (name, email, zooland.utcnow()),
        )
        connection.commit()
        user_id = cursor.lastrowid
        connection.close()
        return user_id

    def create_animal(self, owner_id):
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO animals
               (type, breed, age, price, city, description, contacts, user_id,
                status, created_at, expires_at, deal_status)
               VALUES ('Собака', 'Лабрадор', 2, 1000, 'Москва', 'Описание',
                       '+79990000000', ?, 'active', ?, NULL, 'open')""",
            (owner_id, zooland.utcnow()),
        )
        connection.commit()
        listing_id = cursor.lastrowid
        connection.close()
        return listing_id

    def login_as(self, user_id):
        with self.client.session_transaction() as session_data:
            session_data.clear()
            session_data["user_id"] = user_id
            session_data["session_version"] = 1
            session_data["csrf_token"] = "test-csrf-token"

    def thread_url(self, other_id):
        return (
            f"/messages/animal/{self.listing_id}"
            f"?other_id={other_id}"
        )

    def send_url(self, other_id):
        return (
            f"/messages/animal/{self.listing_id}/send"
            f"?other_id={other_id}"
        )

    def send_message(self, body, receiver_id=None):
        receiver_id = receiver_id or self.owner_id
        return self.client.post(
            self.send_url(receiver_id),
            data={
                "csrf_token": "test-csrf-token",
                "other_id": str(receiver_id),
                "body": body,
            },
        )

    def stored_messages(self):
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.row_factory = zooland.sqlite3.Row
        rows = connection.execute("SELECT * FROM messages ORDER BY id").fetchall()
        connection.close()
        return [dict(row) for row in rows]

    def assert_masked(self, source):
        result = zooland.mask_chat_profanity(source)
        self.assertNotEqual(source, result)
        self.assertNotIn(source.casefold(), result.casefold())
        self.assertIn("*", result)
        self.assertEqual(len(result), len(source))

    def test_empty_values_have_a_stable_string_result(self):
        self.assertEqual(zooland.mask_chat_profanity(None), "")
        self.assertEqual(zooland.mask_chat_profanity(""), "")

    def test_safe_russian_text_urls_and_emails_are_unchanged(self):
        safe_texts = (
            "Добрый день! Щенок ещё продаётся?",
            "Магазин страхует груз, а коллега подстрахует заказ.",
            "Цена два рубля, доставка для корабля, дубляж и бляха-муха.",
            "Не оскорбляйте людей, употребляйте слова, купите хлеб и мебель.",
            "Команда получила мандат, купила мандарин и записалась на педикюр.",
            "Херсон, херес и херувим — обычные слова и имена.",
            "Сука лабрадора здорова и готова к осмотру ветеринаром.",
            "Подстрахуй меня, пожалуйста; we can debate the delivery tomorrow.",
            "Сайт https://example.com/catalog?q=hello, почта support@example.com.",
            "Страница https://example.com/debate и адрес debate@example.com безопасны.",
            "Телефон +7 (999) 123-45-67 тоже должен остаться прежним.",
        )
        for source in safe_texts:
            with self.subTest(source=source):
                self.assertEqual(zooland.mask_chat_profanity(source), source)

    def test_canonical_words_mixed_case_and_suffixes_are_masked(self):
        variants = (
            "хуй",
            "ХУЙ",
            "пизда",
            "ПиЗдЕц",
            "ебать",
            "ёбаный",
            "блядь",
            "БЛЯДСКИЙ",
            "мудак",
            "мудачина",
            "долбоёб",
            "гандон",
            "гондон",
            "нихуя",
            "охуеть",
            "заебал",
            "уёбок",
            "хуесос",
            "шлюха",
            "пидор",
            "залупа",
        )
        for source in variants:
            with self.subTest(source=source):
                self.assert_masked(source)

    def test_latin_transliteration_leet_and_fullwidth_forms_are_masked(self):
        variants = (
            "huy",
            "hui",
            "pizda",
            "blyat",
            "ebat",
            "п1зда",
            "пи3да",
            "6лядь",
            "е6ать",
            "ｈｕｙ",
            "ｐｉｚｄａ",
            "ｂｌｙａｔ",
            "ｅｂａｔ",
        )
        for source in variants:
            with self.subTest(source=source):
                self.assert_masked(source)

    def test_homoglyphs_separators_and_repeated_letters_do_not_bypass_mask(self):
        variants = (
            "xуй",
            "хyй",
            "x-y-й",
            "х.у-й",
            "х\u200bу\u200dй",
            "х🤬у🤬й",
            "п и з д а",
            "п-и-з-д-е-ц",
            "п_и_з_д_а",
            "е.б-а т ь",
            "б...л...я...дь",
            "хххуууй",
            "ппиииззздаа",
            "ееебааать",
            "мммууудаак",
        )
        for source in variants:
            with self.subTest(source=source):
                self.assert_masked(source)

    def test_mask_preserves_separators_and_is_idempotent(self):
        source = "Фраза: х.у-й, а затем П-И-З-Д-А!"
        expected = "Фраза: *.*-*, а затем *-*-*-*-*!"
        masked = zooland.mask_chat_profanity(source)
        self.assertEqual(masked, expected)
        self.assertEqual(zooland.mask_chat_profanity(masked), masked)

    def test_send_masks_storage_threads_dialog_previews_and_notification(self):
        source = "Это х.у-й и ПИЗДЕЦ, но обычный текст остаётся."
        self.login_as(self.sender_id)
        response = self.send_message(source)
        self.assertEqual(response.status_code, 302)

        rows = self.stored_messages()
        self.assertEqual(len(rows), 1)
        stored = rows[0]["body"]
        self.assertNotEqual(stored, source)
        self.assertNotIn("х.у-й", stored.casefold())
        self.assertNotIn("пиздец", stored.casefold())
        self.assertIn("обычный текст остаётся", stored)
        self.assertIn("*", stored)

        # The sender sees only the sanitized database value in both surfaces.
        sender_thread = self.client.get(self.thread_url(self.owner_id)).get_data(as_text=True)
        sender_dialogs = self.client.get("/messages").get_data(as_text=True)
        for html in (sender_thread, sender_dialogs):
            self.assertNotIn("х.у-й", html.casefold())
            self.assertNotIn("пиздец", html.casefold())
            self.assertIn("*", html)

        # The unread notification is checked before opening the recipient's
        # thread because that page deliberately marks incoming messages read.
        self.login_as(self.owner_id)
        notification = self.client.get("/notifications/unread")
        self.assertEqual(notification.status_code, 200)
        payload = notification.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["body"], stored)
        self.assertNotIn("х.у-й", payload["body"].casefold())
        self.assertNotIn("пиздец", payload["body"].casefold())

        recipient_dialogs = self.client.get("/messages").get_data(as_text=True)
        recipient_thread = self.client.get(self.thread_url(self.sender_id)).get_data(as_text=True)
        for html in (recipient_dialogs, recipient_thread):
            self.assertNotIn("х.у-й", html.casefold())
            self.assertNotIn("пиздец", html.casefold())
            self.assertIn("*", html)

    def test_legacy_raw_message_is_masked_on_every_read_surface_and_html_escaped(self):
        source = "Старое х.у-й <script>alert('xss')</script>"
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.execute(
            """INSERT INTO messages
               (listing_type, listing_id, sender_id, receiver_id, body, created_at)
               VALUES ('animal', ?, ?, ?, ?, ?)""",
            (self.listing_id, self.sender_id, self.owner_id, source, zooland.utcnow()),
        )
        connection.commit()
        connection.close()

        self.login_as(self.owner_id)
        notification = self.client.get("/notifications/unread").get_json()
        self.assertEqual(notification["count"], 1)
        self.assertNotIn("х.у-й", notification["body"].casefold())
        self.assertIn("*.*-*", notification["body"])

        dialogs = self.client.get("/messages").get_data(as_text=True)
        thread = self.client.get(self.thread_url(self.sender_id)).get_data(as_text=True)
        for html in (dialogs, thread):
            self.assertNotIn("х.у-й", html.casefold())
            self.assertNotIn("<script>alert", html.casefold())
            self.assertIn("&lt;script&gt;", html)
            self.assertIn("*.*-*", html)

    def test_chat_masks_sender_display_name_without_mutating_profile(self):
        raw_name = "ПИЗДЕЦ х.у-й"
        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        connection.execute("UPDATE users SET name=? WHERE id=?", (raw_name, self.sender_id))
        connection.commit()
        connection.close()

        self.login_as(self.sender_id)
        response = self.send_message("Здравствуйте, объявление актуально?")
        self.assertEqual(response.status_code, 302)

        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        stored_name = connection.execute(
            "SELECT name FROM users WHERE id=?", (self.sender_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(stored_name, raw_name)

        self.login_as(self.owner_id)
        notification = self.client.get("/notifications/unread").get_json()
        self.assertNotEqual(notification["sender"], raw_name)
        self.assertNotIn("пиздец", notification["sender"].casefold())
        self.assertNotIn("х.у-й", notification["sender"].casefold())
        self.assertIn("*", notification["sender"])

        dialogs = self.client.get("/messages").get_data(as_text=True)
        thread = self.client.get(self.thread_url(self.sender_id)).get_data(as_text=True)
        for html in (dialogs, thread):
            self.assertNotIn("пиздец", html.casefold())
            self.assertNotIn("х.у-й", html.casefold())
            self.assertNotIn(raw_name, html)
            self.assertIn("*", html)

        connection = zooland.sqlite3.connect(zooland.DB_NAME)
        stored_name_after_reads = connection.execute(
            "SELECT name FROM users WHERE id=?", (self.sender_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(stored_name_after_reads, raw_name)

    def test_filter_does_not_weaken_chat_auth_or_participant_checks(self):
        with self.client.session_transaction() as session_data:
            session_data.clear()
            session_data["csrf_token"] = "test-csrf-token"
        anonymous = self.send_message("хуй")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/login", anonymous.headers["Location"])
        self.assertEqual(self.stored_messages(), [])

        self.login_as(self.stranger_id)
        forced = self.send_message("хуй", receiver_id=self.sender_id)
        self.assertEqual(forced.status_code, 403)
        self.assertEqual(self.stored_messages(), [])


if __name__ == "__main__":
    unittest.main()
