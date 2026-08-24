import json
import hmac
import math
import os
import re
import secrets
import sqlite3
import time
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from hashlib import sha256
from functools import wraps
from urllib.parse import urljoin, urlparse

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from catalog import ACCESSORY_TYPES, ANIMAL_TYPES, BREEDS, DEAL_TYPES, FOOD_TYPES, PRODUCT_TYPES
from image_security import ImageSafetyError, sanitize_upload
from services import SERVICE_DAYS, SERVICE_TYPES, PET_TYPES


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def load_local_env(path):
    """Загружает локальный .env, не перезаписывая переменные production-сервера."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip("\"'")
            if key:
                os.environ.setdefault(key, value)


load_local_env(os.path.join(PROJECT_ROOT, ".env"))


def env_flag(name, default=False):
    return os.environ.get(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


ENVIRONMENT = os.environ.get("ZOOLAND_ENV", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"
PORT = int(os.environ.get("ZOOLAND_PORT", "5000"))
PUBLIC_BASE_URL = os.environ.get("ZOOLAND_PUBLIC_BASE_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
public_url_parts = urlparse(PUBLIC_BASE_URL)
if public_url_parts.scheme not in {"http", "https"} or not public_url_parts.netloc:
    raise RuntimeError("ZOOLAND_PUBLIC_BASE_URL должен содержать полный адрес сайта.")
if IS_PRODUCTION and public_url_parts.scheme != "https":
    raise RuntimeError("В production ZOOLAND_PUBLIC_BASE_URL должен использовать HTTPS.")

secret_key = os.environ.get("ZOOLAND_SECRET_KEY", "")
if len(secret_key) < 32:
    if IS_PRODUCTION:
        raise RuntimeError("Для production задайте длинный ZOOLAND_SECRET_KEY вне репозитория.")
    # В разработке секрет остаётся непредсказуемым даже без .env, но сессии сбросятся после перезапуска.
    secret_key = secrets.token_urlsafe(48)

trusted_hosts_raw = os.environ.get("ZOOLAND_TRUSTED_HOSTS", "")
trusted_hosts = [value.strip() for value in trusted_hosts_raw.split(",") if value.strip()]
if IS_PRODUCTION and not trusted_hosts:
    trusted_hosts = [public_url_parts.netloc]

app = Flask(__name__)
app.config.update(
    SECRET_KEY=secret_key,
    UPLOAD_FOLDER=os.path.join(PROJECT_ROOT, "static", "uploads"),
    MAX_CONTENT_LENGTH=50 * 1024 * 1024,  # До 10 файлов по 5 МБ.
    MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
    MAX_FORM_PARTS=50,
    # __Host- запрещает браузеру принимать cookie с поддомена в production.
    SESSION_COOKIE_NAME="__Host-zooland_session" if IS_PRODUCTION else "zooland_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    SESSION_COOKIE_PATH="/",
    SESSION_REFRESH_EACH_REQUEST=True,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    TRUSTED_HOSTS=trusted_hosts or None,
)

trusted_proxy_hops = int(os.environ.get("ZOOLAND_TRUST_PROXY_HOPS", "0"))
if trusted_proxy_hops:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxy_hops, x_proto=trusted_proxy_hops, x_host=trusted_proxy_hops)

configured_db_path = os.environ.get("ZOOLAND_DB_PATH", "").strip()
DB_NAME = (configured_db_path if os.path.isabs(configured_db_path)
           else os.path.join(PROJECT_ROOT, configured_db_path or "zooland.db"))
MAX_PHOTOS = 10          # Не более 10 фото на объявление.
MAX_PHOTO_SIZE = 5 * 1024 * 1024  # Не более 5 МБ на файл.
CLAMAV_HOST = os.environ.get("ZOOLAND_CLAMAV_HOST", "").strip() or None
CLAMAV_PORT = int(os.environ.get("ZOOLAND_CLAMAV_PORT", "3310"))
# В production отсутствие сканера намеренно останавливает загрузку, а не
# превращает антивирус в необязательную галочку.
REQUIRE_ANTIVIRUS = env_flag("ZOOLAND_REQUIRE_ANTIVIRUS", IS_PRODUCTION)
FREE_DAYS = 7
SUPPORT_EMAIL = os.environ.get("ZOOLAND_SUPPORT_EMAIL", "support@zooland.ru")
DEAL_TYPE_LABELS = {"sale": "Продам", "buy": "Куплю", "mating": "Вязка", "friend": "Ищу друга/подругу"}
OUTCOME_STATUS_LABELS = {
    "open": "Активно",
    "reserved": "В резерве",
    "sold": "Продано",
    "rehomed": "Питомец нашёл дом",
    "completed": "Услуга оказана",
}
# Результат сделки не смешиваем с публикационным status: тот отвечает только
# за модерацию и срок жизни карточки.
OUTCOME_STATUS_OPTIONS = {
    "animal": (("open", "Активно"), ("reserved", "В резерве"), ("sold", "Продано"), ("rehomed", "Питомец нашёл дом")),
    "service": (("open", "Активно"), ("reserved", "В резерве"), ("completed", "Услуга оказана")),
    "food": (("open", "Активно"), ("reserved", "В резерве"), ("sold", "Продано")),
}
FINAL_OUTCOME_STATUSES = frozenset({"sold", "rehomed", "completed"})
PUBLIC_OUTCOME_SQL = "COALESCE(deal_status, 'open') NOT IN ('sold', 'rehomed', 'completed')"
LISTING_TABLES = {
    "animal": ("animals", "breed", FREE_DAYS),
    "service": ("services", "title", SERVICE_DAYS),
    "food": ("food", "title", FREE_DAYS),
}
LISTING_REVISION_FIELDS = {
    "animal": (
        "type", "breed", "age", "price", "city", "description", "contacts", "photo", "deal_type",
        "contact_methods", "sex", "birth_date", "vaccinated", "vet_passport", "pedigree", "sterilized", "delivery",
    ),
    # Контакты услуг и товаров — снимок на момент публикации. Иначе смена
    # номера или Telegram в профиле обходила бы повторную модерацию карточки.
    "service": ("type", "title", "pet_types", "city", "price", "description", "photo", "contacts", "telegram", "contact_methods"),
    "food": ("category", "title", "city", "price", "description", "photo", "contacts", "telegram", "contact_methods"),
}
TRACKED_METRICS = frozenset({"favorite_added", "phone_click", "telegram_click", "chat_click"})
REPORT_REASONS = ("Мошенничество", "Запрещённый товар или услуга", "Неверная информация", "Оскорбительный контент", "Дубликат", "Другое")
MODERATION_REASONS = ("Недостаточно информации", "Неподходящие или чужие фото", "Запрещённый товар или услуга", "Некорректная категория", "Нарушение правил площадки", "Другое")
JOB_APPLICATION_STATUSES = {
    "new": "Новая", "reviewing": "Рассматривается", "interview": "Назначено собеседование",
    "hired": "Принят", "rejected": "Отказ",
}
ADVERTISING_FORMATS = {
    "promoted-listing": {
        "title": "Продвижение объявления", "icon": "↗",
        "summary": "Поднимем объявление выше в каталоге и визуально выделим его среди обычных публикаций.",
        "features": ("Приоритетная позиция в выбранной категории", "Отметка «Продвигается»", "Статистика показов и переходов"),
    },
    "homepage-banner": {
        "title": "Баннер на главной", "icon": "▰",
        "summary": "Покажем предложение широкой аудитории ZooLand на главной странице сайта.",
        "features": ("Адаптация для компьютера и телефона", "Ссылка на сайт рекламодателя", "Ограничение по срокам показа"),
    },
    "category-banner": {
        "title": "Баннер в категории", "icon": "◎",
        "summary": "Разместим рекламу рядом с наиболее подходящими объявлениями и услугами.",
        "features": ("Выбор тематического раздела", "Более точное попадание в аудиторию", "Статистика по выбранной категории"),
    },
}
ADVERTISING_STATUSES = {
    "new": "Новая", "discussing": "На согласовании", "waiting_materials": "Ожидает материалы",
    "ready": "Готова к размещению", "active": "Активна", "completed": "Завершена", "rejected": "Отклонена",
}
ADVERTISING_CATEGORIES = {
    "all": "Весь сайт", "animals": "Животные", "services": "Услуги",
    "food": "Зоотовары и питание", "accessories": "Аксессуары",
}
ADVERTISING_METRICS = frozenset({"impression", "click"})
VACANCIES = {
    "listing-moderator": {
        "title": "Модератор объявлений", "department": "Безопасность площадки", "format": "Удалённо",
        "schedule": "Сменный график", "salary": "По итогам собеседования",
        "summary": "Проверять новые объявления и помогать сохранять каталог ZooLand безопасным и понятным.",
        "responsibilities": ("Проверять тексты, фотографии и категории объявлений", "Возвращать публикации на доработку с понятной причиной", "Обрабатывать жалобы пользователей"),
        "requirements": ("Грамотный русский язык", "Внимательность к деталям", "Спокойное отношение к повторяющимся задачам"),
    },
    "support-specialist": {
        "title": "Специалист поддержки", "department": "Забота о пользователях", "format": "Удалённо",
        "schedule": "Полная или частичная занятость", "salary": "По итогам собеседования",
        "summary": "Отвечать пользователям ZooLand и помогать им решать вопросы с объявлениями и аккаунтом.",
        "responsibilities": ("Отвечать на обращения в чате и по email", "Помогать с публикацией и настройками профиля", "Передавать сложные случаи разработчику или модератору"),
        "requirements": ("Доброжелательная письменная речь", "Умение объяснять простыми словами", "Ответственность и самостоятельность"),
    },
    "advertising-manager": {
        "title": "Менеджер по рекламе", "department": "Развитие", "format": "Удалённо",
        "schedule": "Гибкий график", "salary": "Оклад и бонус обсуждаются",
        "summary": "Находить полезных для аудитории партнёров и развивать рекламные размещения на ZooLand.",
        "responsibilities": ("Общаться с рекламодателями и партнёрами", "Готовить предложения и медиапланы", "Следить за сроками и результатами размещений"),
        "requirements": ("Опыт продаж или работы с партнёрами", "Грамотная деловая переписка", "Бережное отношение к аудитории площадки"),
    },
    "content-manager": {
        "title": "Контент-менеджер", "department": "Контент", "format": "Удалённо",
        "schedule": "Гибкий график", "salary": "По итогам собеседования",
        "summary": "Создавать полезные материалы о питомцах и поддерживать информацию на сайте актуальной.",
        "responsibilities": ("Готовить и редактировать статьи", "Подбирать темы и иллюстрации", "Обновлять справочные разделы сайта"),
        "requirements": ("Грамотность и хороший стиль", "Умение проверять факты и источники", "Интерес к теме животных"),
    },
}
COUNTRY_CODES = [("Россия", "+7"), ("Казахстан", "+7"), ("США и Канада", "+1"), ("Украина", "+380"), ("Беларусь", "+375"), ("Германия", "+49"), ("Франция", "+33"), ("Великобритания", "+44"), ("Испания", "+34"), ("Италия", "+39"), ("Турция", "+90"), ("ОАЭ", "+971"), ("Израиль", "+972"), ("Китай", "+86"), ("Япония", "+81"), ("Индия", "+91"), ("Южная Корея", "+82"), ("Австралия", "+61"), ("Бразилия", "+55"), ("Мексика", "+52"), ("ЮАР", "+27")]
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# ---------- Защита от DDoS-атак ----------
# Скользящее окно: не более RATE_LIMIT запросов с одного IP за RATE_WINDOW секунд.
# При превышении IP временно блокируется на BAN_SECONDS.
RATE_LIMIT = 40
RATE_WINDOW = 60
BAN_SECONDS = 300
BAN_THRESHOLD = 3  # Сколько раз подряд превысить лимит, чтобы получить бан.

_request_log = {}   # ip -> список timestamp запросов
_ban_count = {}     # ip -> сколько раз превышен лимит
_banned_until = {}  # ip -> timestamp окончания бана
_next_rate_cleanup = 0.0


def _client_ip():
    """ProxyFix подставит проверенный IP только при явно доверенном прокси."""
    return request.remote_addr or "unknown"


def _cleanup_rate_state(now):
    """Удаляет устаревшие записи, чтобы память не росла бесконечно."""
    global _next_rate_cleanup
    # Не сканируем все IP на каждом запросе: это было бы легко превратить в
    # лишнюю нагрузку. Текущий IP очищается ниже при каждом его запросе.
    if now < _next_rate_cleanup:
        return
    _next_rate_cleanup = now + 30
    for ip in list(_request_log):
        _request_log[ip] = [t for t in _request_log[ip] if now - t < RATE_WINDOW]
        if not _request_log[ip]:
            del _request_log[ip]
    for ip in list(_banned_until):
        if now >= _banned_until[ip]:
            del _banned_until[ip]
            _ban_count.pop(ip, None)


@app.before_request
def rate_limit():
    """Ограничивает число запросов с одного IP и банит нарушителей."""
    now = time.time()
    _cleanup_rate_state(now)
    ip = _client_ip()
    # Статические файлы не считаем — браузер запрашивает их пачками.
    if request.path.startswith("/static/"):
        return None
    # Авторизованному модератору лимит не мешает проверять объявления и
    # обращения. Роль сверяется с БД, а не с параметрами запроса или формой.
    if current_user_is_moderator():
        return None
    # IP в бане — отклоняем все запросы до окончания бана.
    if ip in _banned_until:
        if now < _banned_until[ip]:
            return "Слишком много запросов. Попробуйте позже.", 429
        del _banned_until[ip]
        _ban_count.pop(ip, None)
    # Скользящее окно запросов.
    timestamps = [timestamp for timestamp in _request_log.get(ip, []) if now - timestamp < RATE_WINDOW]
    _request_log[ip] = timestamps
    timestamps.append(now)
    if len(timestamps) > RATE_LIMIT:
        _ban_count[ip] = _ban_count.get(ip, 0) + 1
        if _ban_count[ip] >= BAN_THRESHOLD:
            _banned_until[ip] = now + BAN_SECONDS
            _ban_count.pop(ip, None)
            return "Слишком много запросов. Попробуйте позже.", 429
        return "Слишком много запросов. Попробуйте позже.", 429
    return None


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def request_origin_is_trusted(value):
    if not value:
        return False
    parsed = urlparse(value)
    expected_scheme = "https" if IS_PRODUCTION else request.scheme
    return bool(parsed.scheme == expected_scheme and parsed.netloc and
                hmac.compare_digest(parsed.netloc.casefold(), request.host.casefold()))


@app.before_request
def csrf_protect():
    """Токен защищает формы, а Origin/Referer оставляет безопасный fallback без JavaScript."""
    if request.path.startswith("/static/"):
        return None
    csrf_token()
    if request.method not in UNSAFE_METHODS:
        return None
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if supplied and hmac.compare_digest(supplied, session["csrf_token"]):
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if request_origin_is_trusted(source):
        return None
    abort(400, description="Не удалось подтвердить безопасность отправки формы. Обновите страницу и повторите попытку.")


@app.after_request
def apply_security_headers(response):
    """Общие браузерные ограничения и доступный JavaScript CSRF-cookie."""
    if not request.path.startswith("/static/"):
        response.set_cookie(
            "zooland_csrf", csrf_token(), max_age=int(app.permanent_session_lifetime.total_seconds()),
            secure=app.config["SESSION_COOKIE_SECURE"], httponly=False, samesite="Lax", path="/",
        )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; "
        "img-src 'self' data:; connect-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com",
    )
    if request.path.startswith(("/password", "/verify-email", "/settings", "/admin")):
        response.headers.setdefault("Cache-Control", "no-store")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.errorhandler(413)
def too_large(_error):
    return "Файл слишком большой. Максимальный размер одного файла — 5 МБ, всего до 10 файлов.", 413

with open(os.path.join(PROJECT_ROOT, "static", "russian-cities.json"), encoding="utf-8") as cities_file:
    cities_data = json.load(cities_file)
RUSSIAN_CITIES = sorted({item["name"] for item in cities_data})
CITY_COORDS = {item["name"]: (float(item["coords"]["lat"]), float(item["coords"]["lon"])) for item in cities_data}
SIMILAR_RADIUS_KM = 50
SIMILAR_LIMIT = 6

# Раскладки используются именно для поискового запроса: «rjn» -> «кот».
EN_KEYS = "qwertyuiop[]asdfghjkl;'zxcvbnm,./`"
RU_KEYS = "йцукенгшщзхъфывапролджэячсмитьбю.ё"
EN_TO_RU = str.maketrans(EN_KEYS + EN_KEYS.upper(), RU_KEYS + RU_KEYS.upper())
RU_TO_EN = str.maketrans(RU_KEYS + RU_KEYS.upper(), EN_KEYS + EN_KEYS.upper())
RU_TO_LATIN = str.maketrans({
    "а":"a", "б":"b", "в":"v", "г":"g", "д":"d", "е":"e", "ё":"yo", "ж":"zh", "з":"z", "и":"i", "й":"y", "к":"k", "л":"l", "м":"m", "н":"n", "о":"o", "п":"p", "р":"r", "с":"s", "т":"t", "у":"u", "ф":"f", "х":"h", "ц":"ts", "ч":"ch", "ш":"sh", "щ":"sch", "ъ":"", "ы":"y", "ь":"", "э":"e", "ю":"yu", "я":"ya"
})
TYPE_ALIASES = {
    "собака": ("dog", "puppy", "пёс", "пес", "щенок", "canine"),
    "кот": ("cat", "kitten", "кошка", "котёнок", "котенок", "feline"),
    "попугай": ("parrot",), "кролик": ("rabbit", "bunny"),
    "грызун": ("rodent", "hamster", "хомяк"), "черепаха": ("turtle",),
    "змея": ("snake",), "ящерица": ("lizard",), "хорёк": ("ferret", "хорек"),
    "аквариумные рыбы": ("fish", "рыба", "рыбки"),
}
BREED_ALIASES = {
    "Бигль": ("beagle",), "Лабрадор-ретривер": ("labrador",), "Немецкая овчарка": ("german shepherd",),
    "Золотистый ретривер": ("golden retriever",), "Йоркширский терьер": ("york", "yorkshire terrier"),
    "Французский бульдог": ("french bulldog",), "Чихуахуа": ("chihuahua",), "Хаски": ("husky",),
    "Шпиц": ("spitz", "pomeranian"), "Такса": ("dachshund",), "Пудель": ("poodle",),
    "Мейн-кун": ("maine coon",), "Бенгальская": ("bengal",), "Сиамская": ("siamese",),
    "Британская короткошёрстная": ("british shorthair",), "Сибирская": ("siberian",),
}


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_NAME)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection:
        connection.close()


def init_db():
    connection = sqlite3.connect(DB_NAME)
    cursor = connection.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS animals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, breed TEXT, age INTEGER,
        price INTEGER, city TEXT, description TEXT, contacts TEXT, photo TEXT
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, title TEXT NOT NULL,
        pet_types TEXT, city TEXT, price INTEGER DEFAULT 0, description TEXT,
        photo TEXT, contacts TEXT, telegram TEXT, user_id INTEGER, status TEXT DEFAULT 'active',
        created_at TEXT, expires_at TEXT, months_published INTEGER DEFAULT 0,
        views_total INTEGER DEFAULT 0, views_data TEXT
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS food (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, title TEXT NOT NULL,
        city TEXT, price INTEGER DEFAULT 0, description TEXT, photo TEXT,
        contacts TEXT, telegram TEXT, user_id INTEGER, status TEXT DEFAULT 'active', created_at TEXT,
        expires_at TEXT, views_total INTEGER DEFAULT 0, views_data TEXT
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, listing_type TEXT NOT NULL,
        listing_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL,
        body TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER DEFAULT 0, folder TEXT DEFAULT 'inbox'
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_listing ON messages(listing_type, listing_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_receiver ON messages(receiver_id, read)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS dialog_blocks (
        user_id INTEGER NOT NULL, listing_type TEXT NOT NULL, listing_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY (user_id, listing_type, listing_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS favorites (
        user_id INTEGER NOT NULL, listing_type TEXT NOT NULL, listing_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY (user_id, listing_type, listing_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens(token_hash)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS email_verification_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, code_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used_at TEXT, created_at TEXT NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS email_change_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, new_email TEXT NOT NULL,
        code_hash TEXT NOT NULL, expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        used_at TEXT, created_at TEXT NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_email_change_codes_user ON email_change_codes(user_id, expires_at)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS admin_login_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, code_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used_at TEXT, created_at TEXT NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_admin_login_codes_user ON admin_login_codes(user_id, expires_at)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL, listing_type TEXT NOT NULL,
        listing_id INTEGER NOT NULL, reason TEXT NOT NULL, comment TEXT, status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL, resolved_at TEXT, UNIQUE(reporter_id, listing_type, listing_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reviewer_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL,
        listing_type TEXT NOT NULL CHECK (listing_type IN ('animal', 'service', 'food')),
        listing_id INTEGER NOT NULL,
        rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
        comment TEXT NOT NULL DEFAULT '' CHECK (length(comment) <= 1000),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (reviewer_id != owner_id),
        UNIQUE(reviewer_id, owner_id, listing_type, listing_id)
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reviews_owner ON reviews(owner_id, updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reviews_reviewer_listing ON reviews(reviewer_id, listing_type, listing_id)")
    # Правки уже опубликованных объявлений храним отдельно: посетители не
    # теряют одобренную карточку, а модератор видит именно новую версию.
    cursor.execute("""CREATE TABLE IF NOT EXISTS listing_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_type TEXT NOT NULL CHECK (listing_type IN ('animal', 'service', 'food')),
        listing_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'rejected')),
        moderation_reason TEXT,
        submitted_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(listing_type, listing_id)
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_listing_revisions_queue ON listing_revisions(status, submitted_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_listing_revisions_owner ON listing_revisions(owner_id, updated_at)")
    # Только дневные агрегаты действий, без IP, телефонов, текста сообщений
    # и данных посетителей. Это даёт владельцу полезную статистику без
    # слежения за людьми.
    cursor.execute("""CREATE TABLE IF NOT EXISTS listing_daily_metrics (
        listing_type TEXT NOT NULL CHECK (listing_type IN ('animal', 'service', 'food')),
        listing_id INTEGER NOT NULL,
        metric_day TEXT NOT NULL,
        metric TEXT NOT NULL CHECK (metric IN ('favorite_added', 'phone_click', 'telegram_click', 'chat_click')),
        value INTEGER NOT NULL DEFAULT 0 CHECK (value >= 0),
        PRIMARY KEY (listing_type, listing_id, metric_day, metric)
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_listing_daily_metrics_day ON listing_daily_metrics(metric_day)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS saved_searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL,
        filters_json TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS search_notifications (
        saved_search_id INTEGER NOT NULL, listing_type TEXT NOT NULL, listing_id INTEGER NOT NULL,
        sent_at TEXT NOT NULL, PRIMARY KEY(saved_search_id, listing_type, listing_id)
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS rate_limit_events (
        bucket TEXT NOT NULL, subject_hash TEXT NOT NULL, created_at REAL NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_events_lookup ON rate_limit_events(bucket, subject_hash, created_at)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS job_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, vacancy_slug TEXT NOT NULL,
        name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT, telegram TEXT,
        experience TEXT NOT NULL, cover_letter TEXT NOT NULL, resume_url TEXT,
        status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'reviewing', 'interview', 'hired', 'rejected')),
        admin_note TEXT NOT NULL DEFAULT '', consent_at TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_applications_status ON job_applications(status, created_at DESC)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS advertising_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, format TEXT NOT NULL,
        contact_name TEXT NOT NULL, company TEXT NOT NULL, inn TEXT NOT NULL,
        email TEXT NOT NULL, phone TEXT, telegram TEXT, target_url TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'all', cities TEXT, preferred_dates TEXT,
        budget TEXT, comment TEXT NOT NULL DEFAULT '',
        headline TEXT NOT NULL DEFAULT '', ad_text TEXT NOT NULL DEFAULT '',
        button_label TEXT NOT NULL DEFAULT 'Подробнее', start_date TEXT, end_date TEXT,
        status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'discussing', 'waiting_materials', 'ready', 'active', 'completed', 'rejected')),
        admin_note TEXT NOT NULL DEFAULT '', consent_at TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_advertising_requests_status ON advertising_requests(status, created_at DESC)")
    advertising_columns = {row[1] for row in cursor.execute("PRAGMA table_info(advertising_requests)")}
    for column, definition in {
        "headline": "TEXT NOT NULL DEFAULT ''",
        "ad_text": "TEXT NOT NULL DEFAULT ''",
        "button_label": "TEXT NOT NULL DEFAULT 'Подробнее'",
        "start_date": "TEXT",
        "end_date": "TEXT",
    }.items():
        if column not in advertising_columns:
            cursor.execute(f"ALTER TABLE advertising_requests ADD COLUMN {column} {definition}")
    # Старые активные заявки сразу получают безопасный текстовый креатив.
    # Контактные данные и внутренний комментарий в публичный блок не попадают.
    cursor.execute("""UPDATE advertising_requests
                         SET headline=company
                       WHERE status='active' AND (headline IS NULL OR TRIM(headline)='')""")
    cursor.execute("""UPDATE advertising_requests
                         SET ad_text='Узнайте подробнее о предложении рекламодателя.'
                       WHERE status='active' AND (ad_text IS NULL OR TRIM(ad_text)='')""")
    cursor.execute("""UPDATE advertising_requests
                         SET button_label='Подробнее'
                       WHERE button_label IS NULL OR TRIM(button_label)=''""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_advertising_requests_public
                      ON advertising_requests(status, format, category, start_date, end_date)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS advertising_daily_metrics (
        request_id INTEGER NOT NULL,
        metric_day TEXT NOT NULL,
        metric TEXT NOT NULL CHECK (metric IN ('impression', 'click')),
        value INTEGER NOT NULL DEFAULT 0 CHECK (value >= 0),
        PRIMARY KEY (request_id, metric_day, metric)
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_advertising_metrics_request ON advertising_daily_metrics(request_id, metric_day)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS dialog_states (
        user_id INTEGER NOT NULL, listing_type TEXT NOT NULL, listing_id INTEGER NOT NULL, other_id INTEGER NOT NULL,
        folder TEXT NOT NULL DEFAULT 'inbox', updated_at TEXT NOT NULL,
        PRIMARY KEY(user_id, listing_type, listing_id, other_id)
    )""")
    block_columns = {row[1] for row in cursor.execute("PRAGMA table_info(dialog_blocks)")}
    if "other_id" not in block_columns:
        cursor.execute("""CREATE TABLE IF NOT EXISTS dialog_blocks_v2 (
            user_id INTEGER NOT NULL, listing_type TEXT NOT NULL, listing_id INTEGER NOT NULL, other_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(user_id, listing_type, listing_id, other_id)
        )""")
        cursor.execute("""INSERT OR IGNORE INTO dialog_blocks_v2 (user_id, listing_type, listing_id, other_id, created_at)
                          SELECT user_id, listing_type, listing_id, 0, created_at FROM dialog_blocks""")
        cursor.execute("DROP TABLE dialog_blocks")
        cursor.execute("ALTER TABLE dialog_blocks_v2 RENAME TO dialog_blocks")
    # Миграция: добавляем колонку folder, если её нет.
    message_columns = {row[1] for row in cursor.execute("PRAGMA table_info(messages)")}
    if "folder" not in message_columns:
        cursor.execute("ALTER TABLE messages ADD COLUMN folder TEXT DEFAULT 'inbox'")
    cursor.execute("UPDATE messages SET folder='inbox' WHERE folder IS NULL")
    user_columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
    for column, definition in {
        "phone": "TEXT", "email_verified": "INTEGER DEFAULT 0",
        "city": "TEXT", "gender": "TEXT", "birth_date": "TEXT", "about": "TEXT", "avatar": "TEXT", "telegram": "TEXT",
        "is_admin": "INTEGER NOT NULL DEFAULT 0", "session_version": "INTEGER NOT NULL DEFAULT 1",
        "pending_email": "TEXT",
    }.items():
        if column not in user_columns:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    # Уже существующие аккаунты не блокируем после обновления схемы.
    cursor.execute("UPDATE users SET email_verified=1 WHERE email_verified IS NULL")
    cursor.execute("UPDATE users SET session_version=1 WHERE session_version IS NULL")
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(animals)")}
    migrations = {
        "user_id": "INTEGER", "guest_token": "TEXT", "status": "TEXT DEFAULT 'active'",
        "created_at": "TEXT", "expires_at": "TEXT", "archived_at": "TEXT",
        "views_total": "INTEGER DEFAULT 0", "views_data": "TEXT", "deal_type": "TEXT DEFAULT 'sale'",
        "contact_methods": "TEXT DEFAULT 'phone,chat,telegram'",
        "sex": "TEXT", "birth_date": "TEXT", "vaccinated": "INTEGER DEFAULT 0",
        "vet_passport": "INTEGER DEFAULT 0", "pedigree": "INTEGER DEFAULT 0",
        "sterilized": "INTEGER DEFAULT 0", "delivery": "INTEGER DEFAULT 0", "moderation_reason": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            cursor.execute(f"ALTER TABLE animals ADD COLUMN {column} {definition}")
    for table in ("services", "food"):
        columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
        contacts_added = False
        telegram_added = False
        if "contacts" not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN contacts TEXT")
            contacts_added = True
        if "telegram" not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN telegram TEXT")
            telegram_added = True
        if "contact_methods" not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN contact_methods TEXT DEFAULT 'phone,chat,telegram'")
        if "moderation_reason" not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN moderation_reason TEXT")
        # Для уже опубликованных карточек фиксируем текущие контакты ровно
        # один раз — именно в момент миграции. Позже изменение профиля не
        # должно попадать в уже опубликованную карточку после перезапуска.
        if contacts_added:
            cursor.execute(
                f"""UPDATE {table}
                       SET contacts=(SELECT phone FROM users WHERE users.id={table}.user_id)
                     WHERE user_id IS NOT NULL AND (contacts IS NULL OR TRIM(contacts)='')"""
            )
        if telegram_added:
            cursor.execute(
                f"""UPDATE {table}
                       SET telegram=(SELECT telegram FROM users WHERE users.id={table}.user_id)
                     WHERE user_id IS NOT NULL AND (telegram IS NULL OR TRIM(telegram)='')"""
            )
    # Отдельный исход сделки есть у каждого типа карточки. Старые записи по
    # умолчанию остаются обычными активными предложениями.
    for table in ("animals", "services", "food"):
        columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
        if "deal_status" not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN deal_status TEXT DEFAULT 'open'")
        if "outcome_at" not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN outcome_at TEXT")
        cursor.execute(f"UPDATE {table} SET deal_status='open' WHERE deal_status IS NULL OR deal_status=''")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_favorites_listing ON favorites(listing_type, listing_id)")
    cursor.execute("""INSERT OR IGNORE INTO dialog_states (user_id, listing_type, listing_id, other_id, folder, updated_at)
                      SELECT sender_id, listing_type, listing_id, receiver_id, COALESCE(folder, 'inbox'), ? FROM messages""", (utcnow(),))
    cursor.execute("""INSERT OR IGNORE INTO dialog_states (user_id, listing_type, listing_id, other_id, folder, updated_at)
                      SELECT receiver_id, listing_type, listing_id, sender_id, COALESCE(folder, 'inbox'), ? FROM messages""", (utcnow(),))
    # У существующих объявлений по умолчанию тип сделки — продажа.
    cursor.execute("UPDATE animals SET deal_type='sale' WHERE deal_type IS NULL")
    now = utcnow()
    cursor.execute("UPDATE animals SET status='active' WHERE status IS NULL")
    cursor.execute("UPDATE animals SET created_at=? WHERE created_at IS NULL", (now,))
    cursor.execute("UPDATE animals SET expires_at=? WHERE expires_at IS NULL", (iso_after_days(FREE_DAYS),))
    cursor.execute("UPDATE services SET created_at=? WHERE created_at IS NULL", (now,))
    cursor.execute("UPDATE food SET created_at=? WHERE created_at IS NULL", (now,))
    connection.commit()
    connection.close()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


@app.template_filter("publication_date")
def publication_date(value):
    """Показывает дату публикации в привычном для посетителя формате."""
    if not value:
        return "дата не указана"
    try:
        published = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        # ZooLand ориентирован на русскоязычную аудиторию; дата выводится по Москве.
        return published.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(value)[:10]


def advertising_today():
    """Календарный день показа для основной аудитории ZooLand (Москва)."""
    return datetime.now(timezone(timedelta(hours=3))).date().isoformat()


def valid_advertising_target(value):
    """Возвращает безопасную относительную либо http(s)-ссылку рекламодателя."""
    target = (value or "").strip()
    if not target or len(target) > 500 or "\\" in target or re.search(r"[\x00-\x1f\x7f]", target):
        return None
    if target.startswith("/") and not target.startswith("//"):
        try:
            relative_parts = urlparse(target)
        except ValueError:
            return None
        return target if not relative_parts.scheme and not relative_parts.netloc else None
    try:
        parts = urlparse(target)
        # Обращение к hostname/port дополнительно отбрасывает повреждённые URL.
        hostname, port = parts.hostname, parts.port
    except (TypeError, ValueError):
        return None
    if parts.scheme.casefold() not in {"http", "https"} or not hostname:
        return None
    if parts.username is not None or parts.password is not None or port is not None and not 1 <= port <= 65535:
        return None
    return target


def advertising_date_is_valid(value):
    if not value:
        return True
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return parsed.strftime("%Y-%m-%d") == value


def record_advertising_metric(request_id, metric):
    """Сохраняет только дневной агрегат — без IP, cookie и данных посетителя."""
    if metric not in ADVERTISING_METRICS:
        return
    if metric == "impression":
        recorded = getattr(g, "advertising_impressions", set())
        if request_id in recorded:
            return
        recorded.add(request_id)
        g.advertising_impressions = recorded
    db().execute(
        """INSERT INTO advertising_daily_metrics (request_id, metric_day, metric, value)
             VALUES (?, ?, ?, 1)
             ON CONFLICT(request_id, metric_day, metric)
             DO UPDATE SET value=value+1""",
        (request_id, advertising_today(), metric),
    )
    db().commit()


def active_advertisements(ad_format, category="all", city="", limit=1,
                          record_impressions=True, include_all_categories=False):
    """Возвращает текущие размещения для формата и раздела страницы."""
    if ad_format not in ADVERTISING_FORMATS or category not in ADVERTISING_CATEGORIES:
        return []
    today = advertising_today()
    if category == "all" and include_all_categories:
        category_sql, category_params = "1=1", []
    elif category == "all":
        category_sql, category_params = "category='all'", []
    else:
        category_sql, category_params = "category IN ('all', ?)", [category]
    rows = db().execute(
        f"""SELECT id, format, company, target_url, category, cities,
                    headline, ad_text, button_label, start_date, end_date
               FROM advertising_requests
             WHERE status='active' AND format=? AND {category_sql}
               AND (start_date IS NULL OR start_date='' OR start_date<=?)
               AND (end_date IS NULL OR end_date='' OR end_date>=?)
             ORDER BY updated_at DESC, id DESC LIMIT 100""",
        (ad_format, *category_params, today, today),
    ).fetchall()
    normalized_city = normalize_search(city)
    eligible = []
    for row in rows:
        advertisement = dict(row)
        if not valid_advertising_target(advertisement.get("target_url")):
            continue
        targeted_cities = {
            normalize_search(item) for item in re.split(r"[,;\n]", advertisement.get("cities") or "")
            if normalize_search(item)
        }
        if targeted_cities and normalized_city not in targeted_cities:
            continue
        advertisement["headline"] = (advertisement.get("headline") or advertisement["company"]).strip()
        advertisement["ad_text"] = (
            advertisement.get("ad_text") or ADVERTISING_FORMATS[advertisement["format"]]["summary"]
        ).strip()
        advertisement["button_label"] = (advertisement.get("button_label") or "Подробнее").strip()
        eligible.append(advertisement)
    max_results = max(1, min(int(limit), 50))
    if len(eligible) > max_results:
        rotation_key = f"{today}|{request.path}|{ad_format}|{category}"
        start = int.from_bytes(sha256(rotation_key.encode("utf-8")).digest()[:4], "big") % len(eligible)
        eligible = eligible[start:] + eligible[:start]
    advertisements = eligible[:max_results]
    for advertisement in advertisements:
        if record_impressions:
            record_advertising_metric(advertisement["id"], "impression")
    return advertisements


def promoted_listing_reference(advertisement):
    """Разбирает ссылку вида /animal/1, /service/2 или /food/3."""
    target = valid_advertising_target(advertisement.get("target_url"))
    if not target:
        return None
    try:
        target_parts = urlparse(target)
        public_parts = urlparse(PUBLIC_BASE_URL)
    except ValueError:
        return None
    if target_parts.scheme and (
        target_parts.scheme.casefold() != public_parts.scheme.casefold()
        or target_parts.netloc.casefold() != public_parts.netloc.casefold()
    ):
        return None
    path = target_parts.path
    match = re.fullmatch(r"/(animal|service|food)/(\d+)/?", path)
    return (match.group(1), int(match.group(2))) if match else None


def promoted_target_is_public(advertisement):
    reference = promoted_listing_reference(advertisement)
    if not reference:
        return False
    listing_type, listing_id = reference
    table = LISTING_TABLES[listing_type][0]
    row = db().execute(
        f"SELECT * FROM {table} WHERE id=? AND status='active' AND {PUBLIC_OUTCOME_SQL}",
        (listing_id,),
    ).fetchone()
    if not row:
        return False
    category = advertisement.get("category") or "all"
    if listing_type == "animal":
        return category in {"all", "animals"}
    if listing_type == "service":
        return category in {"all", "services"}
    product_category = "accessories" if row["category"] in ACCESSORY_TYPES else "food"
    return category in {"all", product_category}


def apply_promoted_listings(items, category, fixed_listing_type=None, city=""):
    """Поднимает подходящие карточки и помечает их рекламными, не создавая дублей."""
    item_keys = {
        (fixed_listing_type or item.get("kind"), int(item["id"])) for item in items
    }
    advertisements = active_advertisements(
        "promoted-listing", category, city=city, limit=50, record_impressions=False,
        include_all_categories=category == "all",
    )
    promotions = {}
    for advertisement in advertisements:
        reference = promoted_listing_reference(advertisement)
        if reference in item_keys:
            promotions.setdefault(reference, advertisement)
        if len(promotions) >= 5:
            break
    promoted, ordinary = [], []
    for item in items:
        listing_type = fixed_listing_type or item.get("kind")
        advertisement = promotions.get((listing_type, int(item["id"])))
        if advertisement:
            item["advertising_id"] = advertisement["id"]
            record_advertising_metric(advertisement["id"], "impression")
            promoted.append(item)
        else:
            ordinary.append(item)
    return promoted + ordinary


def advertising_metrics(request_id):
    rows = db().execute(
        """SELECT metric, COALESCE(SUM(value), 0) AS total
             FROM advertising_daily_metrics WHERE request_id=? GROUP BY metric""",
        (request_id,),
    ).fetchall()
    totals = {row["metric"]: int(row["total"] or 0) for row in rows}
    return {"impressions": totals.get("impression", 0), "clicks": totals.get("click", 0)}


ACTION_RATE_LIMITS = {
    "register": (4, 60 * 60),
    "login": (8, 15 * 60),
    "verify_resend": (3, 15 * 60),
    "password_reset": (3, 15 * 60),
    "support": (5, 60 * 60),
    "career": (5, 60 * 60),
    "advertising": (5, 60 * 60),
    "message": (20, 60),
    "review": (10, 60 * 60),
    "publish": (8, 60 * 60),
    "upload": (20, 60 * 60),
    "metric": (30, 60),
    "admin_2fa": (5, 15 * 60),
}


def allow_sensitive_action(action, subject=""):
    """SQLite-backed лимит на критические действия, переживающий перезапуск процесса."""
    limit, window = ACTION_RATE_LIMITS[action]
    now = time.time()
    keys = [(f"{action}:ip", _client_ip())]
    if subject:
        keys.append((f"{action}:subject", str(subject).casefold()))
    connection = db()
    # Держим только окно самого длинного лимита плюс небольшой запас.
    connection.execute("DELETE FROM rate_limit_events WHERE created_at<?", (now - 2 * 60 * 60,))
    hashed_keys = [(bucket, sha256(value.encode("utf-8")).hexdigest()) for bucket, value in keys]
    for bucket, subject_hash in hashed_keys:
        count = connection.execute("SELECT COUNT(*) FROM rate_limit_events WHERE bucket=? AND subject_hash=? AND created_at>=?",
                                   (bucket, subject_hash, now - window)).fetchone()[0]
        if count >= limit:
            connection.commit()
            return False
    connection.executemany("INSERT INTO rate_limit_events (bucket, subject_hash, created_at) VALUES (?, ?, ?)",
                           [(bucket, subject_hash, now) for bucket, subject_hash in hashed_keys])
    connection.commit()
    return True


def require_sensitive_action(action, subject, message, redirect_endpoint, **values):
    if allow_sensitive_action(action, subject):
        return None
    flash(message, "error")
    return redirect(url_for(redirect_endpoint, **values))


def iso_after_days(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def password_reset_expires_at():
    return (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()


def email_code_expires_at():
    return (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()


def short_code_hash(code):
    """Хеширует короткие одноразовые коды с ключом приложения, а не простым SHA-256."""
    return hmac.new(app.secret_key.encode("utf-8"), code.encode("utf-8"), sha256).hexdigest()


def send_email(recipient, subject, body, reply_to=None):
    """Отправляет письмо через единый ящик поддержки, заданный в окружении."""
    host = os.environ.get("ZOOLAND_SMTP_HOST")
    sender = os.environ.get("ZOOLAND_SMTP_FROM", SUPPORT_EMAIL)
    if not host or not sender:
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("ZOOLAND_SMTP_PORT", "587")), timeout=15) as smtp:
            if os.environ.get("ZOOLAND_SMTP_TLS", "1") != "1":
                raise smtplib.SMTPException("ZooLand requires verified STARTTLS for SMTP")
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            username = os.environ.get("ZOOLAND_SMTP_USERNAME")
            if username:
                smtp.login(username, os.environ.get("ZOOLAND_SMTP_PASSWORD", ""))
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        return False


def public_url(endpoint, **values):
    """Строит ссылки в письмах только на заранее настроенный домен, без Host header."""
    relative_path = url_for(endpoint, _external=False, **values)
    return urljoin(PUBLIC_BASE_URL + "/", relative_path.lstrip("/"))


def send_password_reset_email(recipient, reset_url):
    return send_email(
        recipient,
        "Восстановление пароля ZooLand",
        f"Для смены пароля ZooLand откройте ссылку:\n\n{reset_url}\n\nСсылка действует 30 минут и используется один раз. Если это были не вы, просто проигнорируйте письмо.",
    )


def create_password_reset_link(user_id):
    """Создаёт новую одноразовую ссылку; все прежние ссылки этого пользователя отменяются."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256(raw_token.encode()).hexdigest()
    connection = db()
    connection.execute("DELETE FROM password_reset_tokens WHERE user_id=? OR expires_at < ?", (user_id, utcnow()))
    connection.execute("INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, token_hash, password_reset_expires_at(), utcnow()))
    connection.commit()
    return public_url("reset_password", token=raw_token), token_hash


def create_email_verification_code(user_id, email):
    code = f"{secrets.randbelow(1_000_000):06d}"
    connection = db()
    connection.execute("DELETE FROM email_verification_codes WHERE user_id=?", (user_id,))
    connection.execute("INSERT INTO email_verification_codes (user_id, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, short_code_hash(code), email_code_expires_at(), utcnow()))
    connection.commit()
    sent = send_email(email, "Код подтверждения ZooLand", f"Ваш код подтверждения email: {code}\n\nКод действует 15 минут. Никому его не сообщайте.")
    if not sent:
        connection.execute("DELETE FROM email_verification_codes WHERE user_id=?", (user_id,))
        connection.commit()
    return sent


def create_email_change_code(user_id, new_email):
    code = f"{secrets.randbelow(1_000_000):06d}"
    connection = db()
    connection.execute("DELETE FROM email_change_codes WHERE user_id=?", (user_id,))
    connection.execute("INSERT INTO email_change_codes (user_id, new_email, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                       (user_id, new_email, short_code_hash(code), email_code_expires_at(), utcnow()))
    connection.commit()
    if send_email(new_email, "ZooLand: подтверждение нового email", f"Ваш код для подтверждения нового email: {code}\n\nКод действует 15 минут. Никому его не сообщайте."):
        return True
    connection.execute("DELETE FROM email_change_codes WHERE user_id=?", (user_id,))
    connection.commit()
    return False


def create_admin_login_code(user_id, email):
    code = f"{secrets.randbelow(1_000_000):06d}"
    connection = db()
    connection.execute("DELETE FROM admin_login_codes WHERE user_id=?", (user_id,))
    connection.execute("INSERT INTO admin_login_codes (user_id, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, short_code_hash(code), email_code_expires_at(), utcnow()))
    connection.commit()
    if send_email(email, "ZooLand: код входа администратора", f"Код для входа в аккаунт администратора: {code}\n\nКод действует 15 минут. Никому его не сообщайте."):
        return True
    connection.execute("DELETE FROM admin_login_codes WHERE user_id=?", (user_id,))
    connection.commit()
    return False


def password_error(password, *, name="", email=""):
    if len(password) < 10:
        return "Пароль должен содержать не менее 10 символов."
    normalized = password.casefold()
    if name and len(name) >= 3 and name.casefold() in normalized:
        return "Пароль не должен содержать ваше имя."
    if email and email.split("@", 1)[0].casefold() in normalized:
        return "Пароль не должен содержать email."
    return None


def save_photos(files):
    """Безопасно нормализует изображения и удаляет частичные результаты при ошибке."""
    uploaded = [file for file in files if file and file.filename]
    if len(uploaded) > MAX_PHOTOS:
        raise ValueError(f"Можно загрузить не более {MAX_PHOTOS} фото.")
    if uploaded:
        user = current_user()
        subject = str(user["id"]) if user else ""
        if not allow_sensitive_action("upload", subject):
            raise ValueError("Слишком много загрузок. Попробуйте снова немного позже.")
    saved = []
    try:
        for file in uploaded:
            saved.append(sanitize_image_upload(file))
        return saved
    except ImageSafetyError as error:
        delete_photos(",".join(saved))
        raise ValueError(str(error)) from error
    except Exception:
        delete_photos(",".join(saved))
        raise


def sanitize_image_upload(file):
    return sanitize_upload(
        file,
        app.config["UPLOAD_FOLDER"],
        max_input_bytes=MAX_PHOTO_SIZE,
        clamav_host=CLAMAV_HOST,
        clamav_port=CLAMAV_PORT,
        require_antivirus=REQUIRE_ANTIVIRUS,
    )


def delete_photos(photo_field):
    """Удаляет файлы фото, перечисленные через запятую в photo_field."""
    if not photo_field:
        return
    for name in str(photo_field).split(","):
        name = name.strip()
        # Имя могло появиться ещё до новой нормализации; никогда не позволяем
        # полю БД выйти из каталога загрузок при удалении файла.
        if not name or name != os.path.basename(name) or name.startswith("."):
            continue
        try:
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], name))
        except OSError:
            pass


def delete_user_and_associated_data(user_id):
    """Безвозвратно удаляет аккаунт и все данные, связанные с ним.

    В SQLite здесь нет внешних ключей с каскадным удалением, поэтому очищаем
    зависимости явно. Файлы удаляются только после успешного коммита БД: при
    ошибке в запросах у пользователя не пропадут фотографии без записи.
    """
    connection = db()
    user = connection.execute(
        "SELECT id, email, pending_email, avatar FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not user:
        return False

    existing_tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    listing_tables = (
        ("animal", "animals"),
        ("service", "services"),
        ("food", "food"),
    )
    owned_listing_ids = {}
    files_to_remove = {user["avatar"]} if user["avatar"] else set()

    for listing_type, table in listing_tables:
        rows = connection.execute(
            f"SELECT id, photo FROM {table} WHERE user_id=?", (user_id,)
        ).fetchall()
        owned_listing_ids[listing_type] = [row["id"] for row in rows]
        files_to_remove.update(row["photo"] for row in rows if row["photo"])
    if "listing_revisions" in existing_tables:
        for revision in connection.execute("SELECT payload_json FROM listing_revisions WHERE owner_id=?", (user_id,)).fetchall():
            files_to_remove.add(revision_payload(revision).get("photo", ""))

    def delete_listing_references(table):
        """Удаляет ссылки на объявления пользователя из указанной таблицы."""
        for listing_type, listing_ids in owned_listing_ids.items():
            if not listing_ids:
                continue
            placeholders = ", ".join("?" for _ in listing_ids)
            connection.execute(
                f"DELETE FROM {table} WHERE listing_type=? AND listing_id IN ({placeholders})",
                (listing_type, *listing_ids),
            )

    try:
        # Уведомления сохранённых поисков сначала — до удаления самих поисков.
        if "search_notifications" in existing_tables:
            connection.execute(
                "DELETE FROM search_notifications WHERE saved_search_id IN "
                "(SELECT id FROM saved_searches WHERE user_id=?)",
                (user_id,),
            )
            delete_listing_references("search_notifications")
        if "saved_searches" in existing_tables:
            connection.execute("DELETE FROM saved_searches WHERE user_id=?", (user_id,))

        # Все избранные и жалобы пользователя, а также ссылки других людей на
        # объявления, которые сейчас исчезнут.
        if "favorites" in existing_tables:
            connection.execute("DELETE FROM favorites WHERE user_id=?", (user_id,))
            delete_listing_references("favorites")
        if "reports" in existing_tables:
            connection.execute("DELETE FROM reports WHERE reporter_id=?", (user_id,))
            delete_listing_references("reports")
        if "listing_daily_metrics" in existing_tables:
            delete_listing_references("listing_daily_metrics")
        if "listing_revisions" in existing_tables:
            connection.execute("DELETE FROM listing_revisions WHERE owner_id=?", (user_id,))

        # Отзывы принадлежат и автору, и владельцу профиля. Удаление аккаунта
        # должно убрать оба вида данных, но удаление отдельного объявления не
        # стирает уже заработанный рейтинг владельца.
        if "reviews" in existing_tables:
            connection.execute(
                "DELETE FROM reviews WHERE reviewer_id=? OR owner_id=?", (user_id, user_id)
            )

        if "messages" in existing_tables:
            connection.execute(
                "DELETE FROM messages WHERE sender_id=? OR receiver_id=?", (user_id, user_id)
            )
            delete_listing_references("messages")

        # Состояния и блокировки относятся к конкретному собеседнику и не
        # должны оставаться у второго участника после удаления аккаунта.
        if "dialog_states" in existing_tables:
            connection.execute(
                "DELETE FROM dialog_states WHERE user_id=? OR other_id=?", (user_id, user_id)
            )
            delete_listing_references("dialog_states")
        if "dialog_blocks" in existing_tables:
            block_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(dialog_blocks)")
            }
            if "other_id" in block_columns:
                connection.execute(
                    "DELETE FROM dialog_blocks WHERE user_id=? OR other_id=?", (user_id, user_id)
                )
            else:  # Совместимость с базой до миграции диалогов.
                connection.execute("DELETE FROM dialog_blocks WHERE user_id=?", (user_id,))
            delete_listing_references("dialog_blocks")

        for table in (
            "password_reset_tokens",
            "email_verification_codes",
            "email_verifications",  # запись из ранних версий базы
            "email_change_codes",
            "admin_login_codes",
        ):
            if table in existing_tables:
                connection.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))

        # В лимитах хранятся только хеши subject. Идентификатор и известные
        # адреса можно сопоставить с аккаунтом, IP — намеренно не трогаем.
        if "rate_limit_events" in existing_tables:
            subjects = {str(user_id), (user["email"] or "").casefold()}
            if user["pending_email"]:
                subjects.add(user["pending_email"].casefold())
            subject_hashes = [sha256(subject.encode("utf-8")).hexdigest() for subject in subjects if subject]
            if subject_hashes:
                placeholders = ", ".join("?" for _ in subject_hashes)
                connection.execute(
                    f"DELETE FROM rate_limit_events WHERE subject_hash IN ({placeholders})",
                    subject_hashes,
                )

        for _listing_type, table in listing_tables:
            connection.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        connection.execute("DELETE FROM users WHERE id=?", (user_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    for photo_field in files_to_remove:
        delete_photos(photo_field)
    return True


def normalize_russian_phone(value):
    return normalize_international_phone(value, "+7")


def normalize_international_phone(value, country_code="+7"):
    raw = (value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not raw.startswith("+"):
        prefix = re.sub(r"\D", "", country_code or "")
        if not prefix:
            return None
        digits = prefix + digits.lstrip("0")
    return f"+{digits}" if 8 <= len(digits) <= 15 else None


def listing_contact_methods(data):
    methods = [method for method in data.getlist("contact_methods") if method in {"phone", "chat", "telegram"}]
    return ",".join(methods) if methods else "chat"


def checkbox_value(data, field):
    return 1 if data.get(field) in ("1", "on", "true") else 0


def listing_title_from_row(listing_type, row):
    if listing_type == "animal":
        return f"{row['type']} · {row['breed']}"
    return row["title"]


def listing_is_final(row):
    """Завершённая сделка не попадает в каталог, но остаётся в истории."""
    return (row["deal_status"] if "deal_status" in row.keys() else "open") in FINAL_OUTCOME_STATUSES


def photo_names(photo_field):
    """Возвращает только безопасные имена файлов из поля с фотографиями."""
    return {
        name.strip()
        for name in str(photo_field or "").split(",")
        if name.strip() and name.strip() == os.path.basename(name.strip()) and not name.strip().startswith(".")
    }


def revision_payload(revision):
    """Извлекает проверяемый JSON черновика без доверия к содержимому БД."""
    if not revision:
        return {}
    try:
        payload = json.loads(revision["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def get_listing_revision(listing_type, listing_id):
    if listing_type not in LISTING_TABLES:
        return None
    return db().execute(
        "SELECT * FROM listing_revisions WHERE listing_type=? AND listing_id=?",
        (listing_type, listing_id),
    ).fetchone()


def revision_form_item(listing_type, item, revision):
    """Показывает владельцу уже отправленную версию, не меняя живую карточку."""
    result = dict(item)
    payload = revision_payload(revision)
    for field in LISTING_REVISION_FIELDS[listing_type]:
        if field in payload:
            result[field] = payload[field]
    if revision:
        result["revision_status"] = revision["status"]
        result["revision_reason"] = revision["moderation_reason"]
    return result


def revision_payload_equal(first, second):
    """Сравнение только разрешённых публичных полей, а не сырого JSON."""
    # Отсутствующее поле — это тоже изменение: иначе неполный старый JSON
    # мог бы ошибочно считаться идентичным опубликованной карточке.
    return all(first.get(field) == second.get(field) for field in set(first) | set(second))


def revision_photo_cleanup(revision, base_photo, keep_photo=""):
    """Фото черновика можно удалить, не затрагивая опубликованную версию."""
    old_photo = revision_payload(revision).get("photo", "") if revision else ""
    removable = photo_names(old_photo) - photo_names(base_photo) - photo_names(keep_photo)
    return ",".join(sorted(removable))


def stage_listing_revision(listing_type, item, payload):
    """Создаёт или переотправляет единственную правку к опубликованной карточке."""
    old_revision = get_listing_revision(listing_type, item["id"])
    now = utcnow()
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if old_revision:
        db().execute(
            """UPDATE listing_revisions
                   SET payload_json=?, status='pending', moderation_reason=NULL, updated_at=?
                 WHERE id=?""",
            (serialized, now, old_revision["id"]),
        )
    else:
        db().execute(
            """INSERT INTO listing_revisions
                   (listing_type, listing_id, owner_id, payload_json, status, moderation_reason, submitted_at, updated_at)
                 VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)""",
            (listing_type, item["id"], item["user_id"], serialized, now, now),
        )
    return revision_photo_cleanup(old_revision, item["photo"], payload.get("photo", ""))


def discard_listing_revision(listing_type, listing_id, base_photo=""):
    """Удаляет черновик из очереди и возвращает только его самостоятельные фото."""
    revision = get_listing_revision(listing_type, listing_id)
    if not revision:
        return ""
    cleanup = revision_photo_cleanup(revision, base_photo)
    db().execute("DELETE FROM listing_revisions WHERE id=?", (revision["id"],))
    return cleanup


def apply_listing_revision(revision_id):
    """Атомарно переносит одобренные поля в карточку, сохраняя её историю."""
    revision = db().execute("SELECT * FROM listing_revisions WHERE id=?", (revision_id,)).fetchone()
    if not revision or revision["status"] != "pending":
        abort(404)
    listing_type = revision["listing_type"]
    table = LISTING_TABLES[listing_type][0]
    item = db().execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (revision["listing_id"], revision["owner_id"])).fetchone()
    if not item:
        db().execute("DELETE FROM listing_revisions WHERE id=?", (revision_id,))
        db().commit()
        abort(404)
    payload = revision_payload(revision)
    fields = LISTING_REVISION_FIELDS[listing_type]
    # Поля и имена таблиц берутся из фиксированного allowlist, а не из JSON.
    values = [payload.get(field, item[field]) for field in fields]
    assignments = ", ".join(f"{field}=?" for field in fields)
    db().execute(
        f"UPDATE {table} SET {assignments}, moderation_reason=NULL WHERE id=?",
        (*values, item["id"]),
    )
    db().execute("DELETE FROM listing_revisions WHERE id=?", (revision_id,))
    db().commit()
    old_only = ",".join(sorted(photo_names(item["photo"]) - photo_names(payload.get("photo", item["photo"]))))
    delete_photos(old_only)
    updated = db().execute(f"SELECT * FROM {table} WHERE id=?", (item["id"],)).fetchone()
    return revision, item, updated


def parse_views_data(value):
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    normalized = {}
    for day, count in data.items():
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)):
            try:
                normalized[str(day)] = max(0, int(count))
            except (TypeError, ValueError):
                continue
    return normalized


def record_listing_view(listing_type, item, user):
    """Единый счётчик просмотров для животных, услуг и товаров."""
    is_owner = bool(user and item["user_id"] == user["id"])
    views_data = parse_views_data(item["views_data"])
    views_total = int(item["views_total"] or 0)
    today = utcnow()[:10]
    if not is_owner:
        views_data[today] = views_data.get(today, 0) + 1
        views_total += 1
        table = LISTING_TABLES[listing_type][0]
        db().execute(
            f"UPDATE {table} SET views_total=?, views_data=? WHERE id=?",
            (views_total, json.dumps(views_data, separators=(",", ":")), item["id"]),
        )
        db().commit()
    return views_data.get(today, 0), views_total


def record_daily_metric(listing_type, listing_id, metric):
    """Увеличивает обезличенный счётчик действия за календарный день."""
    if listing_type not in LISTING_TABLES or metric not in TRACKED_METRICS:
        return
    db().execute(
        """INSERT INTO listing_daily_metrics (listing_type, listing_id, metric_day, metric, value)
             VALUES (?, ?, ?, ?, 1)
             ON CONFLICT(listing_type, listing_id, metric_day, metric)
             DO UPDATE SET value=value+1""",
        (listing_type, listing_id, utcnow()[:10], metric),
    )


def listing_metrics(listing_type, item, owner_id):
    """Приватная статистика владельца: только числа, без данных посетителей."""
    today = datetime.now(timezone.utc).date()
    recent_7 = (today - timedelta(days=6)).isoformat()
    recent_30 = (today - timedelta(days=29)).isoformat()
    views = parse_views_data(item.get("views_data") if isinstance(item, dict) else item["views_data"])
    views_7 = sum(value for day, value in views.items() if day >= recent_7)
    views_30 = sum(value for day, value in views.items() if day >= recent_30)
    metric_rows = db().execute(
        """SELECT metric,
                  COALESCE(SUM(CASE WHEN metric_day>=? THEN value ELSE 0 END), 0) AS seven,
                  COALESCE(SUM(CASE WHEN metric_day>=? THEN value ELSE 0 END), 0) AS thirty
             FROM listing_daily_metrics
             WHERE listing_type=? AND listing_id=?
             GROUP BY metric""",
        (recent_7, recent_30, listing_type, item["id"]),
    ).fetchall()
    metric_values = {row["metric"]: (int(row["seven"] or 0), int(row["thirty"] or 0)) for row in metric_rows}
    favorites_now = db().execute(
        "SELECT COUNT(*) FROM favorites WHERE listing_type=? AND listing_id=?",
        (listing_type, item["id"]),
    ).fetchone()[0]
    message_rows = db().execute(
        """SELECT
                 COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END), 0) AS seven,
                 COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END), 0) AS thirty
             FROM messages
             WHERE listing_type=? AND listing_id=? AND receiver_id=?""",
        (recent_7, recent_30, listing_type, item["id"], owner_id),
    ).fetchone()
    phone_7, phone_30 = metric_values.get("phone_click", (0, 0))
    telegram_7, telegram_30 = metric_values.get("telegram_click", (0, 0))
    chat_7, chat_30 = metric_values.get("chat_click", (0, 0))
    favorite_7, favorite_30 = metric_values.get("favorite_added", (0, 0))
    return {
        "views_7": views_7, "views_30": views_30,
        "favorites_now": int(favorites_now or 0), "favorites_7": favorite_7, "favorites_30": favorite_30,
        "phone_7": phone_7, "phone_30": phone_30,
        "telegram_7": telegram_7, "telegram_30": telegram_30,
        "chat_7": chat_7, "chat_30": chat_30,
        "contacts_7": phone_7 + telegram_7 + chat_7,
        "contacts_30": phone_30 + telegram_30 + chat_30,
        "messages_7": int(message_rows["seven"] or 0), "messages_30": int(message_rows["thirty"] or 0),
    }


def nonnegative_int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def notify_saved_searches(listing_type, listing_id):
    """Рассылает одно письмо на каждое новое подходящее объявление."""
    table = {"animal": "animals", "service": "services", "food": "food"}.get(listing_type)
    if not table:
        return
    listing = db().execute(
        f"SELECT * FROM {table} WHERE id=? AND status='active' AND {PUBLIC_OUTCOME_SQL}",
        (listing_id,),
    ).fetchone()
    if not listing:
        return
    kind_value = {"animal": "animals", "service": "services", "food": "food"}[listing_type]
    listing_data = dict(listing)
    search_text = " ".join(str(listing_data.get(key) or "") for key in ("title", "type", "breed", "description", "category")).casefold()
    for saved in db().execute("SELECT s.*, u.email FROM saved_searches s JOIN users u ON u.id=s.user_id").fetchall():
        try:
            filters = json.loads(saved["filters_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(filters, dict) or filters.get("kind") not in ("", kind_value):
            continue
        if saved["user_id"] == listing["user_id"]:
            continue
        if filters.get("city") and filters["city"] != listing["city"]:
            continue
        if filters.get("q") and normalize_search(filters["q"]) not in normalize_search(search_text):
            continue
        if listing_type == "animal":
            if filters.get("type") and filters["type"] != listing["type"]: continue
            if filters.get("deal") and filters["deal"] != listing["deal_type"]: continue
            if filters.get("sex") and filters["sex"] != (listing["sex"] or ""): continue
            if filters.get("vaccinated") and not listing["vaccinated"]: continue
            if filters.get("delivery") and not listing["delivery"]: continue
        try:
            price_min = int(filters.get("price_min") or 0)
            price_max = int(filters.get("price_max") or 0)
        except (TypeError, ValueError):
            continue
        if price_min and (listing["price"] or 0) < price_min: continue
        if price_max and (listing["price"] or 0) > price_max: continue
        exists = db().execute("SELECT 1 FROM search_notifications WHERE saved_search_id=? AND listing_type=? AND listing_id=?", (saved["id"], listing_type, listing_id)).fetchone()
        if exists:
            continue
        label = listing_title_from_row(listing_type, listing)
        details_url = public_url({"animal": "animal_detail", "service": "service_detail", "food": "food_detail"}[listing_type], **{f"{listing_type}_id": listing_id})
        if send_email(saved["email"], f"ZooLand: новое объявление по подписке «{saved['name']}»", f"Появилось подходящее объявление: {label}\n\n{details_url}"):
            db().execute("INSERT INTO search_notifications (saved_search_id, listing_type, listing_id, sent_at) VALUES (?, ?, ?, ?)", (saved["id"], listing_type, listing_id, utcnow()))
    db().commit()


KNOWN_EMAIL_DOMAINS = {
    "gmail.com", "yandex.ru", "ya.ru", "mail.ru", "bk.ru", "inbox.ru", "list.ru",
    "outlook.com", "hotmail.com", "icloud.com", "rambler.ru", "yahoo.com", "proton.me", "protonmail.com",
}


def is_valid_email(value):
    email = (value or "").strip().casefold()
    return bool(re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", email) and email.rsplit("@", 1)[1] in KNOWN_EMAIL_DOMAINS)


def normalize_search(value):
    """Нормализует строку для поиска на русском, английском и ошибочной раскладке."""
    return " ".join("".join(char if char.isalnum() else " " for char in (value or "").casefold().replace("ё", "е")).split())


def latin_to_russian(value):
    value = value.casefold()
    for source, target in (("shch", "щ"), ("sch", "щ"), ("yo", "ё"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я")):
        value = value.replace(source, target)
    return value.translate(str.maketrans("abvgdeziyklmnoprstufhe", "абвгдезийклмнопрстуфхе"))


def search_variants(value):
    raw = value or ""
    switched_to_ru, switched_to_en = raw.translate(EN_TO_RU), raw.translate(RU_TO_EN)
    variants = {raw, switched_to_ru, switched_to_en, latin_to_russian(raw), latin_to_russian(switched_to_en)}
    variants.update(item.translate(RU_TO_LATIN) for item in list(variants))
    return {normalized for item in variants if (normalized := normalize_search(item))}


def animal_matches_search(animal, query):
    variants = search_variants(query)
    fields = [animal["type"] or "", animal["breed"] or "", animal["description"] or "", animal["city"] or ""]
    # Английские названия распространённых типов и пород также сопоставляются с русским каталогом.
    fields.extend(TYPE_ALIASES.get((animal["type"] or "").casefold(), ()))
    fields.extend(BREED_ALIASES.get(animal["breed"] or "", ()))
    normalized_fields = [normalize_search(field) for field in fields]
    return any(variant in field for variant in variants for field in normalized_fields)


def text_matches_search(fields, query):
    """Ищет запрос во всех пользовательских и справочных текстовых полях.

    Для карточек услуг и товаров используем ту же нормализацию, что и для
    животных: «мейн-кун», «мейн кун» и ошибочная раскладка не должны давать
    разные результаты.  Поля проверяются по отдельности, чтобы фраза не
    "склеивалась" на границе, например, названия и описания.
    """
    variants = search_variants(query)
    normalized_fields = [normalize_search(value) for value in fields if value]
    return any(variant in field for variant in variants for field in normalized_fields)


def _query_contains_catalog_term(query_variants, terms):
    """Проверяет, что запрос содержит целый справочный термин.

    Например, «мейн кун котята» содержит породу «Мейн-кун», но «кот» не
    совпадает случайно с частью другого слова.  Термины короче трёх символов
    намеренно игнорируются: они слишком легко дают ложные совпадения.
    """
    padded_queries = tuple(f" {variant} " for variant in query_variants)
    for term in terms:
        for term_variant in search_variants(term):
            if len(term_variant) < 3:
                continue
            needle = f" {term_variant} "
            if any(needle in query for query in padded_queries):
                return True
    return False


def inferred_search_kinds(query, selected_kind, animal_type="", filters=None):
    """Возвращает каталоги, уместные для глобального поискового запроса.

    Без выбранного вида каталога порода или вид животного — это намерение
    искать животное, а не любой текст с этими словами.  Иначе «мейн кун» мог
    попадать в груминг или лежанку, если продавец упомянул там кошек.  Явный
    выбор каталога всегда важнее автоматического определения: так посетитель
    по-прежнему может искать услугу для мейн-куна в разделе услуг.
    """
    allowed = {"animals", "services", "food", "accessories"}
    if selected_kind in allowed:
        return {selected_kind}

    filters = filters or {}
    # Фильтры, существующие только у животных, также явно задают каталог.
    animal_only_filters = ("breed", "age_min", "age_max", "sex", "deal", "only_photo", "vaccinated", "delivery")
    if animal_type or any(filters.get(name) for name in animal_only_filters):
        return {"animals"}
    if not query:
        return allowed

    variants = search_variants(query)
    animal_terms = list(ANIMAL_TYPES) + list(TYPE_ALIASES)
    animal_terms.extend(alias for aliases in TYPE_ALIASES.values() for alias in aliases)
    animal_terms.extend(breed for breeds in BREEDS.values() for breed in breeds)
    animal_terms.extend(BREED_ALIASES)
    animal_terms.extend(alias for aliases in BREED_ALIASES.values() for alias in aliases)

    kinds = set()
    if _query_contains_catalog_term(variants, animal_terms):
        kinds.add("animals")
    if _query_contains_catalog_term(variants, SERVICE_TYPES):
        kinds.add("services")

    # Полные названия категорий надёжнее, чем отдельное слово «для» или
    # «другое». Дополняем их частыми самостоятельными словами товаров.
    food_terms = list(FOOD_TYPES) + ["корм", "лакомства", "наполнитель", "витамины"]
    accessory_terms = list(ACCESSORY_TYPES) + [
        "игрушки", "лежанка", "домик", "когтеточка", "ошейник", "поводок",
        "шлейка", "переноска", "клетка", "аквариум", "террариум",
    ]
    if _query_contains_catalog_term(variants, food_terms):
        kinds.add("food")
    if _query_contains_catalog_term(variants, accessory_terms):
        kinds.add("accessories")

    return kinds or allowed


ANIMAL_FIELD_LABELS = {
    "type": "вид животного",
    "breed": "порода",
    "age": "возраст",
    "price": "цена",
    "city": "город",
    "contacts": "номер телефона",
}


def validate_animal_listing(data, profile_phone=None, current_phone=None):
    """Проверяет данные животного до лимита публикаций и загрузки файлов.

    HTML-атрибут required удобен, но его можно обойти обычным POST-запросом.
    Возвращаем словарь по именам полей, чтобы интерфейс мог выделить все
    ошибки за один раз, а не отправлять пользователя по одной к каждому полю.
    """
    errors = {}
    animal_type = (data.get("type") or "").strip()
    breed = (data.get("breed") or "").strip()
    city = (data.get("city") or "").strip()
    deal_type = (data.get("deal_type") or "sale").strip()

    if animal_type not in ANIMAL_TYPES:
        errors["type"] = "Выберите вид животного."
    if not breed:
        errors["breed"] = "Укажите породу или напишите «метис»."
    elif len(breed) > 100:
        errors["breed"] = "Название породы не должно быть длиннее 100 символов."

    for field, label, maximum in (("age", "возраст", 100), ("price", "цену", 100_000_000)):
        value = (data.get(field) or "").strip()
        try:
            number = int(value)
        except (TypeError, ValueError):
            errors[field] = f"Укажите {label} целым числом."
            continue
        if number < 0 or number > maximum:
            errors[field] = (
                "Возраст должен быть от 0 до 100 лет."
                if field == "age" else "Укажите корректную цену."
            )

    if not city:
        errors["city"] = "Укажите город."
    elif len(city) > 100:
        errors["city"] = "Название города не должно быть длиннее 100 символов."
    if deal_type not in DEAL_TYPES:
        errors["deal_type"] = "Выберите корректный тип сделки."

    phone_source = data.get("phone_source")
    use_saved_phone = phone_source in {"profile", "current"}
    saved_phone = profile_phone if phone_source == "profile" else current_phone
    if use_saved_phone:
        if not saved_phone:
            errors["contacts"] = "В профиле нет номера телефона. Укажите другой номер."
    elif not normalize_international_phone(
        data.get("contacts"), data.get("phone_country_custom") or data.get("phone_country")
    ):
        errors["contacts"] = "Укажите корректный международный номер телефона."

    return errors


def animal_validation_redirect(errors, *, animal_id=None):
    """Возвращает к форме с машиночитаемым перечнем неверных полей."""
    fields = list(errors)
    labels = ", ".join(ANIMAL_FIELD_LABELS.get(field, field) for field in fields)
    flash(f"Проверьте поля: {labels}.", "error")
    if animal_id is None:
        return redirect(url_for(
            "index", publish_error=fields[0], publish_errors=",".join(fields), _anchor="publish"
        ))
    return redirect(url_for(
        "edit_animal", animal_id=animal_id, form_error=fields[0], form_errors=",".join(fields)
    ))


def distance_km(lat1, lon1, lat2, lon2):
    """Расстояние между координатами в километрах по формуле гаверсинуса."""
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = db().execute("SELECT id, name, email, phone, email_verified, city, gender, birth_date, about, avatar, telegram, is_admin, session_version FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or session.get("session_version") != user["session_version"]:
        session.clear()
        return None
    return user


def current_user_is_moderator():
    """Проверяет подтверждённую сессию модератора (в текущей схеме — is_admin)."""
    if not session.get("user_id"):
        return False
    user = current_user()
    return bool(user and user["is_admin"])


def establish_authenticated_session(user):
    """Начинает новую сессию после входа/подтверждения и исключает фиксацию старой."""
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["session_version"] = user["session_version"]


def normalize_telegram(value):
    """Принимает @username или ссылку t.me/username и возвращает username."""
    username = re.sub(r"^(?:https?://)?(?:www\.)?t\.me/", "", (value or "").strip(), flags=re.I).lstrip("@")
    return username if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username) else None


def attach_owner_telegrams(items):
    """Добавляет Telegram для карточек, не раскрывая живой профиль в объявлении.

    У услуг и товаров значение уже сохранено в самой карточке во время
    публикации. Для животных сохраняем прежнее поведение: Telegram берётся
    из профиля владельца, потому что отдельного снимка там пока нет.
    """
    ids = set()
    for item in items:
        if "telegram" in item:
            item["owner_telegram"] = item.get("telegram")
        elif item.get("user_id"):
            ids.add(item["user_id"])
    if not ids:
        return items
    placeholders = ",".join("?" for _ in ids)
    telegrams = {row["id"]: row["telegram"] for row in db().execute(
        f"SELECT id, telegram FROM users WHERE id IN ({placeholders})", tuple(ids)
    )}
    for item in items:
        if "owner_telegram" not in item:
            item["owner_telegram"] = telegrams.get(item["user_id"])
    return items


def guest_token():
    return request.cookies.get("zooland_guest")


def can_manage(animal, user=None):
    user = user or current_user()
    return bool((user and animal["user_id"] == user["id"]) or (not user and guest_token() and animal["guest_token"] == guest_token()))


def cleanup_expired():
    connection = db()
    now = utcnow()
    # Гостевые объявления удаляются, пользовательские после недели уходят в архив.
    connection.execute("DELETE FROM animals WHERE status='active' AND user_id IS NULL AND expires_at < ?", (now,))
    connection.execute("""UPDATE animals SET status='archived', archived_at=?
                          WHERE status='active' AND user_id IS NOT NULL AND expires_at < ?""", (now, now))
    connection.execute("UPDATE services SET status='expired' WHERE status='active' AND expires_at < ?", (now,))
    connection.execute("UPDATE food SET status='expired' WHERE status='active' AND expires_at < ?", (now,))
    connection.execute("""UPDATE advertising_requests
                             SET status='completed', updated_at=?
                           WHERE status='active' AND end_date IS NOT NULL AND end_date!='' AND end_date<?""",
                       (now, advertising_today()))
    # Для статистики достаточно последних 30 дней с небольшим запасом.
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=35)).isoformat()
    connection.execute("DELETE FROM listing_daily_metrics WHERE metric_day<?", (cutoff,))
    connection.commit()


@app.before_request
def housekeeping():
    cleanup_expired()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Войдите в аккаунт, чтобы открыть личный кабинет.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if not user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def global_template_data():
    user = current_user()
    favs = set()
    if user:
        favs = {f"{row['listing_type']}:{row['listing_id']}" for row in
                db().execute("SELECT listing_type, listing_id FROM favorites WHERE user_id=?", (user["id"],)).fetchall()}
    return {"account_user": user, "animal_types": ANIMAL_TYPES, "breeds": BREEDS, "can_manage": can_manage,
            "russian_cities": RUSSIAN_CITIES, "service_types": SERVICE_TYPES, "pet_types": PET_TYPES,
            "food_types": FOOD_TYPES, "accessory_types": ACCESSORY_TYPES, "deal_types": DEAL_TYPES, "deal_type_labels": DEAL_TYPE_LABELS,
            "outcome_status_labels": OUTCOME_STATUS_LABELS, "outcome_status_options": OUTCOME_STATUS_OPTIONS,
            "final_outcome_statuses": FINAL_OUTCOME_STATUSES,
            "country_codes": COUNTRY_CODES, "report_reasons": REPORT_REASONS, "moderation_reasons": MODERATION_REASONS,
            "is_favorite": lambda t, i: f"{t}:{i}" in favs}


@app.route("/")
def index():
    city, animal_type, search, kind = (request.args.get(key, "").strip() for key in ("city", "type", "q", "kind"))
    sort = request.args.get("sort", "new")
    filters = {key: request.args.get(key, "").strip() for key in ("price_min", "price_max", "age_min", "age_max", "sex", "breed", "deal", "only_photo", "vaccinated", "delivery")}
    search_kinds = inferred_search_kinds(search, kind, animal_type, filters)
    def number(value):
        try: return max(0, int(value)) if value else None
        except ValueError: return None
    # Животные (объявления о продаже) — бесплатная публикация.
    if "animals" in search_kinds:
        query, conditions, params = "SELECT * FROM animals", ["status='active'", PUBLIC_OUTCOME_SQL], []
        if city: conditions.append("city=?"); params.append(city)
        if animal_type: conditions.append("type=?"); params.append(animal_type)
        if filters["breed"]: conditions.append("breed LIKE ?"); params.append(f"%{filters['breed']}%")
        if filters["sex"] in ("male", "female"): conditions.append("sex=?"); params.append(filters["sex"])
        if filters["deal"] in DEAL_TYPES: conditions.append("deal_type=?"); params.append(filters["deal"])
        if filters["only_photo"]: conditions.append("photo IS NOT NULL AND photo!=''")
        if filters["vaccinated"]: conditions.append("vaccinated=1")
        if filters["delivery"]: conditions.append("delivery=1")
        for key, column, op in (("price_min", "price", ">="), ("price_max", "price", "<="), ("age_min", "age", ">="), ("age_max", "age", "<=")):
            if (value := number(filters[key])) is not None: conditions.append(f"{column}{op}?"); params.append(value)
        query += " WHERE " + " AND ".join(conditions)
        query += {"price_asc": " ORDER BY price ASC", "price_desc": " ORDER BY price DESC"}.get(sort, " ORDER BY id DESC")
        animals = [dict(row) for row in db().execute(query, params).fetchall()]
        if search:
            animals = [animal for animal in animals if animal_matches_search(animal, search)]
        for animal in animals:
            animal["kind"] = "animal"
    else:
        animals = []
    # Услуги для животных.
    if "services" in search_kinds:
        query, conditions, params = "SELECT * FROM services", ["status='active'", PUBLIC_OUTCOME_SQL], []
        if city: conditions.append("city=?"); params.append(city)
        for key, op in (("price_min", ">="), ("price_max", "<=")):
            if (value := number(filters[key])) is not None: conditions.append(f"price{op}?"); params.append(value)
        query += " WHERE " + " AND ".join(conditions) + " ORDER BY id DESC"
        services = [dict(row) for row in db().execute(query, params).fetchall()]
        if search:
            services = [s for s in services if text_matches_search(
                (s["type"], s["title"], s["pet_types"], s["description"]), search
            )]
        for service in services:
            service["kind"] = "service"
    else:
        services = []
    # Зоопитание и аксессуары хранятся в общей таблице, но выбор в поиске
    # ограничивает результат категориями соответствующего каталога.
    if {"food", "accessories"} & search_kinds:
        query, conditions, params = "SELECT * FROM food", ["status='active'", PUBLIC_OUTCOME_SQL], []
        product_categories = []
        if "food" in search_kinds:
            product_categories.extend(FOOD_TYPES)
        if "accessories" in search_kinds:
            product_categories.extend(ACCESSORY_TYPES)
        placeholders = ",".join("?" for _ in product_categories)
        conditions.append(f"category IN ({placeholders})")
        params.extend(product_categories)
        if city: conditions.append("city=?"); params.append(city)
        for key, op in (("price_min", ">="), ("price_max", "<=")):
            if (value := number(filters[key])) is not None: conditions.append(f"price{op}?"); params.append(value)
        query += " WHERE " + " AND ".join(conditions) + " ORDER BY id DESC"
        food = [dict(row) for row in db().execute(query, params).fetchall()]
        if search:
            food = [f for f in food if text_matches_search(
                (f["category"], f["title"], f["description"]), search
            )]
        for item in food:
            item["kind"] = "food"
    else:
        food = []
    page_category = kind if kind in ADVERTISING_CATEGORIES and kind != "all" else "all"
    items = sorted(list(animals) + list(services) + list(food), key=lambda item: item["created_at"], reverse=True)
    items = apply_promoted_listings(items, page_category, city=city)[:24]
    items = attach_owner_telegrams(items)
    homepage_ads = active_advertisements(
        "homepage-banner", page_category, city=city, include_all_categories=page_category == "all"
    )
    category_ads = active_advertisements("category-banner", page_category, city=city) if page_category != "all" else []
    return render_template("index.html", items=items, animals=animals, services=services, food=food,
                           cities=RUSSIAN_CITIES, selected_city=city, selected_type=animal_type,
                           search=search, sort=sort, kind=kind, filters=filters,
                           homepage_ads=homepage_ads, category_ads=category_ads)


@app.route("/animal/<int:animal_id>")
def animal_detail(animal_id):
    animal = db().execute("SELECT * FROM animals WHERE id=? AND status='active'", (animal_id,)).fetchone()
    if not animal:
        abort(404)
    user = current_user()
    views_today, views_total = record_listing_view("animal", animal, user)
    owner = None
    if animal["user_id"]:
        owner = db().execute("SELECT id, name, telegram FROM users WHERE id=?", (animal["user_id"],)).fetchone()
    is_owner = bool(user and animal["user_id"] == user["id"])
    similar = []
    animal_coords = CITY_COORDS.get(animal["city"])
    if animal_coords:
        # Близкие города в радиусе 50 км от города объявления.
        nearby_cities = {city for city, coords in CITY_COORDS.items()
                         if distance_km(animal_coords[0], animal_coords[1], coords[0], coords[1]) <= SIMILAR_RADIUS_KM}
        if nearby_cities:
            placeholders = ",".join("?" for _ in nearby_cities)
            similar = db().execute(
                f"SELECT * FROM animals WHERE status='active' AND {PUBLIC_OUTCOME_SQL} AND type=? AND city IN ({placeholders}) AND id!=? ORDER BY id DESC LIMIT ?",
                (animal["type"], *nearby_cities, animal_id, SIMILAR_LIMIT)).fetchall()
    review_context = owner_review_context(owner, "animal", animal_id, user)
    outcome = animal["deal_status"] or "open"
    return render_template("animal.html", animal=animal, owner=owner, similar=similar,
                           views_today=views_today, views_total=views_total, is_owner=is_owner,
                           outcome=outcome, is_final_outcome=outcome in FINAL_OUTCOME_STATUSES,
                           listing_type="animal", listing_id=animal_id, **review_context)


@app.route("/services")
def services_list():
    """Каталог услуг для животных."""
    service_type, city, search = (request.args.get(key, "").strip() for key in ("type", "city", "q"))
    query, conditions, params = "SELECT * FROM services", ["status='active'", PUBLIC_OUTCOME_SQL], []
    if service_type:
        conditions.append("type=?")
        params.append(service_type)
    if city:
        conditions.append("city=?")
        params.append(city)
    query += " WHERE " + " AND ".join(conditions) + " ORDER BY id DESC"
    services = db().execute(query, params).fetchall()
    if search:
        search = search.casefold()
        services = [s for s in services if search in (s["title"] or "").casefold() or search in (s["description"] or "").casefold()]
    services = attach_owner_telegrams([dict(service) for service in services])
    services = apply_promoted_listings(services, "services", fixed_listing_type="service", city=city)
    category_ads = active_advertisements("category-banner", "services", city=city)
    return render_template("services.html", services=services, cities=RUSSIAN_CITIES,
                           selected_type=service_type, selected_city=city, search=search,
                           category_ads=category_ads)


@app.route("/service/<int:service_id>")
def service_detail(service_id):
    service = db().execute("SELECT * FROM services WHERE id=? AND status='active'", (service_id,)).fetchone()
    if not service:
        abort(404)
    owner = None
    my_service = False
    user = current_user()
    if service["user_id"]:
        # Телефон и Telegram выводятся из снимка в service, а не из живого
        # профиля: это не даёт обойти модерацию сменой контактов.
        owner = db().execute("SELECT id, name FROM users WHERE id=?", (service["user_id"],)).fetchone()
        my_service = bool(user and user["id"] == service["user_id"])
    views_today, views_total = record_listing_view("service", service, user)
    review_context = owner_review_context(owner, "service", service_id, user)
    outcome = service["deal_status"] or "open"
    return render_template("service.html", service=service, owner=owner, my_service=my_service,
                           views_today=views_today, views_total=views_total,
                           outcome=outcome, is_final_outcome=outcome in FINAL_OUTCOME_STATUSES,
                           listing_type="service", listing_id=service_id, **review_context)


@app.route("/service/add", methods=["POST"])
@login_required
def add_service():
    user = current_user()
    data = request.form
    service_type = data.get("type", "")
    title = data.get("title", "").strip()
    if service_type not in SERVICE_TYPES or len(title) < 3:
        flash("Выберите тип услуги и укажите название не короче 3 символов.", "error")
        return redirect(url_for("services_list"))
    if not allow_sensitive_action("publish", str(user["id"])):
        flash("Слишком много новых объявлений. Попробуйте позже.", "error")
        return redirect(url_for("services_list"))
    pet_types = ",".join(data.getlist("pet_types")) if data.getlist("pet_types") else "Другие животные"
    city = data.get("city", "").strip()
    try:
        filenames = save_photos(request.files.getlist("photos"))
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("services_list"))
    photos = ",".join(filenames)
    db().execute("""INSERT INTO services (type, title, pet_types, city, price, description, photo, contacts, telegram, user_id, status, created_at, expires_at, months_published, contact_methods)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                 (service_type, title, pet_types, city, data.get("price", 0) or 0, data.get("description", ""), photos,
                  user["phone"] or "", user["telegram"] or "", user["id"], utcnow(), iso_after_days(SERVICE_DAYS), 0,
                  listing_contact_methods(data)))
    db().commit()
    flash("Услуга отправлена на модерацию. После одобрения она появится в каталоге.", "success")
    return redirect(url_for("account"))


@app.route("/service/<int:service_id>/renew", methods=["POST"])
@login_required
def renew_service(service_id):
    service = db().execute("SELECT * FROM services WHERE id=? AND user_id=?", (service_id, current_user()["id"])).fetchone()
    if not service:
        abort(404)
    if service["status"] not in {"active", "expired"}:
        abort(400)
    db().execute("UPDATE services SET status='active', expires_at=? WHERE id=?",
                 (iso_after_days(SERVICE_DAYS), service_id))
    db().commit()
    flash("Услуга бесплатно продлена на 30 дней.", "success")
    return redirect(url_for("services_list"))


@app.route("/service/<int:service_id>/delete", methods=["POST"])
@login_required
def delete_service(service_id):
    service = db().execute("SELECT * FROM services WHERE id=? AND user_id=?", (service_id, current_user()["id"])).fetchone()
    if not service:
        abort(404)
    revision_photos = discard_listing_revision("service", service_id, service["photo"])
    db().execute("DELETE FROM listing_daily_metrics WHERE listing_type='service' AND listing_id=?", (service_id,))
    db().execute("DELETE FROM services WHERE id=?", (service_id,))
    db().commit()
    delete_photos(service["photo"])
    delete_photos(revision_photos)
    flash("Услуга удалена.", "success")
    return redirect(url_for("services_list"))


@app.route("/service/<int:service_id>/edit", methods=["GET", "POST"])
@login_required
def edit_service(service_id):
    service = db().execute("SELECT * FROM services WHERE id=? AND user_id=?", (service_id, current_user()["id"])).fetchone()
    if not service:
        abort(404)
    revision = get_listing_revision("service", service_id)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        service_type = request.form.get("type", "")
        if service_type not in SERVICE_TYPES or len(title) < 3:
            flash("Выберите тип услуги и укажите название не короче 3 символов.", "error")
            return redirect(url_for("edit_service", service_id=service_id))
        try:
            new_photos = save_photos(request.files.getlist("photos"))
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("edit_service", service_id=service_id))
        revision_values = revision_payload(revision)
        photos = revision_values.get("photo", service["photo"] or "") if revision else service["photo"] or ""
        if new_photos:
            photos = ",".join(new_photos)
        refresh_profile_contacts = request.form.get("refresh_profile_contacts") == "1"
        profile_contacts = current_user()
        payload = {
            "type": service_type,
            "title": title,
            "pet_types": ",".join(request.form.getlist("pet_types")) or "Другие животные",
            "city": request.form.get("city", "").strip(),
            "price": nonnegative_int(request.form.get("price", 0)),
            "description": request.form.get("description", ""),
            "photo": photos,
            # Снимок обновляется из профиля только по явному выбору владельца
            # и затем всё равно проходит отдельную модерацию.
            "contacts": (profile_contacts["phone"] or "") if refresh_profile_contacts else revision_values.get("contacts", service["contacts"] or ""),
            "telegram": (profile_contacts["telegram"] or "") if refresh_profile_contacts else revision_values.get("telegram", service["telegram"] or ""),
            "contact_methods": listing_contact_methods(request.form),
        }
        if service["status"] == "active":
            if not revision and revision_payload_equal(payload, {field: service[field] for field in LISTING_REVISION_FIELDS["service"]}):
                flash("Изменений нет — опубликованная версия осталась прежней.", "success")
                return redirect(url_for("account"))
            if revision and revision["status"] == "pending" and revision_payload_equal(payload, revision_values):
                if new_photos:
                    delete_photos(",".join(new_photos))
                flash("Эта версия уже находится на проверке.", "success")
                return redirect(url_for("account"))
            obsolete_photos = stage_listing_revision("service", service, payload)
            db().commit()
            delete_photos(obsolete_photos)
            flash("Изменения отправлены на повторную модерацию. Пока посетители видят предыдущую опубликованную версию.", "success")
            return redirect(url_for("account"))
        old_photo = service["photo"] or ""
        db().execute(
            """UPDATE services SET type=?, title=?, pet_types=?, city=?, price=?, description=?, photo=?, contacts=?, telegram=?, contact_methods=?,
                                   status='pending', moderation_reason=NULL, deal_status='open', outcome_at=NULL
                 WHERE id=?""",
            (*[payload[field] for field in LISTING_REVISION_FIELDS["service"]], service_id),
        )
        db().commit()
        if new_photos:
            delete_photos(old_photo)
        flash("Услуга отправлена на повторную модерацию.", "success")
        return redirect(url_for("account"))
    return render_template("edit_listing.html", item=revision_form_item("service", service, revision), listing_type="service", revision=revision)


@app.route("/add", methods=["POST"])
@login_required
def add_animal():
    user = current_user()
    data = request.form
    validation_errors = validate_animal_listing(data, profile_phone=user["phone"])
    if validation_errors:
        return animal_validation_redirect(validation_errors)
    phone = user["phone"] if data.get("phone_source") == "profile" else normalize_international_phone(data.get("contacts"), data.get("phone_country_custom") or data.get("phone_country"))
    if not allow_sensitive_action("publish", str(user["id"])):
        flash("Слишком много новых объявлений. Попробуйте позже.", "error")
        return redirect(url_for("index", _anchor="publish"))
    try:
        filenames = save_photos(request.files.getlist("photos"))
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index", _anchor="publish"))
    photos = ",".join(filenames)
    deal_type = data.get("deal_type", "sale")
    if deal_type not in DEAL_TYPES:
        deal_type = "sale"
    methods = listing_contact_methods(data)
    sex = data.get("sex", "") if data.get("sex", "") in ("male", "female") else None
    db().execute("""INSERT INTO animals (type, breed, age, price, city, description, contacts, photo, user_id, guest_token, status, created_at, expires_at, deal_type, contact_methods, sex, birth_date, vaccinated, vet_passport, pedigree, sterilized, delivery)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (data["type"], data["breed"], data["age"], data["price"], data["city"], data.get("description", ""), phone, photos,
                  user["id"], None, utcnow(), iso_after_days(FREE_DAYS), deal_type, methods, sex, data.get("birth_date") or None,
                  checkbox_value(data, "vaccinated"), checkbox_value(data, "vet_passport"), checkbox_value(data, "pedigree"), checkbox_value(data, "sterilized"), checkbox_value(data, "delivery")))
    db().commit()
    flash("Объявление отправлено на модерацию. После одобрения оно появится в каталоге.", "success")
    return redirect(url_for("account"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user(): return redirect(url_for("account"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "")
        phone = normalize_international_phone(request.form.get("phone"), request.form.get("phone_country_custom") or request.form.get("phone_country"))
        if not allow_sensitive_action("register", email):
            flash("Слишком много попыток регистрации. Попробуйте позже.", "error")
        elif len(name) < 2 or not is_valid_email(email) or not phone:
            flash("Укажите имя, международный номер и корректный email с @ на известном почтовом сервисе.", "error")
        elif (error := password_error(password, name=name, email=email)):
            flash(error, "error")
        elif db().execute("SELECT 1 FROM users WHERE email=? OR phone=?", (email, phone)).fetchone():
            flash("Этот email или номер уже зарегистрирован. Войдите в аккаунт.", "error")
        else:
            cursor = db().execute("INSERT INTO users (name, email, phone, password_hash, email_verified, created_at) VALUES (?, ?, ?, ?, 0, ?)", (name, email, phone, generate_password_hash(password), utcnow()))
            token = guest_token()
            if token: db().execute("UPDATE animals SET user_id=?, guest_token=NULL WHERE guest_token=?", (cursor.lastrowid, token))
            db().commit()
            if not create_email_verification_code(cursor.lastrowid, email):
                db().execute("DELETE FROM users WHERE id=?", (cursor.lastrowid,)); db().commit()
                flash("Не удалось отправить код подтверждения. Попробуйте зарегистрироваться позже.", "error")
            else:
                session.clear()
                session["verify_user_id"] = cursor.lastrowid
                response = redirect(url_for("verify_email")); response.delete_cookie("zooland_guest"); return response
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user(): return redirect(url_for("account"))
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        if not allow_sensitive_action("login", email):
            flash("Слишком много попыток входа. Попробуйте через 15 минут.", "error")
            return redirect(url_for("login"))
        user = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            if not user["email_verified"]:
                session.clear()
                session["verify_user_id"] = user["id"]
                flash("Сначала подтвердите email кодом из письма.", "error")
                return redirect(url_for("verify_email"))
            if user["is_admin"]:
                if not allow_sensitive_action("admin_2fa", str(user["id"])):
                    flash("Слишком много запросов кода. Попробуйте позже.", "error")
                    return redirect(url_for("login"))
                if not create_admin_login_code(user["id"], user["email"]):
                    flash("Не удалось отправить код администратора. Проверьте почтовые настройки.", "error")
                    return redirect(url_for("login"))
                session.clear()
                session["admin_2fa_user_id"] = user["id"]
                return redirect(url_for("verify_admin_login"))
            establish_authenticated_session(user)
            return redirect(url_for("account"))
        flash("Неверный email или пароль.", "error")
    return render_template("auth.html", mode="login")


@app.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    user_id = session.get("verify_user_id")
    user = db().execute("SELECT id, email, is_admin, session_version FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
    if not user:
        flash("Начните регистрацию заново.", "error")
        return redirect(url_for("register"))
    if request.method == "POST":
        code = re.sub(r"\D", "", request.form.get("code", ""))
        row = db().execute("SELECT * FROM email_verification_codes WHERE user_id=? AND used_at IS NULL ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        if not row or row["expires_at"] <= utcnow() or row["attempts"] >= 5:
            flash("Код истёк. Запросите новый.", "error")
        elif not hmac.compare_digest(short_code_hash(code), row["code_hash"]):
            db().execute("UPDATE email_verification_codes SET attempts=attempts+1 WHERE id=?", (row["id"],)); db().commit()
            flash("Неверный код.", "error")
        else:
            db().execute("UPDATE email_verification_codes SET used_at=? WHERE id=?", (utcnow(), row["id"]))
            db().execute("UPDATE users SET email_verified=1 WHERE id=?", (user["id"],))
            db().commit()
            if user["is_admin"]:
                if not create_admin_login_code(user["id"], user["email"]):
                    flash("Email подтверждён, но код администратора не удалось отправить.", "error")
                    return redirect(url_for("login"))
                session.clear(); session["admin_2fa_user_id"] = user["id"]
                return redirect(url_for("verify_admin_login"))
            establish_authenticated_session(user)
            flash("Email подтверждён.", "success")
            return redirect(url_for("account"))
    return render_template("verify_email.html", email=user["email"])


@app.route("/verify-email/resend", methods=["POST"])
def resend_verification_email():
    user_id = session.get("verify_user_id")
    user = db().execute("SELECT id, email FROM users WHERE id=? AND email_verified=0", (user_id,)).fetchone() if user_id else None
    if not user:
        return redirect(url_for("register"))
    if not allow_sensitive_action("verify_resend", str(user["id"])):
        flash("Слишком много запросов кода. Попробуйте позже.", "error")
    elif create_email_verification_code(user["id"], user["email"]):
        flash("Новый код отправлен.", "success")
    else:
        flash("Не удалось отправить код. Попробуйте позже.", "error")
    return redirect(url_for("verify_email"))


@app.route("/admin/verify-login", methods=["GET", "POST"])
def verify_admin_login():
    user_id = session.get("admin_2fa_user_id")
    user = db().execute("SELECT id, email, is_admin, session_version FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
    if not user or not user["is_admin"]:
        session.pop("admin_2fa_user_id", None)
        flash("Сначала войдите с email и паролем администратора.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        if not allow_sensitive_action("admin_2fa", str(user["id"])):
            flash("Слишком много попыток. Запросите новый код позже.", "error")
            return redirect(url_for("verify_admin_login"))
        code = re.sub(r"\D", "", request.form.get("code", ""))
        row = db().execute("SELECT * FROM admin_login_codes WHERE user_id=? AND used_at IS NULL ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        if not row or row["expires_at"] <= utcnow() or row["attempts"] >= 5:
            flash("Код истёк. Запросите новый.", "error")
        elif not hmac.compare_digest(short_code_hash(code), row["code_hash"]):
            db().execute("UPDATE admin_login_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
            db().commit()
            flash("Неверный код.", "error")
        else:
            db().execute("UPDATE admin_login_codes SET used_at=? WHERE id=?", (utcnow(), row["id"]))
            db().commit()
            establish_authenticated_session(user)
            flash("Двухэтапная проверка пройдена.", "success")
            return redirect(url_for("account"))
    return render_template("admin_2fa.html", email=user["email"])


@app.route("/admin/verify-login/resend", methods=["POST"])
def resend_admin_login_code():
    user_id = session.get("admin_2fa_user_id")
    user = db().execute("SELECT id, email, is_admin FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
    if not user or not user["is_admin"]:
        return redirect(url_for("login"))
    if not allow_sensitive_action("admin_2fa", str(user["id"])):
        flash("Слишком много запросов. Попробуйте позже.", "error")
    elif create_admin_login_code(user["id"], user["email"]):
        flash("Новый код отправлен на email администратора.", "success")
    else:
        flash("Не удалось отправить код. Проверьте почтовые настройки.", "error")
    return redirect(url_for("verify_admin_login"))


@app.route("/password/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        user = db().execute("SELECT id, email FROM users WHERE email=?", (email,)).fetchone()
        sent = False
        if allow_sensitive_action("password_reset", email) and user:
            reset_url, token_hash = create_password_reset_link(user["id"])
            sent = send_password_reset_email(user["email"], reset_url)
            if not sent:
                db().execute("DELETE FROM password_reset_tokens WHERE token_hash=?", (token_hash,))
                db().commit()
        # Один и тот же ответ не позволяет определить, зарегистрирован ли чужой email.
        flash("Если этот email зарегистрирован, мы отправили ссылку для смены пароля.", "success")
        return redirect(url_for("forgot_password"))
    return render_template("forgot_password.html")


@app.route("/password/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = sha256(token.encode()).hexdigest()
    reset = db().execute("""SELECT * FROM password_reset_tokens
                          WHERE token_hash=? AND used_at IS NULL AND expires_at > ?""", (token_hash, utcnow())).fetchone()
    if not reset:
        flash("Ссылка недействительна или уже использована. Запросите новую.", "error")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if error := password_error(password):
            flash(error, "error")
        elif password != confirm_password:
            flash("Пароли не совпадают.", "error")
        else:
            db().execute("UPDATE users SET password_hash=?, session_version=session_version+1 WHERE id=?", (generate_password_hash(password), reset["user_id"]))
            db().execute("UPDATE password_reset_tokens SET used_at=? WHERE user_id=?", (utcnow(), reset["user_id"]))
            db().commit()
            flash("Пароль изменён. Теперь войдите с новым паролем.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html", token=token)


@app.route("/support", methods=["GET", "POST"])
def support():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").lower().strip()
        subject = request.form.get("subject", "").strip()
        body = request.form.get("message", "").strip()
        if len(name) < 2 or not is_valid_email(email) or len(subject) < 3 or len(body) < 10:
            flash("Заполните имя, корректный email, тему и сообщение не короче 10 символов.", "error")
        elif not allow_sensitive_action("support", email):
            flash("Слишком много обращений. Попробуйте отправить сообщение позже.", "error")
        else:
            sent = send_email(
                SUPPORT_EMAIL,
                f"[ZooLand поддержка] {subject[:120]}",
                f"Имя: {name}\nEmail: {email}\nID пользователя: {user['id'] if user else 'гость'}\n\nСообщение:\n{body}",
                reply_to=email,
            )
            if sent:
                flash("Сообщение отправлено в поддержку. Мы ответим на указанный email.", "success")
                return redirect(url_for("support"))
            flash("Сервис поддержки временно недоступен. Попробуйте позже.", "error")
    return render_template("support.html", support_email=SUPPORT_EMAIL, user=user)


@app.route("/advertising", methods=["GET", "POST"])
def advertising():
    user = current_user()
    if request.method == "POST":
        ad_format = request.form.get("format", "").strip()
        contact_name = request.form.get("contact_name", "").strip()
        company = request.form.get("company", "").strip()
        inn = re.sub(r"\D", "", request.form.get("inn", ""))
        email = request.form.get("email", "").strip().casefold()
        phone = request.form.get("phone", "").strip()
        telegram_raw = request.form.get("telegram", "").strip()
        telegram = normalize_telegram(telegram_raw) if telegram_raw else ""
        target_url = request.form.get("target_url", "").strip()
        category = request.form.get("category", "all").strip()
        cities = request.form.get("cities", "").strip()
        preferred_dates = request.form.get("preferred_dates", "").strip()
        budget = request.form.get("budget", "").strip()
        comment = request.form.get("comment", "").strip()
        consent = request.form.get("personal_data_consent") == "yes"
        budget_options = {"До 10 000 ₽", "10 000–30 000 ₽", "30 000–70 000 ₽", "Более 70 000 ₽", "Нужно рассчитать"}
        errors = []
        if ad_format not in ADVERTISING_FORMATS:
            errors.append("выберите рекламный формат")
        if not 2 <= len(contact_name) <= 80:
            errors.append("укажите контактное лицо")
        if not 2 <= len(company) <= 120:
            errors.append("укажите компанию или бренд")
        if len(inn) not in {10, 12}:
            errors.append("ИНН должен содержать 10 или 12 цифр")
        if not is_valid_email(email):
            errors.append("укажите корректный email")
        if phone and not re.fullmatch(r"[+0-9()\-\s]{7,30}", phone):
            errors.append("проверьте номер телефона")
        if telegram_raw and not telegram:
            errors.append("укажите корректный Telegram username")
        if not valid_advertising_target(target_url):
            errors.append("укажите корректную ссылку на объявление или сайт")
        if category not in ADVERTISING_CATEGORIES:
            errors.append("выберите раздел показа")
        if len(cities) > 300 or len(preferred_dates) > 160:
            errors.append("сократите города или желаемые даты")
        if budget not in budget_options:
            errors.append("выберите предполагаемый бюджет")
        if len(comment) > 2000:
            errors.append("комментарий не должен превышать 2000 символов")
        if not consent:
            errors.append("подтвердите согласие на обработку данных")
        if errors:
            flash("Не удалось отправить заявку: " + "; ".join(errors) + ".", "error")
        elif not allow_sensitive_action("advertising", email):
            flash("Слишком много заявок. Попробуйте отправить её позже.", "error")
        else:
            now = utcnow()
            cursor = db().execute("""INSERT INTO advertising_requests
                (format, contact_name, company, inn, email, phone, telegram, target_url, category, cities,
                 preferred_dates, budget, comment, consent_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ad_format, contact_name, company, inn, email, phone, telegram, target_url, category, cities,
                 preferred_dates, budget, comment, now, now, now))
            db().commit()
            request_id = cursor.lastrowid
            send_email(
                SUPPORT_EMAIL, f"[ZooLand реклама] Новая заявка #{request_id}: {ADVERTISING_FORMATS[ad_format]['title']}",
                f"Компания: {company}\nКонтакт: {contact_name}\nEmail: {email}\nБюджет: {budget}\n\nЗаявка сохранена в панели администратора.",
                reply_to=email,
            )
            flash(f"Заявка № {request_id} принята. Мы проверим тематику и свяжемся с вами для согласования.", "success")
            return redirect(url_for("advertising", sent="1"))
    return render_template("advertising.html", formats=ADVERTISING_FORMATS,
                           categories=ADVERTISING_CATEGORIES, user=user)


@app.route("/advertising/click/<int:request_id>")
def advertising_click(request_id):
    today = advertising_today()
    advertisement = db().execute(
        """SELECT * FROM advertising_requests
             WHERE id=? AND status='active'
               AND (start_date IS NULL OR start_date='' OR start_date<=?)
               AND (end_date IS NULL OR end_date='' OR end_date>=?)""",
        (request_id, today, today),
    ).fetchone()
    target = valid_advertising_target(advertisement["target_url"] if advertisement else "")
    if not advertisement or not target:
        abort(404)
    if advertisement["format"] == "promoted-listing" and not promoted_target_is_public(dict(advertisement)):
        abort(404)
    record_advertising_metric(request_id, "click")
    response = redirect(target, code=302)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/admin/advertising")
@admin_required
def admin_advertising():
    selected_status = request.args.get("status", "").strip()
    if selected_status and selected_status not in ADVERTISING_STATUSES:
        abort(400)
    query, params = "SELECT * FROM advertising_requests", ()
    if selected_status:
        query, params = query + " WHERE status=?", (selected_status,)
    requests = []
    today = advertising_today()
    for row in db().execute(query + " ORDER BY created_at DESC", params).fetchall():
        item = dict(row)
        item["metrics"] = advertising_metrics(item["id"])
        item["safe_target_url"] = valid_advertising_target(item.get("target_url"))
        if item["status"] != "active":
            item["placement_state"] = "Не показывается"
        elif item.get("start_date") and item["start_date"] > today:
            item["placement_state"] = "Запланирована"
        elif item.get("end_date") and item["end_date"] < today:
            item["placement_state"] = "Срок завершён"
        elif not item["safe_target_url"]:
            item["placement_state"] = "Некорректная целевая ссылка"
        elif item["format"] == "promoted-listing" and not promoted_target_is_public(item):
            item["placement_state"] = "Целевое объявление недоступно"
        else:
            item["placement_state"] = "Активна в ротации"
        requests.append(item)
    return render_template("admin_advertising.html", advertising_requests=requests,
                           formats=ADVERTISING_FORMATS, categories=ADVERTISING_CATEGORIES,
                           statuses=ADVERTISING_STATUSES, selected_status=selected_status)


@app.route("/admin/advertising/<int:request_id>", methods=["POST"])
@admin_required
def update_advertising_request(request_id):
    existing_row = db().execute("SELECT * FROM advertising_requests WHERE id=?", (request_id,)).fetchone()
    if not existing_row:
        abort(404)
    existing = dict(existing_row)
    status = request.form.get("status", "")
    note = request.form.get("admin_note", "").strip()[:2000]
    ad_format = request.form.get("format", existing["format"]).strip()
    category = request.form.get("category", existing["category"]).strip()
    target_url = request.form.get("target_url", existing["target_url"]).strip()
    cities = request.form.get("cities", existing.get("cities") or "").strip()
    headline = request.form.get("headline", existing.get("headline") or "").strip()
    ad_text = request.form.get("ad_text", existing.get("ad_text") or "").strip()
    button_label = request.form.get("button_label", existing.get("button_label") or "Подробнее").strip()
    start_date = request.form.get("start_date", existing.get("start_date") or "").strip()
    end_date = request.form.get("end_date", existing.get("end_date") or "").strip()
    if status not in ADVERTISING_STATUSES:
        abort(400)
    errors = []
    if ad_format not in ADVERTISING_FORMATS:
        errors.append("неверный формат")
    if category not in ADVERTISING_CATEGORIES:
        errors.append("неверный раздел")
    if not valid_advertising_target(target_url):
        errors.append("некорректная целевая ссылка")
    if len(cities) > 300:
        errors.append("слишком длинный список городов")
    if len(headline) > 120:
        errors.append("заголовок длиннее 120 символов")
    if len(ad_text) > 320:
        errors.append("текст длиннее 320 символов")
    if not 2 <= len(button_label) <= 40:
        errors.append("текст кнопки должен содержать от 2 до 40 символов")
    if not advertising_date_is_valid(start_date) or not advertising_date_is_valid(end_date):
        errors.append("проверьте даты показа")
    elif start_date and end_date and start_date > end_date:
        errors.append("дата окончания раньше даты начала")
    if status == "active":
        headline = headline or existing["company"]
        ad_text = ad_text or ADVERTISING_FORMATS.get(ad_format, {}).get("summary", "")
        if end_date and end_date < advertising_today():
            errors.append("дата окончания активного размещения уже прошла")
        candidate = dict(existing, format=ad_format, category=category, target_url=target_url)
        if ad_format == "promoted-listing" and not promoted_target_is_public(candidate):
            errors.append("для продвижения нужна ссылка на активное объявление из выбранного раздела")
    if errors:
        flash("Не удалось сохранить размещение: " + "; ".join(errors) + ".", "error")
        return redirect(url_for("admin_advertising"))
    db().execute("""UPDATE advertising_requests
                        SET status=?, admin_note=?, format=?, category=?, target_url=?, cities=?,
                            headline=?, ad_text=?, button_label=?, start_date=?, end_date=?, updated_at=?
                      WHERE id=?""",
                 (status, note, ad_format, category, target_url, cities, headline, ad_text,
                  button_label, start_date or None, end_date or None, utcnow(), request_id))
    db().commit()
    flash(f"Статус рекламной заявки № {request_id} обновлён.", "success")
    return redirect(url_for("admin_advertising"))


@app.route("/vacancies")
def vacancies():
    return render_template("vacancies.html", vacancies=VACANCIES)


@app.route("/vacancies/<slug>", methods=["GET", "POST"])
def vacancy_detail(slug):
    vacancy = VACANCIES.get(slug)
    if not vacancy:
        abort(404)
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().casefold()
        phone = request.form.get("phone", "").strip()
        telegram_raw = request.form.get("telegram", "").strip()
        telegram = normalize_telegram(telegram_raw) if telegram_raw else ""
        experience = request.form.get("experience", "").strip()
        cover_letter = request.form.get("cover_letter", "").strip()
        resume_url = request.form.get("resume_url", "").strip()
        consent = request.form.get("personal_data_consent") == "yes"
        resume_parts = urlparse(resume_url) if resume_url else None
        errors = []
        if not 2 <= len(name) <= 80:
            errors.append("укажите имя от 2 до 80 символов")
        if not is_valid_email(email):
            errors.append("укажите корректный email")
        if phone and not re.fullmatch(r"[+0-9()\-\s]{7,30}", phone):
            errors.append("проверьте номер телефона")
        if telegram_raw and not telegram:
            errors.append("укажите корректный Telegram username")
        if not 10 <= len(experience) <= 1500:
            errors.append("расскажите об опыте — от 10 до 1500 символов")
        if not 10 <= len(cover_letter) <= 2000:
            errors.append("напишите несколько слов о себе — от 10 до 2000 символов")
        if resume_url and (len(resume_url) > 500 or resume_parts.scheme not in {"http", "https"} or not resume_parts.netloc):
            errors.append("ссылка на резюме должна начинаться с http:// или https://")
        if not consent:
            errors.append("подтвердите согласие на обработку данных для рассмотрения отклика")
        if errors:
            flash("Не удалось отправить отклик: " + "; ".join(errors) + ".", "error")
        elif not allow_sensitive_action("career", email):
            flash("Слишком много откликов. Попробуйте отправить заявку позже.", "error")
        else:
            now = utcnow()
            cursor = db().execute("""INSERT INTO job_applications
                (vacancy_slug, name, email, phone, telegram, experience, cover_letter, resume_url, consent_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (slug, name, email, phone, telegram, experience, cover_letter, resume_url, now, now, now))
            db().commit()
            application_id = cursor.lastrowid
            send_email(
                SUPPORT_EMAIL, f"[ZooLand вакансии] Новый отклик #{application_id}: {vacancy['title']}",
                f"Кандидат: {name}\nEmail: {email}\nТелефон: {phone or 'не указан'}\nTelegram: {telegram or 'не указан'}\n\nОтклик сохранён в панели администратора.",
                reply_to=email,
            )
            flash(f"Спасибо! Заявка № {application_id} принята. Мы свяжемся с вами после рассмотрения.", "success")
            return redirect(url_for("vacancy_detail", slug=slug, sent="1"))
    return render_template("vacancy_detail.html", slug=slug, vacancy=vacancy, user=user)


@app.route("/admin/job-applications")
@admin_required
def admin_job_applications():
    selected_status = request.args.get("status", "").strip()
    if selected_status and selected_status not in JOB_APPLICATION_STATUSES:
        abort(400)
    query, params = "SELECT * FROM job_applications", ()
    if selected_status:
        query, params = query + " WHERE status=?", (selected_status,)
    applications = db().execute(query + " ORDER BY created_at DESC", params).fetchall()
    return render_template("admin_job_applications.html", applications=applications,
                           vacancies=VACANCIES, statuses=JOB_APPLICATION_STATUSES,
                           selected_status=selected_status)


@app.route("/admin/job-applications/<int:application_id>", methods=["POST"])
@admin_required
def update_job_application(application_id):
    status = request.form.get("status", "")
    note = request.form.get("admin_note", "").strip()[:2000]
    if status not in JOB_APPLICATION_STATUSES:
        abort(400)
    if not db().execute("SELECT 1 FROM job_applications WHERE id=?", (application_id,)).fetchone():
        abort(404)
    db().execute("UPDATE job_applications SET status=?, admin_note=?, updated_at=? WHERE id=?",
                 (status, note, utcnow(), application_id))
    db().commit()
    flash(f"Статус заявки № {application_id} обновлён.", "success")
    return redirect(url_for("admin_job_applications"))




@app.route("/logout", methods=["POST"])
def logout():
    session.clear(); return redirect(url_for("index"))


@app.route("/account")
@login_required
def account():
    user = current_user()
    # Кабинет объединяет все виды объявлений: животных, услуги и товары.
    active, pending, rejected, completed, archived = [], [], [], [], []
    revisions = {
        (row["listing_type"], row["listing_id"]): row
        for row in db().execute("SELECT * FROM listing_revisions WHERE owner_id=?", (user["id"],)).fetchall()
    }
    for listing_type, table, title in (("animal", "animals", "breed"), ("service", "services", "title"), ("food", "food", "title")):
        rows = db().execute(f"SELECT * FROM {table} WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
        for row in rows:
            item = dict(row)
            item["listing_type"] = listing_type
            item["listing_title"] = item.get(title) or "Без названия"
            revision = revisions.get((listing_type, item["id"]))
            if revision:
                item["revision"] = dict(revision)
                item["revision_status"] = revision["status"]
                item["revision_reason"] = revision["moderation_reason"]
            if item["status"] == "active":
                if item.get("deal_status") in FINAL_OUTCOME_STATUSES:
                    completed.append(item)
                else:
                    item["metrics"] = listing_metrics(listing_type, item, user["id"])
                    active.append(item)
            elif item["status"] == "pending":
                pending.append(item)
            elif item["status"] == "rejected":
                rejected.append(item)
            else:
                archived.append(item)
    active.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    pending.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    rejected.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    completed.sort(key=lambda item: item.get("outcome_at") or item.get("created_at") or "", reverse=True)
    archived.sort(key=lambda item: item.get("archived_at") or item.get("created_at") or "", reverse=True)
    account_metrics = {
        "views_7": sum(item["metrics"]["views_7"] for item in active),
        "favorites_now": sum(item["metrics"]["favorites_now"] for item in active),
        "contacts_7": sum(item["metrics"]["contacts_7"] for item in active),
        "messages_7": sum(item["metrics"]["messages_7"] for item in active),
    }
    saved_searches = db().execute("SELECT * FROM saved_searches WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    return render_template("account.html", active=active, pending=pending, rejected=rejected, completed=completed,
                           archived=archived, account_metrics=account_metrics, saved_searches=saved_searches, tab="listings")


@app.route("/settings")
@login_required
def settings():
    pending_change = db().execute("SELECT new_email, expires_at FROM email_change_codes WHERE user_id=? AND used_at IS NULL ORDER BY id DESC LIMIT 1", (current_user()["id"],)).fetchone()
    return render_template("settings.html", tab="settings", pending_email_change=pending_change)


@app.route("/user/<int:user_id>")
def public_profile(user_id):
    owner = db().execute("SELECT id, name, city, about, avatar, telegram, created_at FROM users WHERE id=?", (user_id,)).fetchone()
    if not owner:
        abort(404)
    listings = []
    for listing_type, table, title in (("animal", "animals", "breed"), ("service", "services", "title"), ("food", "food", "title")):
        for row in db().execute(f"SELECT * FROM {table} WHERE user_id=? AND status='active' AND {PUBLIC_OUTCOME_SQL} ORDER BY created_at DESC", (user_id,)).fetchall():
            item = dict(row); item["kind"] = listing_type; item["listing_title"] = item[title]; listings.append(item)
    review_context = owner_review_context(owner, user=current_user())
    return render_template("profile.html", profile=owner,
                           listings=sorted(listings, key=lambda item: item["created_at"], reverse=True),
                           **review_context)


@app.route("/searches/save", methods=["POST"])
@login_required
def save_search():
    filters = {key: request.form.get(key, "").strip() for key in ("q", "city", "type", "kind", "price_min", "price_max", "age_min", "age_max", "sex", "breed", "deal", "only_photo", "vaccinated", "delivery")}
    if not any(filters.values()):
        flash("Выберите хотя бы один параметр, прежде чем сохранять поиск.", "error")
        return redirect(url_for("index"))
    name = request.form.get("name", "").strip() or filters.get("q") or "Мой поиск"
    db().execute("INSERT INTO saved_searches (user_id, name, filters_json, created_at) VALUES (?, ?, ?, ?)",
                 (current_user()["id"], name[:80], json.dumps(filters, ensure_ascii=False), utcnow()))
    db().commit()
    flash("Поиск сохранён. Мы напишем на email, когда появится подходящее объявление.", "success")
    return redirect(url_for("index", **{key: value for key, value in filters.items() if value}))


@app.route("/searches/<int:search_id>/delete", methods=["POST"])
@login_required
def delete_saved_search(search_id):
    db().execute("DELETE FROM saved_searches WHERE id=? AND user_id=?", (search_id, current_user()["id"])); db().commit()
    flash("Сохранённый поиск удалён.", "success")
    return redirect(url_for("account"))


@app.route("/report/<listing_type>/<int:listing_id>", methods=["POST"])
@login_required
def report_listing(listing_type, listing_id):
    table, item = admin_get_listing(listing_type, listing_id)
    if item["user_id"] == current_user()["id"]:
        abort(403)
    reason = request.form.get("reason", "")
    if reason not in REPORT_REASONS:
        flash("Выберите причину жалобы.", "error")
    else:
        db().execute("INSERT OR REPLACE INTO reports (reporter_id, listing_type, listing_id, reason, comment, status, created_at, resolved_at) VALUES (?, ?, ?, ?, ?, 'open', ?, NULL)",
                     (current_user()["id"], listing_type, listing_id, reason, request.form.get("comment", "").strip()[:500], utcnow()))
        db().commit(); flash("Жалоба отправлена на проверку.", "success")
    return redirect(url_for("index"))


@app.route("/admin/reports")
@admin_required
def admin_reports():
    reports = []
    stale_reports = []
    rows = db().execute("SELECT r.*, u.name AS reporter_name FROM reports r JOIN users u ON u.id=r.reporter_id WHERE r.status='open' ORDER BY r.created_at DESC").fetchall()
    for row in rows:
        report = dict(row)
        listing_data = LISTING_TABLES.get(report["listing_type"])
        if not listing_data:
            stale_reports.append(report["id"])
            continue
        table, title_column, _days = listing_data
        item = db().execute(f"SELECT * FROM {table} WHERE id=?", (report["listing_id"],)).fetchone()
        if not item:
            stale_reports.append(report["id"])
            continue
        report["listing_title"] = item[title_column] or "Без названия"
        report["listing_status"] = item["status"]
        reports.append(report)
    if stale_reports:
        db().executemany("UPDATE reports SET status='resolved', resolved_at=? WHERE id=?", [(utcnow(), report_id) for report_id in stale_reports])
        db().commit()
    return render_template("admin_reports.html", reports=reports)


@app.route("/admin/reports/<int:report_id>/resolve", methods=["POST"])
@admin_required
def resolve_report(report_id):
    db().execute("UPDATE reports SET status='resolved', resolved_at=? WHERE id=?", (utcnow(), report_id)); db().commit()
    flash("Жалоба отмечена как обработанная.", "success")
    return redirect(url_for("admin_reports"))


@app.route("/admin/users")
@admin_required
def admin_users():
    """Список пользователей для владельца сайта без приватных контактов."""
    users = db().execute("""SELECT u.id, u.name, u.email, u.city, u.gender, u.birth_date, u.about, u.telegram, u.created_at,
                          (SELECT COUNT(*) FROM animals a WHERE a.user_id=u.id) AS animals_count,
                          (SELECT COUNT(*) FROM services s WHERE s.user_id=u.id) AS services_count,
                          (SELECT COUNT(*) FROM food f WHERE f.user_id=u.id) AS food_count
                          FROM users u ORDER BY u.id DESC""").fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/password-reset", methods=["POST"])
@admin_required
def admin_send_password_reset(user_id):
    user = db().execute("SELECT id, email FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        abort(404)
    if not allow_sensitive_action("password_reset", user["email"]):
        flash("Слишком много ссылок восстановления. Попробуйте позже.", "error")
        return redirect(url_for("admin_users"))
    reset_url, token_hash = create_password_reset_link(user["id"])
    if send_password_reset_email(user["email"], reset_url):
        flash("Ссылка для восстановления пароля отправлена пользователю.", "success")
    else:
        db().execute("DELETE FROM password_reset_tokens WHERE token_hash=?", (token_hash,))
        db().commit()
        flash("Не удалось отправить письмо. Проверьте настройки почты поддержки.", "error")
    return redirect(url_for("admin_users"))


def admin_listing_rows(user_id=None, only_pending=False):
    items = []
    for listing_type, (table, title_column, _days) in LISTING_TABLES.items():
        conditions, params = [], []
        if user_id is not None:
            conditions.append("user_id=?")
            params.append(user_id)
        if only_pending:
            conditions.append("status='pending'")
        query = f"SELECT * FROM {table}" + (" WHERE " + " AND ".join(conditions) if conditions else "") + " ORDER BY created_at DESC"
        for row in db().execute(query, params).fetchall():
            item = dict(row)
            item["listing_type"] = listing_type
            item["listing_title"] = item.get(title_column) or "Без названия"
            items.append(item)
    # Изменения к уже опубликованным карточкам живут в отдельной очереди —
    # показываем модератору именно предложенную версию, а не старый текст.
    if only_pending:
        revision_rows = db().execute(
            "SELECT * FROM listing_revisions WHERE status='pending' ORDER BY submitted_at DESC"
        ).fetchall()
        for revision in revision_rows:
            listing_type = revision["listing_type"]
            table, title_column, _days = LISTING_TABLES[listing_type]
            base = db().execute(
                f"SELECT * FROM {table} WHERE id=? AND user_id=?",
                (revision["listing_id"], revision["owner_id"]),
            ).fetchone()
            if not base:
                db().execute("DELETE FROM listing_revisions WHERE id=?", (revision["id"],))
                db().commit()
                continue
            item = revision_form_item(listing_type, base, revision)
            item["listing_type"] = listing_type
            item["listing_title"] = item.get(title_column) or "Без названия"
            item["status"] = "pending"
            item["is_revision"] = True
            item["revision_id"] = revision["id"]
            item["live_status"] = base["status"]
            item["queued_at"] = revision["submitted_at"]
            items.append(item)
    return sorted(items, key=lambda item: item.get("queued_at") or item.get("created_at") or "", reverse=True)


@app.route("/admin/moderation")
@admin_required
def admin_moderation():
    return render_template("admin_listings.html", listings=admin_listing_rows(only_pending=True), title="Модерация объявлений", owner=None)


@app.route("/admin/users/<int:user_id>/listings")
@admin_required
def admin_user_listings(user_id):
    owner = db().execute("SELECT id, name FROM users WHERE id=?", (user_id,)).fetchone()
    if not owner:
        abort(404)
    return render_template("admin_listings.html", listings=admin_listing_rows(user_id=user_id), title=f"Объявления пользователя: {owner['name']}", owner=owner)


def admin_get_listing(listing_type, listing_id):
    if listing_type not in LISTING_TABLES:
        abort(404)
    table = LISTING_TABLES[listing_type][0]
    item = db().execute(f"SELECT * FROM {table} WHERE id=?", (listing_id,)).fetchone()
    if not item:
        abort(404)
    return table, item


def moderation_reason_from_form():
    """Возвращает безопасную и понятную владельцу причину решения модератора."""
    reason = request.form.get("reason", "")
    return reason if reason in MODERATION_REASONS else "Нарушение правил площадки"


def resolve_listing_reports(listing_type, listing_id):
    """После решения модератора закрывает все открытые жалобы на объявление."""
    db().execute("UPDATE reports SET status='resolved', resolved_at=? WHERE listing_type=? AND listing_id=? AND status='open'",
                 (utcnow(), listing_type, listing_id))


def listing_edit_url(listing_type, listing_id):
    endpoint = {"animal": "edit_animal", "service": "edit_service", "food": "edit_food"}[listing_type]
    return public_url(endpoint, **{f"{listing_type}_id": listing_id})


def send_moderation_email(listing_type, item, subject, action_text, reason, edit_url=None):
    owner = db().execute("SELECT email FROM users WHERE id=?", (item["user_id"],)).fetchone()
    if not owner:
        return
    body = f"Объявление «{listing_title_from_row(listing_type, item)}» {action_text}.\nПричина: {reason}."
    if edit_url:
        body += f"\n\nИсправьте информацию и отправьте объявление на повторную модерацию:\n{edit_url}"
    send_email(owner["email"], subject, body)


@app.route("/admin/listings/<listing_type>/<int:listing_id>/preview")
@admin_required
def admin_preview_listing(listing_type, listing_id):
    _table, item = admin_get_listing(listing_type, listing_id)
    revision = None
    revision_id = request.args.get("revision_id", type=int)
    if revision_id:
        revision = db().execute(
            "SELECT * FROM listing_revisions WHERE id=? AND listing_type=? AND listing_id=?",
            (revision_id, listing_type, listing_id),
        ).fetchone()
        if not revision:
            abort(404)
        item = revision_form_item(listing_type, item, revision)
        item["status"] = revision["status"]
        item["is_revision"] = True
        item["revision_id"] = revision["id"]
    owner = db().execute("SELECT id, name, email, phone, telegram FROM users WHERE id=?", (item["user_id"],)).fetchone()
    title_field = LISTING_TABLES[listing_type][1]
    return render_template("admin_preview.html", item=item, listing_type=listing_type,
                           title=item[title_field] or "Без названия", owner=owner, revision=revision)


@app.route("/admin/revisions/<int:revision_id>/approve", methods=["POST"])
@admin_required
def admin_approve_revision(revision_id):
    revision, _item, updated = apply_listing_revision(revision_id)
    owner = db().execute("SELECT email FROM users WHERE id=?", (revision["owner_id"],)).fetchone()
    if owner:
        send_email(
            owner["email"],
            "ZooLand: изменения одобрены",
            f"Изменения в объявлении «{listing_title_from_row(revision['listing_type'], updated)}» прошли модерацию и опубликованы.",
        )
    flash("Изменения одобрены: опубликованная карточка обновлена.", "success")
    return redirect(url_for("admin_moderation"))


@app.route("/admin/revisions/<int:revision_id>/reject", methods=["POST"])
@admin_required
def admin_reject_revision(revision_id):
    revision = db().execute("SELECT * FROM listing_revisions WHERE id=?", (revision_id,)).fetchone()
    if not revision or revision["status"] != "pending":
        abort(404)
    table = LISTING_TABLES[revision["listing_type"]][0]
    item = db().execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (revision["listing_id"], revision["owner_id"])).fetchone()
    if not item:
        abort(404)
    reason = moderation_reason_from_form()
    db().execute(
        "UPDATE listing_revisions SET status='rejected', moderation_reason=?, updated_at=? WHERE id=?",
        (reason, utcnow(), revision_id),
    )
    db().commit()
    proposed = revision_form_item(revision["listing_type"], item, revision)
    send_moderation_email(
        revision["listing_type"], proposed,
        "ZooLand: изменения требуют доработки",
        "изменения возвращены на доработку; прежняя версия объявления остаётся опубликованной",
        reason,
        listing_edit_url(revision["listing_type"], item["id"]),
    )
    flash("Изменения возвращены владельцу, опубликованная версия не затронута.", "success")
    return redirect(url_for("admin_moderation"))


@app.route("/admin/listings/<listing_type>/<int:listing_id>/approve", methods=["POST"])
@admin_required
def admin_approve_listing(listing_type, listing_id):
    table, item = admin_get_listing(listing_type, listing_id)
    days = LISTING_TABLES[listing_type][2]
    archived_reset = ", archived_at=NULL" if listing_type == "animal" else ""
    db().execute(f"UPDATE {table} SET status='active', created_at=?, expires_at=?{archived_reset} WHERE id=? AND status='pending'",
                 (utcnow(), iso_after_days(days), listing_id))
    db().commit()
    owner = db().execute("SELECT email FROM users WHERE id=?", (item["user_id"],)).fetchone()
    if owner:
        send_email(owner["email"], "ZooLand: объявление одобрено", f"Ваше объявление «{listing_title_from_row(listing_type, item)}» прошло модерацию и опубликовано.")
    notify_saved_searches(listing_type, listing_id)
    flash("Объявление одобрено и опубликовано.", "success")
    return redirect(url_for("admin_moderation"))


@app.route("/admin/listings/<listing_type>/<int:listing_id>/reject", methods=["POST"])
@admin_required
def admin_reject_listing(listing_type, listing_id):
    table, item = admin_get_listing(listing_type, listing_id)
    reason = moderation_reason_from_form()
    db().execute(f"UPDATE {table} SET status='rejected', moderation_reason=? WHERE id=? AND status='pending'", (reason, listing_id))
    resolve_listing_reports(listing_type, listing_id)
    db().commit()
    send_moderation_email(listing_type, item, "ZooLand: объявление требует доработки", "возвращено на доработку", reason,
                          listing_edit_url(listing_type, listing_id))
    flash("Объявление отклонено и не будет показано в каталоге.", "success")
    return redirect(url_for("admin_moderation"))


@app.route("/admin/listings/<listing_type>/<int:listing_id>/return-for-revision", methods=["POST"])
@admin_required
def return_listing_for_revision(listing_type, listing_id):
    """Снимает опубликованное или ожидающее объявление после жалобы и возвращает владельцу."""
    table, item = admin_get_listing(listing_type, listing_id)
    if item["status"] not in {"active", "pending", "rejected"}:
        abort(400)
    reason = moderation_reason_from_form()
    db().execute(f"UPDATE {table} SET status='rejected', moderation_reason=? WHERE id=?", (reason, listing_id))
    revision_photos = discard_listing_revision(listing_type, listing_id, item["photo"])
    resolve_listing_reports(listing_type, listing_id)
    db().commit()
    delete_photos(revision_photos)
    send_moderation_email(listing_type, item, "ZooLand: объявление возвращено на доработку", "снято с публикации и возвращено на доработку", reason,
                          listing_edit_url(listing_type, listing_id))
    flash("Объявление возвращено владельцу на доработку, причина отправлена на email.", "success")
    return redirect(url_for("admin_reports") if request.form.get("return_to") == "reports" else url_for("admin_moderation"))


@app.route("/admin/listings/<listing_type>/<int:listing_id>/delete", methods=["POST"])
@admin_required
def admin_delete_listing(listing_type, listing_id):
    table, item = admin_get_listing(listing_type, listing_id)
    reason = moderation_reason_from_form()
    send_moderation_email(listing_type, item, "ZooLand: объявление удалено", "удалено с ZooLand", reason)
    revision_photos = discard_listing_revision(listing_type, listing_id, item["photo"])
    db().execute("DELETE FROM listing_daily_metrics WHERE listing_type=? AND listing_id=?", (listing_type, listing_id))
    db().execute(f"DELETE FROM {table} WHERE id=?", (listing_id,))
    resolve_listing_reports(listing_type, listing_id)
    db().commit()
    delete_photos(item["photo"])
    delete_photos(revision_photos)
    flash("Объявление удалено, причина отправлена владельцу на email.", "success")
    return redirect(url_for("admin_reports") if request.form.get("return_to") == "reports" else url_for("admin_moderation"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    if user_id == current_user()["id"]:
        abort(400)
    owner = db().execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not owner:
        abort(404)
    delete_user_and_associated_data(user_id)
    flash("Пользователь и все связанные с ним данные удалены.", "success")
    return redirect(url_for("admin_users"))


@app.route("/settings/profile", methods=["POST"])
@login_required
def settings_profile():
    user = current_user()
    name = request.form.get("name", "").strip()
    city = request.form.get("city", "").strip()
    gender = request.form.get("gender", "").strip()
    birth_date = request.form.get("birth_date", "").strip()
    about = request.form.get("about", "").strip()
    telegram_raw = request.form.get("telegram", "").strip()
    telegram = normalize_telegram(telegram_raw) if telegram_raw else None
    if len(name) < 2:
        flash("Имя должно быть не короче 2 символов.", "error")
        return redirect(url_for("settings"))
    if telegram_raw and not telegram:
        flash("Укажите Telegram в формате @username или https://t.me/username.", "error")
        return redirect(url_for("settings"))
    avatar = user["avatar"]
    file = request.files.get("avatar")
    if file and file.filename:
        try:
            if not allow_sensitive_action("upload", str(user["id"])):
                raise ImageSafetyError("Слишком много загрузок. Попробуйте снова немного позже.")
            filename = sanitize_image_upload(file)
        except ImageSafetyError as error:
            flash(str(error), "error")
            return redirect(url_for("settings"))
        if avatar:
            try:
                os.remove(os.path.join(app.config["UPLOAD_FOLDER"], avatar))
            except OSError:
                pass
        avatar = filename
    db().execute("UPDATE users SET name=?, city=?, gender=?, birth_date=?, about=?, avatar=?, telegram=? WHERE id=?",
                 (name, city or None, gender or None, birth_date or None, about or None, avatar, telegram, user["id"]))
    db().commit()
    flash("Профиль обновлён.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/email", methods=["POST"])
@login_required
def settings_email():
    user = current_user()
    email = request.form.get("email", "").lower().strip()
    current_password = request.form.get("current_password", "")
    if not is_valid_email(email):
        flash("Укажите корректный email на известном почтовом сервисе.", "error")
    elif db().execute("SELECT 1 FROM users WHERE email=? AND id!=?", (email, user["id"])).fetchone():
        flash("Этот email уже занят другим аккаунтом.", "error")
    elif email == user["email"]:
        flash("Это уже ваш текущий email.", "error")
    else:
        secured_user = db().execute("SELECT id, email, password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not check_password_hash(secured_user["password_hash"], current_password):
            flash("Введите текущий пароль, чтобы изменить email.", "error")
        elif not allow_sensitive_action("verify_resend", str(user["id"])):
            flash("Слишком много запросов кода. Попробуйте позже.", "error")
        elif create_email_change_code(user["id"], email):
            db().execute("UPDATE users SET pending_email=? WHERE id=?", (email, user["id"]))
            db().commit()
            send_email(user["email"], "ZooLand: запрос на смену email", f"Запрошена смена email аккаунта ZooLand на {email}. Если это не вы, немедленно смените пароль.")
            flash("Код подтверждения отправлен на новый email.", "success")
            return redirect(url_for("verify_email_change"))
        else:
            flash("Не удалось отправить код. Попробуйте позже.", "error")
    return redirect(url_for("settings"))


@app.route("/settings/email/verify", methods=["GET", "POST"])
@login_required
def verify_email_change():
    user = current_user()
    row = db().execute("SELECT * FROM email_change_codes WHERE user_id=? AND used_at IS NULL ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    if not row or row["expires_at"] <= utcnow() or row["attempts"] >= 5:
        flash("Код смены email истёк. Запросите новый в настройках.", "error")
        return redirect(url_for("settings"))
    if request.method == "POST":
        code = re.sub(r"\D", "", request.form.get("code", ""))
        if not hmac.compare_digest(short_code_hash(code), row["code_hash"]):
            db().execute("UPDATE email_change_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
            db().commit()
            flash("Неверный код.", "error")
        elif db().execute("SELECT 1 FROM users WHERE email=? AND id!=?", (row["new_email"], user["id"])).fetchone():
            flash("Этот email уже занят другим аккаунтом.", "error")
        else:
            old_email = user["email"]
            db().execute("UPDATE email_change_codes SET used_at=? WHERE id=?", (utcnow(), row["id"]))
            db().execute("UPDATE users SET email=?, pending_email=NULL, email_verified=1, session_version=session_version+1 WHERE id=?", (row["new_email"], user["id"]))
            db().commit()
            updated_user = db().execute("SELECT id, session_version FROM users WHERE id=?", (user["id"],)).fetchone()
            establish_authenticated_session(updated_user)
            send_email(old_email, "ZooLand: email изменён", f"Email вашего аккаунта ZooLand изменён на {row['new_email']}. Если это были не вы, срочно обратитесь в поддержку.")
            flash("Новый email подтверждён. Все другие сессии завершены.", "success")
            return redirect(url_for("settings"))
    return render_template("verify_email_change.html", email=row["new_email"])


@app.route("/settings/phone", methods=["POST"])
@login_required
def settings_phone():
    user = current_user()
    phone = normalize_international_phone(
        request.form.get("phone"),
        request.form.get("phone_country_custom") or request.form.get("phone_country"),
    )
    if not phone:
        flash("Укажите корректный международный номер телефона.", "error")
    elif db().execute("SELECT 1 FROM users WHERE phone=? AND id!=?", (phone, user["id"])).fetchone():
        flash("Этот номер уже привязан к другому аккаунту.", "error")
    else:
        db().execute("UPDATE users SET phone=? WHERE id=?", (phone, user["id"])); db().commit()
        flash("Номер телефона обновлён.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/delete", methods=["POST"])
@login_required
def settings_delete():
    user = current_user()
    secured_user = db().execute(
        "SELECT password_hash FROM users WHERE id=?", (user["id"],)
    ).fetchone()
    if not secured_user or not check_password_hash(
        secured_user["password_hash"], request.form.get("current_password", "")
    ):
        flash("Введите текущий пароль, чтобы удалить аккаунт.", "error")
        return redirect(url_for("settings"))
    delete_user_and_associated_data(user["id"])
    session.clear()
    flash("Аккаунт и все связанные с ним данные удалены без возможности восстановления.", "success")
    return redirect(url_for("index"))


def owned_animal(animal_id):
    animal = db().execute("SELECT * FROM animals WHERE id=?", (animal_id,)).fetchone()
    user = current_user()
    if not animal or not user or animal["user_id"] != user["id"]:
        abort(404)
    return animal


@app.route("/edit/<int:animal_id>")
@login_required
def edit_animal(animal_id):
    animal = owned_animal(animal_id)
    revision = get_listing_revision("animal", animal_id)
    return render_template("edit.html", animal=revision_form_item("animal", animal, revision), revision=revision)


@app.route("/update/<int:animal_id>", methods=["POST"])
@login_required
def update_animal(animal_id):
    animal, data = owned_animal(animal_id), request.form
    revision = get_listing_revision("animal", animal_id)
    revision_values = revision_payload(revision)
    current_phone = revision_values.get("contacts", animal["contacts"]) if revision else animal["contacts"]
    validation_errors = validate_animal_listing(data, current_phone=current_phone)
    if validation_errors:
        return animal_validation_redirect(validation_errors, animal_id=animal_id)
    phone = current_phone if data.get("phone_source") == "current" else normalize_international_phone(data.get("contacts"), data.get("phone_country_custom") or data.get("phone_country"))
    photos = revision_values.get("photo", animal["photo"] or "") if revision else animal["photo"] or ""
    try:
        new_photos = save_photos(request.files.getlist("photos"))
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("edit_animal", animal_id=animal_id))
    if new_photos:
        photos = ",".join(new_photos)
    deal_type = data.get("deal_type", "sale")
    if deal_type not in DEAL_TYPES:
        deal_type = "sale"
    sex = data.get("sex", "") if data.get("sex", "") in ("male", "female") else None
    payload = {
        "type": data["type"], "breed": data["breed"].strip(), "age": nonnegative_int(data["age"]),
        "price": nonnegative_int(data["price"]), "city": data["city"].strip(),
        "description": data.get("description", ""), "contacts": phone, "photo": photos,
        "deal_type": deal_type, "contact_methods": listing_contact_methods(data), "sex": sex,
        "birth_date": data.get("birth_date") or None,
        "vaccinated": checkbox_value(data, "vaccinated"), "vet_passport": checkbox_value(data, "vet_passport"),
        "pedigree": checkbox_value(data, "pedigree"), "sterilized": checkbox_value(data, "sterilized"),
        "delivery": checkbox_value(data, "delivery"),
    }
    if animal["status"] == "active":
        if not revision and revision_payload_equal(payload, {field: animal[field] for field in LISTING_REVISION_FIELDS["animal"]}):
            flash("Изменений нет — опубликованная версия осталась прежней.", "success")
            return redirect(url_for("account"))
        if revision and revision["status"] == "pending" and revision_payload_equal(payload, revision_values):
            if new_photos:
                delete_photos(",".join(new_photos))
            flash("Эта версия уже находится на проверке.", "success")
            return redirect(url_for("account"))
        obsolete_photos = stage_listing_revision("animal", animal, payload)
        db().commit()
        delete_photos(obsolete_photos)
        flash("Изменения отправлены на повторную модерацию. Пока посетители видят предыдущую опубликованную версию.", "success")
        return redirect(url_for("account"))
    old_photo = animal["photo"] or ""
    db().execute(
        """UPDATE animals SET type=?, breed=?, age=?, price=?, city=?, description=?, contacts=?, photo=?, deal_type=?,
                              contact_methods=?, sex=?, birth_date=?, vaccinated=?, vet_passport=?, pedigree=?, sterilized=?, delivery=?,
                              status='pending', moderation_reason=NULL, deal_status='open', outcome_at=NULL
             WHERE id=?""",
        (*[payload[field] for field in LISTING_REVISION_FIELDS["animal"]], animal_id),
    )
    db().commit()
    if new_photos:
        delete_photos(old_photo)
    flash("Объявление отправлено на повторную модерацию.", "success")
    return redirect(url_for("account") if current_user() else url_for("index"))


@app.route("/archive/<int:animal_id>", methods=["POST"])
@login_required
def archive_animal(animal_id):
    animal = owned_animal(animal_id)
    if animal["status"] != "active": abort(400)
    revision_photos = discard_listing_revision("animal", animal_id, animal["photo"])
    db().execute("UPDATE animals SET status='archived', archived_at=? WHERE id=?", (utcnow(), animal_id)); db().commit()
    delete_photos(revision_photos)
    flash("Объявление убрано в архив.", "success")
    return redirect(url_for("account"))


@app.route("/extend/<int:animal_id>", methods=["POST"])
@login_required
def extend_animal(animal_id):
    animal = owned_animal(animal_id)
    if animal["status"] != "active": abort(400)
    db().execute("UPDATE animals SET expires_at=? WHERE id=?", (iso_after_days(FREE_DAYS), animal_id)); db().commit()
    flash("Объявление бесплатно продлено на 7 дней.", "success")
    return redirect(url_for("account"))


@app.route("/reactivate/<int:animal_id>", methods=["POST"])
@login_required
def reactivate(animal_id):
    animal = owned_animal(animal_id)
    if animal["status"] != "archived": abort(400)
    db().execute("UPDATE animals SET status='active', expires_at=?, archived_at=NULL WHERE id=?", (iso_after_days(FREE_DAYS), animal_id)); db().commit()
    flash("Объявление снова опубликовано бесплатно на 7 дней.", "success")
    return redirect(url_for("account"))


@app.route("/delete/<int:animal_id>", methods=["POST"])
@login_required
def delete_animal(animal_id):
    animal = owned_animal(animal_id)
    revision_photos = discard_listing_revision("animal", animal_id, animal["photo"])
    db().execute("DELETE FROM listing_daily_metrics WHERE listing_type='animal' AND listing_id=?", (animal_id,))
    db().execute("DELETE FROM animals WHERE id=?", (animal_id,)); db().commit()
    delete_photos(animal["photo"])
    delete_photos(revision_photos)
    return redirect(url_for("account") if current_user() else url_for("index"))


# ---------- Зоопитание и сопутствующие товары ----------

@app.route("/food")
def food_list():
    return product_list(False)


@app.route("/accessories")
def accessories_list():
    return product_list(True)


def product_list(accessories):
    """Общий каталог для питания и аксессуаров."""
    category, city, search = (request.args.get(key, "").strip() for key in ("category", "city", "q"))
    categories = ACCESSORY_TYPES if accessories else FOOD_TYPES
    query, conditions, params = "SELECT * FROM food", ["status='active'", PUBLIC_OUTCOME_SQL], []
    placeholders = ",".join("?" for _ in categories)
    conditions.append(f"category IN ({placeholders})")
    params.extend(categories)
    if category in categories:
        conditions.append("category=?")
        params.append(category)
    if city:
        conditions.append("city=?")
        params.append(city)
    query += " WHERE " + " AND ".join(conditions) + " ORDER BY id DESC"
    food = [dict(item) for item in db().execute(query, params).fetchall()]
    if search:
        search_lower = search.casefold()
        food = [f for f in food if search_lower in (f["title"] or "").casefold() or search_lower in (f["description"] or "").casefold()]
    page_category = "accessories" if accessories else "food"
    food = attach_owner_telegrams(food)
    food = apply_promoted_listings(food, page_category, fixed_listing_type="food", city=city)
    category_ads = active_advertisements("category-banner", page_category, city=city)
    return render_template("food.html", food=food, cities=RUSSIAN_CITIES,
                           selected_category=category, selected_city=city, search=search,
                           catalog_categories=categories, accessories=accessories,
                           category_ads=category_ads)


@app.route("/food/<int:food_id>")
def food_detail(food_id):
    item = db().execute("SELECT * FROM food WHERE id=? AND status='active'", (food_id,)).fetchone()
    if not item:
        abort(404)
    user = current_user()
    views_today, views_total = record_listing_view("food", item, user)
    owner = None
    my_item = False
    if item["user_id"]:
        # Контакты товара принадлежат зафиксированной версии объявления.
        owner = db().execute("SELECT id, name FROM users WHERE id=?", (item["user_id"],)).fetchone()
        my_item = bool(user and user["id"] == item["user_id"])
    review_context = owner_review_context(owner, "food", food_id, user)
    outcome = item["deal_status"] or "open"
    return render_template("food_item.html", item=item, owner=owner, my_item=my_item,
                           views_today=views_today, views_total=views_total,
                           outcome=outcome, is_final_outcome=outcome in FINAL_OUTCOME_STATUSES,
                           listing_type="food", listing_id=food_id, **review_context)


@app.route("/food/add", methods=["POST"])
@login_required
def add_food():
    user = current_user()
    data = request.form
    category = data.get("category", "")
    title = data.get("title", "").strip()
    if category not in PRODUCT_TYPES or len(title) < 3:
        flash("Выберите категорию и укажите название не короче 3 символов.", "error")
        return redirect(url_for("food_list"))
    if not allow_sensitive_action("publish", str(user["id"])):
        flash("Слишком много новых объявлений. Попробуйте позже.", "error")
        return redirect(url_for("food_list"))
    city = data.get("city", "").strip()
    try:
        filenames = save_photos(request.files.getlist("photos"))
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("food_list"))
    photos = ",".join(filenames)
    db().execute("""INSERT INTO food (category, title, city, price, description, photo, contacts, telegram, user_id, status, created_at, expires_at, contact_methods)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                 (category, title, city, data.get("price", 0) or 0, data.get("description", ""), photos,
                  user["phone"] or "", user["telegram"] or "", user["id"], utcnow(), iso_after_days(FREE_DAYS),
                  listing_contact_methods(data)))
    db().commit()
    flash("Объявление отправлено на модерацию. После одобрения оно появится в каталоге.", "success")
    return redirect(url_for("account"))


@app.route("/food/<int:food_id>/delete", methods=["POST"])
@login_required
def delete_food(food_id):
    item = db().execute("SELECT * FROM food WHERE id=? AND user_id=?", (food_id, current_user()["id"])).fetchone()
    if not item:
        abort(404)
    revision_photos = discard_listing_revision("food", food_id, item["photo"])
    db().execute("DELETE FROM listing_daily_metrics WHERE listing_type='food' AND listing_id=?", (food_id,))
    db().execute("DELETE FROM food WHERE id=?", (food_id,))
    db().commit()
    delete_photos(item["photo"])
    delete_photos(revision_photos)
    flash("Объявление удалено.", "success")
    return redirect(url_for("food_list"))


@app.route("/food/<int:food_id>/renew", methods=["POST"])
@login_required
def renew_food(food_id):
    item = db().execute("SELECT * FROM food WHERE id=? AND user_id=?", (food_id, current_user()["id"])).fetchone()
    if not item:
        abort(404)
    if item["status"] not in {"active", "expired"}:
        abort(400)
    db().execute("UPDATE food SET status='active', expires_at=? WHERE id=?", (iso_after_days(FREE_DAYS), food_id))
    db().commit()
    flash("Объявление снова опубликовано на 7 дней.", "success")
    return redirect(url_for("account"))


@app.route("/food/<int:food_id>/edit", methods=["GET", "POST"])
@login_required
def edit_food(food_id):
    item = db().execute("SELECT * FROM food WHERE id=? AND user_id=?", (food_id, current_user()["id"])).fetchone()
    if not item:
        abort(404)
    revision = get_listing_revision("food", food_id)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "")
        if category not in PRODUCT_TYPES or len(title) < 3:
            flash("Выберите категорию и укажите название не короче 3 символов.", "error")
            return redirect(url_for("edit_food", food_id=food_id))
        try:
            new_photos = save_photos(request.files.getlist("photos"))
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("edit_food", food_id=food_id))
        revision_values = revision_payload(revision)
        photos = revision_values.get("photo", item["photo"] or "") if revision else item["photo"] or ""
        if new_photos:
            photos = ",".join(new_photos)
        refresh_profile_contacts = request.form.get("refresh_profile_contacts") == "1"
        profile_contacts = current_user()
        payload = {
            "category": category, "title": title, "city": request.form.get("city", "").strip(),
            "price": nonnegative_int(request.form.get("price", 0)), "description": request.form.get("description", ""),
            "photo": photos,
            "contacts": (profile_contacts["phone"] or "") if refresh_profile_contacts else revision_values.get("contacts", item["contacts"] or ""),
            "telegram": (profile_contacts["telegram"] or "") if refresh_profile_contacts else revision_values.get("telegram", item["telegram"] or ""),
            "contact_methods": listing_contact_methods(request.form),
        }
        if item["status"] == "active":
            if not revision and revision_payload_equal(payload, {field: item[field] for field in LISTING_REVISION_FIELDS["food"]}):
                flash("Изменений нет — опубликованная версия осталась прежней.", "success")
                return redirect(url_for("account"))
            if revision and revision["status"] == "pending" and revision_payload_equal(payload, revision_values):
                if new_photos:
                    delete_photos(",".join(new_photos))
                flash("Эта версия уже находится на проверке.", "success")
                return redirect(url_for("account"))
            obsolete_photos = stage_listing_revision("food", item, payload)
            db().commit()
            delete_photos(obsolete_photos)
            flash("Изменения отправлены на повторную модерацию. Пока посетители видят предыдущую опубликованную версию.", "success")
            return redirect(url_for("account"))
        old_photo = item["photo"] or ""
        db().execute(
            """UPDATE food SET category=?, title=?, city=?, price=?, description=?, photo=?, contacts=?, telegram=?, contact_methods=?,
                               status='pending', moderation_reason=NULL, deal_status='open', outcome_at=NULL
                 WHERE id=?""",
            (*[payload[field] for field in LISTING_REVISION_FIELDS["food"]], food_id),
        )
        db().commit()
        if new_photos:
            delete_photos(old_photo)
        flash("Объявление отправлено на повторную модерацию.", "success")
        return redirect(url_for("account"))
    return render_template("edit_listing.html", item=revision_form_item("food", item, revision), listing_type="food", revision=revision)


# ---------- Переписка с продавцом ----------

def listing_owner(listing_type, listing_id):
    """Возвращает user_id владельца объявления или None."""
    if listing_type == "animal":
        row = db().execute("SELECT user_id FROM animals WHERE id=?", (listing_id,)).fetchone()
    elif listing_type == "service":
        row = db().execute("SELECT user_id FROM services WHERE id=?", (listing_id,)).fetchone()
    elif listing_type == "food":
        row = db().execute("SELECT user_id FROM food WHERE id=?", (listing_id,)).fetchone()
    else:
        return None
    return row["user_id"] if row else None


def listing_title(listing_type, listing_id):
    """Возвращает заголовок объявления для отображения в чате."""
    if listing_type == "animal":
        row = db().execute("SELECT type, breed FROM animals WHERE id=?", (listing_id,)).fetchone()
        return f"{row['type']} · {row['breed']}" if row else "Объявление"
    if listing_type == "service":
        row = db().execute("SELECT title FROM services WHERE id=?", (listing_id,)).fetchone()
        return row["title"] if row else "Услуга"
    if listing_type == "food":
        row = db().execute("SELECT title FROM food WHERE id=?", (listing_id,)).fetchone()
        return row["title"] if row else "Товар"
    return "Объявление"


@app.route("/listing/<listing_type>/<int:listing_id>/outcome", methods=["POST"])
@login_required
def set_listing_outcome(listing_type, listing_id):
    """Владелец прозрачно отмечает исход сделки, не ломая модерацию."""
    if listing_type not in LISTING_TABLES:
        abort(404)
    table = LISTING_TABLES[listing_type][0]
    item = db().execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (listing_id, current_user()["id"])).fetchone()
    if not item or item["status"] != "active":
        abort(404)
    selected = request.form.get("deal_status", "")
    allowed = {key for key, _label in OUTCOME_STATUS_OPTIONS[listing_type]}
    if selected not in allowed:
        abort(400)
    current = item["deal_status"] or "open"
    if selected == current:
        flash("Статус объявления уже актуален.", "success")
        return redirect(url_for("account"))
    # Завершённую карточку можно вернуть в рынок только через модерацию:
    # предложение могло устареть за время сделки.
    if selected == "open" and current in FINAL_OUTCOME_STATUSES:
        db().execute(
            f"UPDATE {table} SET status='pending', deal_status='open', outcome_at=NULL, moderation_reason=NULL WHERE id=?",
            (listing_id,),
        )
        db().commit()
        flash("Повторная публикация отправлена на модерацию. После проверки карточка снова появится в каталоге.", "success")
        return redirect(url_for("account"))
    if current in FINAL_OUTCOME_STATUSES:
        abort(400)
    db().execute(
        f"UPDATE {table} SET deal_status=?, outcome_at=? WHERE id=?",
        (selected, None if selected == "open" else utcnow(), listing_id),
    )
    db().commit()
    messages = {
        "open": "Резерв снят: объявление снова активно.",
        "reserved": "Объявление помечено как «В резерве».",
        "sold": "Готово: товар или животное отмечен как проданный.",
        "rehomed": "Готово: объявление отмечено как «Питомец нашёл дом».",
        "completed": "Готово: услуга отмечена как оказанная.",
    }
    flash(messages[selected], "success")
    return redirect(url_for("account"))


# ---------- Отзывы и рейтинг владельца ----------

REVIEWABLE_LISTING_TYPES = frozenset({"animal", "service", "food"})


def get_owner_rating(owner_id):
    """Возвращает числовые средний рейтинг и число отзывов владельца."""
    if not owner_id:
        return {"average": 0.0, "count": 0}
    row = db().execute(
        "SELECT AVG(rating) AS average, COUNT(*) AS count FROM reviews WHERE owner_id=?",
        (owner_id,),
    ).fetchone()
    average = float(row["average"]) if row and row["average"] is not None else 0.0
    return {"average": round(average, 1), "count": int(row["count"] or 0) if row else 0}


def get_owner_reviews(owner_id, limit=100):
    """Последние отзывы о владельце вместе с безопасными данными автора."""
    if not owner_id:
        return []
    return db().execute(
        """SELECT r.id, r.reviewer_id, r.owner_id, r.listing_type, r.listing_id,
                  r.rating, r.comment, r.created_at, r.updated_at,
                  u.name AS reviewer_name, u.avatar AS reviewer_avatar
             FROM reviews r
             JOIN users u ON u.id=r.reviewer_id
             WHERE r.owner_id=?
             ORDER BY r.updated_at DESC, r.id DESC
             LIMIT ?""",
        (owner_id, limit),
    ).fetchall()


def has_two_way_message_exchange(reviewer_id, owner_id, listing_type, listing_id):
    """Проверяет переписку в обе стороны именно по этому объявлению.

    Одно исходящее сообщение недостаточно для оценки: владелец должен хотя бы
    раз ответить, что снижает риск накрутки рейтинга случайными обращениями.
    """
    if reviewer_id == owner_id or listing_type not in REVIEWABLE_LISTING_TYPES:
        return False
    row = db().execute(
        """SELECT
                MAX(CASE WHEN sender_id=? AND receiver_id=? THEN 1 ELSE 0 END) AS reviewer_sent,
                MAX(CASE WHEN sender_id=? AND receiver_id=? THEN 1 ELSE 0 END) AS owner_sent
             FROM messages
             WHERE listing_type=? AND listing_id=?
               AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))""",
        (reviewer_id, owner_id, owner_id, reviewer_id, listing_type, listing_id,
         reviewer_id, owner_id, owner_id, reviewer_id),
    ).fetchone()
    return bool(row and row["reviewer_sent"] and row["owner_sent"])


def review_eligibility_for(user, owner_id, listing_type, listing_id):
    """Может ли текущий пользователь оставить отзыв о владельце объявления."""
    return bool(
        user
        and owner_id
        and listing_type in REVIEWABLE_LISTING_TYPES
        and user["id"] != owner_id
        and has_two_way_message_exchange(user["id"], owner_id, listing_type, listing_id)
    )


def get_my_review(reviewer_id, owner_id, listing_type, listing_id):
    if not reviewer_id or not owner_id or listing_type not in REVIEWABLE_LISTING_TYPES:
        return None
    return db().execute(
        """SELECT id, reviewer_id, owner_id, listing_type, listing_id, rating, comment, created_at, updated_at
             FROM reviews
             WHERE reviewer_id=? AND owner_id=? AND listing_type=? AND listing_id=?""",
        (reviewer_id, owner_id, listing_type, listing_id),
    ).fetchone()


def owner_review_context(owner, listing_type=None, listing_id=None, user=None):
    """Единый набор данных отзывов для карточки и публичного профиля."""
    owner_id = owner["id"] if owner else None
    context = {
        "owner_rating": get_owner_rating(owner_id),
        "owner_reviews": get_owner_reviews(owner_id),
        "review_eligibility": False,
        "my_review": None,
    }
    if owner_id and listing_type in REVIEWABLE_LISTING_TYPES and listing_id is not None:
        context["review_eligibility"] = review_eligibility_for(user, owner_id, listing_type, listing_id)
        if user and user["id"] != owner_id:
            context["my_review"] = get_my_review(user["id"], owner_id, listing_type, listing_id)
    return context


def review_listing_owner_or_404(listing_type, listing_id):
    """Проверяет тип, существование объявления и существование его владельца."""
    if listing_type not in REVIEWABLE_LISTING_TYPES:
        abort(404)
    owner_id = listing_owner(listing_type, listing_id)
    if not owner_id or not db().execute("SELECT 1 FROM users WHERE id=?", (owner_id,)).fetchone():
        abort(404)
    return owner_id


def review_listing_redirect(listing_type, listing_id):
    endpoints = {
        "animal": ("animal_detail", "animal_id"),
        "service": ("service_detail", "service_id"),
        "food": ("food_detail", "food_id"),
    }
    endpoint, parameter = endpoints[listing_type]
    return redirect(url_for(endpoint, **{parameter: listing_id}) + "#reviews")


@app.route("/reviews/<listing_type>/<int:listing_id>", methods=["POST"])
@login_required
def submit_owner_review(listing_type, listing_id):
    """Создаёт либо обновляет только собственный отзыв покупателя."""
    user = current_user()
    owner_id = review_listing_owner_or_404(listing_type, listing_id)
    if not review_eligibility_for(user, owner_id, listing_type, listing_id):
        abort(403)

    try:
        rating = int((request.form.get("rating") or "").strip())
    except (TypeError, ValueError):
        rating = 0
    comment = (request.form.get("comment") or "").strip()
    if rating not in {1, 2, 3, 4, 5}:
        flash("Оценка должна быть от 1 до 5.", "error")
        return review_listing_redirect(listing_type, listing_id)
    if len(comment) > 1000:
        flash("Текст отзыва не должен быть длиннее 1000 символов.", "error")
        return review_listing_redirect(listing_type, listing_id)
    if not allow_sensitive_action("review", str(user["id"])):
        flash("Слишком много изменений отзывов. Попробуйте немного позже.", "error")
        return review_listing_redirect(listing_type, listing_id)

    existing = get_my_review(user["id"], owner_id, listing_type, listing_id)
    now = utcnow()
    db().execute(
        """INSERT INTO reviews
               (reviewer_id, owner_id, listing_type, listing_id, rating, comment, created_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT(reviewer_id, owner_id, listing_type, listing_id)
             DO UPDATE SET rating=excluded.rating, comment=excluded.comment, updated_at=excluded.updated_at""",
        (user["id"], owner_id, listing_type, listing_id, rating, comment, now, now),
    )
    db().commit()
    flash("Отзыв обновлён." if existing else "Спасибо, ваш отзыв опубликован.", "success")
    return review_listing_redirect(listing_type, listing_id)


@app.route("/reviews/<listing_type>/<int:listing_id>/delete", methods=["POST"])
@login_required
def delete_owner_review(listing_type, listing_id):
    """Удаляет только отзыв текущего пользователя по указанному объявлению."""
    user = current_user()
    owner_id = review_listing_owner_or_404(listing_type, listing_id)
    review = get_my_review(user["id"], owner_id, listing_type, listing_id)
    if not review:
        abort(404)
    db().execute("DELETE FROM reviews WHERE id=? AND reviewer_id=?", (review["id"], user["id"]))
    db().commit()
    flash("Ваш отзыв удалён.", "success")
    return review_listing_redirect(listing_type, listing_id)


@app.route("/favorites")
@login_required
def favorites():
    """Избранные объявления пользователя."""
    user = current_user()
    rows = db().execute("SELECT * FROM favorites WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
    items = []
    for row in rows:
        if row["listing_type"] == "animal":
            item = db().execute(f"SELECT * FROM animals WHERE id=? AND status='active' AND {PUBLIC_OUTCOME_SQL}", (row["listing_id"],)).fetchone()
            if item:
                item = dict(item); item["kind"] = "animal"; item["fav_type"] = "animal"
        elif row["listing_type"] == "service":
            item = db().execute(f"SELECT * FROM services WHERE id=? AND status='active' AND {PUBLIC_OUTCOME_SQL}", (row["listing_id"],)).fetchone()
            if item:
                item = dict(item); item["kind"] = "service"; item["fav_type"] = "service"
        else:
            item = db().execute(f"SELECT * FROM food WHERE id=? AND status='active' AND {PUBLIC_OUTCOME_SQL}", (row["listing_id"],)).fetchone()
            if item:
                item = dict(item); item["kind"] = "food"; item["fav_type"] = "food"
        if item:
            items.append(item)
    return render_template("favorites.html", items=items, tab="favorites")


@app.route("/listing/<listing_type>/<int:listing_id>/metric/<metric>", methods=["POST"])
def record_contact_metric(listing_type, listing_id, metric):
    """Фиксирует только обезличенный факт открытия способа связи."""
    if listing_type not in LISTING_TABLES or metric not in {"phone_click", "telegram_click", "chat_click"}:
        abort(404)
    table = LISTING_TABLES[listing_type][0]
    listing = db().execute(
        f"SELECT user_id, status, deal_status FROM {table} WHERE id=?",
        (listing_id,),
    ).fetchone()
    if not listing or listing["status"] != "active" or (listing["deal_status"] or "open") in FINAL_OUTCOME_STATUSES:
        abort(404)
    user = current_user()
    if user and listing["user_id"] == user["id"]:
        return "", 204
    # Аналитика никогда не должна мешать звонку или переходу в чат. Лимит и
    # дедупликация просто молча не учитывают повторный тап.
    if not allow_sensitive_action("metric", f"{listing_type}:{listing_id}:{metric}"):
        return "", 204
    today = utcnow()[:10]
    seen = session.get("listing_metric_seen", {})
    if not isinstance(seen, dict):
        seen = {}
    key = f"{today}:{listing_type}:{listing_id}:{metric}"
    if key not in seen:
        seen[key] = True
        # Не растим cookie: оставляем только действия текущего дня.
        seen = {name: True for name in seen if name.startswith(f"{today}:")}
        session["listing_metric_seen"] = seen
        record_daily_metric(listing_type, listing_id, metric)
        db().commit()
    return "", 204


@app.route("/favorite/<listing_type>/<int:listing_id>", methods=["POST"])
@login_required
def toggle_favorite(listing_type, listing_id):
    """Добавляет или убирает объявление из избранного одним кликом."""
    if listing_type not in ("animal", "service", "food"):
        abort(400)
    user = current_user()

    table = {"animal": "animals", "service": "services", "food": "food"}[listing_type]
    listing = db().execute(
        f"SELECT user_id FROM {table} WHERE id=? AND status='active' AND {PUBLIC_OUTCOME_SQL}", (listing_id,)
    ).fetchone()
    if not listing:
        abort(404)
    # Не позволяем сохранить собственное объявление обходом интерфейса.
    if listing["user_id"] == user["id"]:
        abort(403)

    exists = db().execute("SELECT 1 FROM favorites WHERE user_id=? AND listing_type=? AND listing_id=?",
                          (user["id"], listing_type, listing_id)).fetchone()
    if exists:
        db().execute("DELETE FROM favorites WHERE user_id=? AND listing_type=? AND listing_id=?",
                     (user["id"], listing_type, listing_id))
        db().commit()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(favorite=False)
        return redirect(request.referrer or url_for("index"))
    db().execute("INSERT INTO favorites (user_id, listing_type, listing_id, created_at) VALUES (?, ?, ?, ?)",
                 (user["id"], listing_type, listing_id, utcnow()))
    record_daily_metric(listing_type, listing_id, "favorite_added")
    db().commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(favorite=True)
    return redirect(request.referrer or url_for("index"))


@app.route("/messages")
@login_required
def messages_list():
    """Список отдельных диалогов: одно объявление + один собеседник."""
    user = current_user()
    folder = request.args.get("folder", "inbox")
    if folder not in ("inbox", "archive", "spam", "deleted"):
        folder = "inbox"
    # Все сообщения пользователя, отсортированные по убыванию id. Состояние
    # папки хранится отдельно для каждого собеседника, не в самом сообщении.
    rows = db().execute("""SELECT m.*, u.name AS other_name, u.avatar AS other_avatar
                           FROM messages m
                           JOIN users u ON u.id = CASE WHEN m.sender_id=? THEN m.receiver_id ELSE m.sender_id END
                           WHERE m.sender_id=? OR m.receiver_id=?
                           ORDER BY m.id DESC""",
                        (user["id"], user["id"], user["id"])).fetchall()
    # Группируем по диалогу (listing_type + listing_id + собеседник), берём последнее сообщение.
    dialogs_map = {}
    for row in rows:
        other_id = row["receiver_id"] if row["sender_id"] == user["id"] else row["sender_id"]
        key = (row["listing_type"], row["listing_id"], other_id)
        if key not in dialogs_map:
            dialogs_map[key] = {
                "listing_type": row["listing_type"],
                "listing_id": row["listing_id"],
                "other_id": other_id,
                "other_name": row["other_name"],
                "other_avatar": row["other_avatar"],
                "last_message": row["body"],
                "last_time": row["created_at"],
            }
    state_rows = db().execute("""SELECT listing_type, listing_id, other_id, folder
                               FROM dialog_states WHERE user_id=?""", (user["id"],)).fetchall()
    state_map = {(row["listing_type"], row["listing_id"], row["other_id"]): row["folder"] for row in state_rows}
    dialogs = []
    for key, dialog in dialogs_map.items():
        dialog["folder"] = state_map.get(key, "inbox")
        # «Удалить навсегда» скрывает диалог только у текущего человека;
        # чужая переписка и история собеседника остаются нетронутыми.
        if dialog["folder"] == "hidden":
            continue
        if dialog["folder"] != folder:
            continue
        unread = db().execute("""SELECT COUNT(*) AS c FROM messages
                                 WHERE listing_type=? AND listing_id=? AND receiver_id=? AND sender_id=? AND read=0""",
                              (dialog["listing_type"], dialog["listing_id"], user["id"], dialog["other_id"])).fetchone()["c"]
        dialog["unread"] = unread
        dialog["title"] = listing_title(dialog["listing_type"], dialog["listing_id"])
        dialogs.append(dialog)
    # Считаем количество диалогов в каждой папке.
    counts = {"inbox": 0, "archive": 0, "spam": 0, "deleted": 0}
    for key, dialog in dialogs_map.items():
        dialog_folder = state_map.get(key, "inbox")
        if dialog_folder in counts:
            counts[dialog_folder] += 1
    return render_template("messages.html", dialogs=dialogs, tab="messages",
                           folder=folder, counts=counts)


@app.route("/notifications/unread")
@login_required
def unread_notifications():
    """Данные для браузерного уведомления о новом сообщении."""
    user = current_user()
    unread_query = """FROM messages m
                      JOIN users u ON u.id=m.sender_id
                      LEFT JOIN dialog_states state ON state.user_id=m.receiver_id
                         AND state.listing_type=m.listing_type AND state.listing_id=m.listing_id
                         AND state.other_id=m.sender_id
                      WHERE m.receiver_id=? AND m.read=0 AND COALESCE(state.folder, 'inbox')='inbox'"""
    row = db().execute("SELECT m.body, u.name " + unread_query + " ORDER BY m.id DESC LIMIT 1", (user["id"],)).fetchone()
    count = db().execute("SELECT COUNT(*) AS count " + unread_query, (user["id"],)).fetchone()["count"]
    return jsonify(count=count, sender=row["name"] if row else "", body=row["body"] if row else "")


def requested_other_id():
    """Возвращает положительный ID собеседника из формы/URL либо None."""
    raw = request.form.get("other_id") or request.args.get("other_id")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        abort(400)
    if value < 1:
        abort(400)
    return value


def has_dialog_with(user_id, listing_type, listing_id, other_id):
    """Проверяет, что указанный ID — настоящий собеседник в этом диалоге."""
    if other_id == user_id:
        return False
    return bool(db().execute("""SELECT 1 FROM messages
                              WHERE listing_type=? AND listing_id=?
                                AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?))
                              LIMIT 1""",
                           (listing_type, listing_id, user_id, other_id, other_id, user_id)).fetchone())


def resolve_dialog_other(user, listing_type, listing_id, candidate=None):
    """Не позволяет владельцу объявления случайно попасть в чужой диалог."""
    owner_id = listing_owner(listing_type, listing_id)
    if not owner_id:
        abort(404)
    if owner_id != user["id"]:
        if candidate is not None and candidate != owner_id:
            abort(403)
        return owner_id
    if candidate is None:
        return None
    if not has_dialog_with(user["id"], listing_type, listing_id, candidate):
        abort(404)
    return candidate


def set_dialog_folder(user_id, listing_type, listing_id, other_id, folder):
    db().execute("""INSERT INTO dialog_states (user_id, listing_type, listing_id, other_id, folder, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, listing_type, listing_id, other_id)
                    DO UPDATE SET folder=excluded.folder, updated_at=excluded.updated_at""",
                 (user_id, listing_type, listing_id, other_id, folder, utcnow()))


def dialog_is_blocked(user_id, listing_type, listing_id, other_id):
    # other_id=0 — сохранённая миграцией старая блокировка всего объявления.
    return bool(db().execute("""SELECT 1 FROM dialog_blocks
                              WHERE user_id=? AND listing_type=? AND listing_id=? AND other_id IN (?, 0)""",
                           (user_id, listing_type, listing_id, other_id)).fetchone())


@app.route("/messages/<listing_type>/<int:listing_id>/folder/<folder>", methods=["POST"])
@login_required
def move_message(listing_type, listing_id, folder):
    """Меняет папку только у одного собеседника текущего пользователя."""
    if folder not in ("inbox", "archive", "spam", "deleted"):
        abort(400)
    user = current_user()
    other_id = requested_other_id()
    if other_id is None or not has_dialog_with(user["id"], listing_type, listing_id, other_id):
        abort(404)
    set_dialog_folder(user["id"], listing_type, listing_id, other_id, folder)
    # Спам — собеседник больше не сможет писать в этот диалог.
    if folder == "spam":
        db().execute("""INSERT OR REPLACE INTO dialog_blocks (user_id, listing_type, listing_id, other_id, created_at)
                        VALUES (?, ?, ?, ?, ?)""",
                     (user["id"], listing_type, listing_id, other_id, utcnow()))
    else:
        db().execute("""DELETE FROM dialog_blocks WHERE user_id=? AND listing_type=? AND listing_id=? AND other_id=?""",
                     (user["id"], listing_type, listing_id, other_id))
    db().commit()
    source = request.form.get("from_folder", "inbox")
    if source not in ("inbox", "archive", "spam", "deleted"):
        source = "inbox"
    return redirect(url_for("messages_list", folder=source))


@app.route("/messages/<listing_type>/<int:listing_id>/delete", methods=["POST"])
@login_required
def delete_dialog(listing_type, listing_id):
    """Безвозвратно скрывает диалог только из кабинета текущего пользователя."""
    user = current_user()
    other_id = requested_other_id()
    if other_id is None or not has_dialog_with(user["id"], listing_type, listing_id, other_id):
        abort(404)
    set_dialog_folder(user["id"], listing_type, listing_id, other_id, "hidden")
    db().execute("""DELETE FROM dialog_blocks WHERE user_id=? AND listing_type=? AND listing_id=? AND other_id=?""",
                 (user["id"], listing_type, listing_id, other_id))
    db().commit()
    flash("Диалог удалён из вашего кабинета.", "success")
    return redirect(url_for("messages_list", folder="deleted"))


@app.route("/messages/<listing_type>/<int:listing_id>")
@login_required
def message_thread(listing_type, listing_id):
    """Страница переписки по конкретному объявлению."""
    user = current_user()
    other_id = resolve_dialog_other(user, listing_type, listing_id, requested_other_id())
    if not other_id:
        flash("Выберите собеседника в списке сообщений.", "error")
        return redirect(url_for("messages_list"))
    # Отмечаем входящие сообщения прочитанными.
    db().execute("""UPDATE messages SET read=1 WHERE listing_type=? AND listing_id=?
                  AND receiver_id=? AND sender_id=? AND read=0""",
                 (listing_type, listing_id, user["id"], other_id))
    db().commit()
    messages = db().execute("""SELECT m.*, u.name AS sender_name, u.avatar AS sender_avatar
                               FROM messages m JOIN users u ON u.id=m.sender_id
                               WHERE m.listing_type=? AND m.listing_id=?
                                 AND ((m.sender_id=? AND m.receiver_id=?) OR (m.sender_id=? AND m.receiver_id=?))
                               ORDER BY m.id ASC""",
                            (listing_type, listing_id, user["id"], other_id, other_id, user["id"])).fetchall()
    other = db().execute("SELECT id, name, avatar FROM users WHERE id=?", (other_id,)).fetchone()
    if not other:
        abort(404)
    spam_blocked = dialog_is_blocked(other_id, listing_type, listing_id, user["id"])
    return render_template("thread.html", messages=messages, other=other,
                           listing_type=listing_type, listing_id=listing_id,
                           title=listing_title(listing_type, listing_id), tab="messages",
                           spam_blocked=spam_blocked)


@app.route("/messages/<listing_type>/<int:listing_id>/send", methods=["POST"])
@login_required
def send_message(listing_type, listing_id):
    user = current_user()
    receiver_id = resolve_dialog_other(user, listing_type, listing_id, requested_other_id())
    body = request.form.get("body", "").strip()
    if not body:
        flash("Сообщение не может быть пустым.", "error")
        return redirect(url_for("message_thread", listing_type=listing_type, listing_id=listing_id, other_id=receiver_id))
    if not receiver_id:
        flash("Не удалось определить собеседника.", "error")
        return redirect(url_for("messages_list"))
    if len(body) > 3000:
        flash("Сообщение не должно быть длиннее 3000 символов.", "error")
        return redirect(url_for("message_thread", listing_type=listing_type, listing_id=listing_id, other_id=receiver_id))
    if not allow_sensitive_action("message", f"{user['id']}:{receiver_id}"):
        flash("Слишком много сообщений. Подождите минуту и попробуйте снова.", "error")
        return redirect(url_for("message_thread", listing_type=listing_type, listing_id=listing_id, other_id=receiver_id))
    # Если собеседник отправил диалог в спам — писать ему больше нельзя.
    if dialog_is_blocked(receiver_id, listing_type, listing_id, user["id"]):
        flash("Диалог находится в спаме — собеседник больше не принимает сообщения.", "error")
        return redirect(url_for("message_thread", listing_type=listing_type, listing_id=listing_id, other_id=receiver_id))
    db().execute("INSERT INTO messages (listing_type, listing_id, sender_id, receiver_id, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (listing_type, listing_id, user["id"], receiver_id, body, utcnow()))
    # Если диалог ранее был удалён, новое сообщение возвращает его во входящие
    # лишь для участников этой конкретной переписки.
    for participant_id, other_id in ((user["id"], receiver_id), (receiver_id, user["id"])):
        state = db().execute("""SELECT folder FROM dialog_states WHERE user_id=? AND listing_type=?
                              AND listing_id=? AND other_id=?""",
                             (participant_id, listing_type, listing_id, other_id)).fetchone()
        if not state or state["folder"] in {"deleted", "hidden"}:
            set_dialog_folder(participant_id, listing_type, listing_id, other_id, "inbox")
    db().commit()
    return redirect(url_for("message_thread", listing_type=listing_type, listing_id=listing_id, other_id=receiver_id))


init_db()
if __name__ == "__main__":
    # Для production используйте Gunicorn из wsgi.py. Debug нельзя включать в production.
    app.run(
        debug=env_flag("ZOOLAND_DEBUG") and not IS_PRODUCTION,
        host=os.environ.get("ZOOLAND_HOST", "127.0.0.1"),
        port=PORT,
    )
