import atexit
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest import mock


TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="zooland-locations-i18n-")
atexit.register(TEST_DIRECTORY.cleanup)
TEST_DB_PATH = os.path.join(TEST_DIRECTORY.name, "zooland-test.db")

# Several test modules share the already imported application.  Configure a
# private database only when this module is the first one to import app.py.
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
    raise RuntimeError("Location/i18n tests require a temporary database")

zooland.init_db()


class LocationAndI18nTests(unittest.TestCase):
    def setUp(self):
        self.token = uuid.uuid4().hex
        self.user_ids = []
        self.animal_ids = []
        self.remote_addr = f"locations-i18n-{self.token}"
        self.previous_housekeeping_at = zooland._next_housekeeping_at
        self.previous_rate_cleanup = zooland._next_rate_cleanup
        zooland._next_housekeeping_at = float("inf")
        zooland._next_rate_cleanup = float("inf")
        self.client = zooland.app.test_client()
        self.client.environ_base["REMOTE_ADDR"] = self.remote_addr

    def tearDown(self):
        try:
            connection = sqlite3.connect(zooland.DB_NAME)
            if self.animal_ids:
                placeholders = ",".join("?" for _ in self.animal_ids)
                connection.execute(
                    f"DELETE FROM listing_revisions WHERE listing_type='animal' "
                    f"AND listing_id IN ({placeholders})",
                    self.animal_ids,
                )
                connection.execute(
                    f"DELETE FROM animals WHERE id IN ({placeholders})",
                    self.animal_ids,
                )
            if self.user_ids:
                placeholders = ",".join("?" for _ in self.user_ids)
                connection.execute(
                    f"DELETE FROM users WHERE id IN ({placeholders})",
                    self.user_ids,
                )
            connection.commit()
            connection.close()
        finally:
            zooland._request_log.pop(self.remote_addr, None)
            zooland._ban_count.pop(self.remote_addr, None)
            zooland._banned_until.pop(self.remote_addr, None)
            zooland._next_housekeeping_at = self.previous_housekeeping_at
            zooland._next_rate_cleanup = self.previous_rate_cleanup

    def create_user(self):
        connection = sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO users
                   (name, email, phone, password_hash, created_at,
                    email_verified, is_admin, session_version)
               VALUES (?, ?, ?, 'test-password-hash', ?, 1, 0, 1)""",
            (
                "Проверка географии",
                f"locations-i18n-{self.token}-{len(self.user_ids)}@example.com",
                f"+9955{uuid.uuid4().int % 10_000_000:07d}",
                zooland.utcnow(),
            ),
        )
        connection.commit()
        user_id = cursor.lastrowid
        connection.close()
        self.user_ids.append(user_id)
        return user_id

    def login_as(self, user_id):
        with self.client.session_transaction() as session_data:
            session_data.clear()
            session_data["user_id"] = user_id
            session_data["session_version"] = 1
            session_data["csrf_token"] = "test-csrf-token"

    def create_public_animal(self, owner_id, *, country_code, city, breed):
        connection = sqlite3.connect(zooland.DB_NAME)
        cursor = connection.execute(
            """INSERT INTO animals
                   (type, breed, age, price, country_code, city, description,
                    contacts, photo, user_id, status, created_at, expires_at,
                    deal_type, contact_methods, deal_status)
               VALUES ('Кот', ?, 2, 1000, ?, ?, 'Тестовое объявление',
                       '+995555123456', '', ?, 'active', ?, NULL,
                       'sale', 'phone', 'open')""",
            (breed, country_code, city, owner_id, zooland.utcnow()),
        )
        connection.commit()
        animal_id = cursor.lastrowid
        connection.close()
        self.animal_ids.append(animal_id)
        return animal_id

    def test_locations_json_contains_ru_ge_and_localized_tbilisi(self):
        response = self.client.get("/static/locations.json")
        try:
            self.assertEqual(response.status_code, 200)
            locations = json.loads(response.get_data(as_text=True))
        finally:
            response.close()
        self.assertEqual(set(locations["countries"]), {"RU", "GE"})

        russian_source_path = os.path.join(
            zooland.PROJECT_ROOT, "static", "russian-cities.json"
        )
        with open(russian_source_path, encoding="utf-8") as source_file:
            russian_source = json.load(source_file)
        russian_locations = locations["countries"]["RU"]["cities"]
        self.assertEqual(len(russian_locations), len(russian_source))
        self.assertEqual(
            [
                (item["names"]["ru"], item["lat"], item["lon"])
                for item in russian_locations
            ],
            [
                (item["name"], float(item["coords"]["lat"]), float(item["coords"]["lon"]))
                for item in russian_source
            ],
        )

        tbilisi = next(
            city
            for city in locations["countries"]["GE"]["cities"]
            if city["names"]["ru"] == "Тбилиси"
        )
        self.assertEqual(
            {language: tbilisi["names"][language] for language in ("ru", "en", "ka")},
            {"ru": "Тбилиси", "en": "Tbilisi", "ka": "თბილისი"},
        )
        self.assertIsInstance(tbilisi["lat"], (int, float))
        self.assertIsInstance(tbilisi["lon"], (int, float))

    def test_init_db_sets_ru_country_default_on_location_tables(self):
        zooland.init_db()
        connection = sqlite3.connect(zooland.DB_NAME)
        try:
            for table in ("users", "animals", "services", "food"):
                with self.subTest(table=table):
                    columns = {
                        row[1]: row
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    }
                    self.assertIn("country_code", columns)
                    country_column = columns["country_code"]
                    self.assertEqual(country_column[3], 1, "country_code must be NOT NULL")
                    default_value = str(country_column[4] or "").strip()
                    default_value = default_value.strip("()").strip("'\"")
                    self.assertEqual(default_value, "RU")
        finally:
            connection.close()

    def test_normalize_listing_location_enforces_country_city_membership(self):
        self.assertEqual(
            zooland.normalize_listing_location(
                {"country": "ge", "city": "  Тбилиси  "}
            ),
            ("GE", "Тбилиси", None),
        )

        country_code, city, error = zooland.normalize_listing_location(
            {"country": "GE", "city": "Москва"}
        )
        self.assertEqual((country_code, city), ("GE", "Москва"))
        self.assertTrue(error)
        self.assertIn("выбранной страны", error)

    def test_authenticated_add_stores_georgia_country_code(self):
        user_id = self.create_user()
        self.login_as(user_id)
        breed = f"GeoPost-{self.token}"
        form = {
            "csrf_token": "test-csrf-token",
            "type": "Кот",
            "breed": breed,
            "age": "2",
            "price": "1500",
            "country": "GE",
            "city": "Тбилиси",
            "description": "Проверка сохранения страны объявления.",
            "deal_type": "sale",
            "phone_source": "profile",
            "contact_methods": ["phone", "chat"],
        }

        with (
            mock.patch.object(zooland, "allow_sensitive_action", return_value=True),
            mock.patch.object(zooland, "save_photos", return_value=[]),
        ):
            response = self.client.post("/add", data=form)

        connection = sqlite3.connect(zooland.DB_NAME)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT id, user_id, country_code, city, status
                 FROM animals WHERE user_id=? AND breed=?""",
            (user_id, breed),
        ).fetchone()
        connection.close()
        if row:
            self.animal_ids.append(row["id"])

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/account"))
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], user_id)
        self.assertEqual((row["country_code"], row["city"]), ("GE", "Тбилиси"))
        self.assertEqual(row["status"], "pending")

    def test_catalog_filter_keeps_georgia_and_russia_separate(self):
        owner_id = self.create_user()
        ge_marker = f"VisibleGE-{self.token}"
        ru_marker = f"HiddenRU-{self.token}"
        self.create_public_animal(
            owner_id,
            country_code="GE",
            city="Тбилиси",
            breed=ge_marker,
        )
        # This intentionally malformed legacy row proves that the SQL filter
        # uses both columns instead of relying on city names being globally unique.
        self.create_public_animal(
            owner_id,
            country_code="RU",
            city="Тбилиси",
            breed=ru_marker,
        )

        response = self.client.get(
            "/",
            query_string={"country": "GE", "city": "Тбилиси", "kind": "animals"},
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(ge_marker, html)
        self.assertNotIn(ru_marker, html)
        self.assertIn('data-location-country="GE"', html)
        self.assertIn('data-location-city="Тбилиси"', html)

        invalid_response = self.client.get(
            "/", query_string={"country": "GE", "city": "Москва"}
        )
        self.assertEqual(invalid_response.status_code, 400)

    def test_public_catalog_pages_render_location_and_i18n_assets(self):
        country_select = re.compile(r"<select\b[^>]*\bdata-country-select(?:\s|=|>)")
        city_select = re.compile(r"<select\b[^>]*\bdata-city-select(?:\s|=|>)")
        optional_country = re.compile(
            r"<select\b(?=[^>]*\bdata-country-select\b)"
            r"(?=[^>]*\bdata-allow-empty\b)[^>]*>",
            re.IGNORECASE,
        )
        optional_city = re.compile(
            r"<select\b(?=[^>]*\bdata-city-select\b)"
            r"(?=[^>]*\bdata-allow-empty\b)(?=[^>]*\bdisabled\b)[^>]*>",
            re.IGNORECASE,
        )
        script_asset = lambda filename: re.compile(
            rf"<script\b[^>]*\bsrc=[\"'][^\"']*{re.escape(filename)}(?:\?[^\"']*)?[\"']",
            re.IGNORECASE,
        )

        for path in ("/", "/services", "/food"):
            with self.subTest(path=path):
                response = self.client.get(path)
                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertRegex(html, country_select)
                self.assertRegex(html, city_select)
                country_match = optional_country.search(html)
                self.assertIsNotNone(country_match)
                self.assertIn('data-selected-country=""', country_match.group(0))
                self.assertRegex(html, optional_city)
                self.assertIn("data-language-switcher", html)
                self.assertRegex(html, script_asset("i18n.js"))
                self.assertRegex(html, script_asset("location-picker.js"))

        for asset_path in (
            "/static/i18n.js",
            "/static/location-picker.js",
            "/static/locations.json",
        ):
            with self.subTest(asset=asset_path):
                response = self.client.get(asset_path)
                try:
                    self.assertEqual(response.status_code, 200)
                    if asset_path.endswith("location-picker.js"):
                        picker_source = response.get_data(as_text=True)
                        for translated_label in (
                            "Любая страна",
                            "Any country",
                            "ნებისმიერი ქვეყანა",
                        ):
                            self.assertIn(translated_label, picker_source)
                finally:
                    response.close()

    def test_georgian_language_button_uses_ge_label_and_ka_language_code(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertRegex(html, r'data-language="ka"[^>]*>GE</button>')
        self.assertNotRegex(html, r'data-language="ka"[^>]*>KA</button>')


if __name__ == "__main__":
    unittest.main()
