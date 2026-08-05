import json
import math
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from catalog import ANIMAL_TYPES, BREEDS, FOOD_TYPES, DEAL_TYPES
from services import SERVICE_TYPES, SERVICE_PRICE, SERVICE_FIRST_PRICE, SERVICE_DAYS, PET_TYPES, service_price

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("ZOOLAND_SECRET_KEY", "change-this-secret-before-production"),
    UPLOAD_FOLDER="static/uploads",
)
DB_NAME = "zooland.db"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
FREE_DAYS = 7
EXTEND_PRICE = 100
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

with open("static/russian-cities.json", encoding="utf-8") as cities_file:
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
        photo TEXT, user_id INTEGER, status TEXT DEFAULT 'active',
        created_at TEXT, expires_at TEXT, months_published INTEGER DEFAULT 0,
        views_total INTEGER DEFAULT 0, views_data TEXT
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS food (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, title TEXT NOT NULL,
        city TEXT, price INTEGER DEFAULT 0, description TEXT, photo TEXT,
        user_id INTEGER, status TEXT DEFAULT 'active', created_at TEXT,
        expires_at TEXT, views_total INTEGER DEFAULT 0, views_data TEXT
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, listing_type TEXT NOT NULL,
        listing_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL,
        body TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER DEFAULT 0
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_listing ON messages(listing_type, listing_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_receiver ON messages(receiver_id, read)")
    user_columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
    for column, definition in {
        "phone": "TEXT", "email_verified": "INTEGER DEFAULT 0", "balance": "INTEGER DEFAULT 0",
        "city": "TEXT", "gender": "TEXT", "birth_date": "TEXT", "about": "TEXT", "avatar": "TEXT", "telegram": "TEXT",
    }.items():
        if column not in user_columns:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    # Уже существующие аккаунты не блокируем после обновления схемы.
    cursor.execute("UPDATE users SET email_verified=1 WHERE email_verified IS NULL")
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(animals)")}
    migrations = {
        "user_id": "INTEGER", "guest_token": "TEXT", "status": "TEXT DEFAULT 'active'",
        "created_at": "TEXT", "expires_at": "TEXT", "archived_at": "TEXT",
        "views_total": "INTEGER DEFAULT 0", "views_data": "TEXT", "deal_type": "TEXT DEFAULT 'sale'",
    }
    for column, definition in migrations.items():
        if column not in columns:
            cursor.execute(f"ALTER TABLE animals ADD COLUMN {column} {definition}")
    # У существующих объявлений по умолчанию тип сделки — продажа.
    cursor.execute("UPDATE animals SET deal_type='sale' WHERE deal_type IS NULL")
    now = utcnow()
    cursor.execute("UPDATE animals SET status='active' WHERE status IS NULL")
    cursor.execute("UPDATE animals SET created_at=? WHERE created_at IS NULL", (now,))
    cursor.execute("UPDATE animals SET expires_at=? WHERE expires_at IS NULL", (iso_after_days(FREE_DAYS),))
    connection.commit()
    connection.close()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def iso_after_days(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def normalize_russian_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    return f"+{digits}" if len(digits) == 11 and digits.startswith("7") else None


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
    return db().execute("SELECT id, name, email, phone, email_verified, balance, city, gender, birth_date, about, avatar, telegram FROM users WHERE id=?", (user_id,)).fetchone()


def guest_token():
    return request.cookies.get("zooland_guest")


def owner_clause(user=None):
    if user:
        return "user_id=?", [user["id"]]
    token = guest_token()
    return "guest_token=?", [token] if token else [""]


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


@app.context_processor
def global_template_data():
    return {"account_user": current_user(), "animal_types": ANIMAL_TYPES, "breeds": BREEDS, "can_manage": can_manage, "russian_cities": RUSSIAN_CITIES, "service_types": SERVICE_TYPES, "pet_types": PET_TYPES, "food_types": FOOD_TYPES, "deal_types": DEAL_TYPES}


@app.route("/")
def index():
    city, animal_type, search, kind = (request.args.get(key, "").strip() for key in ("city", "type", "q", "kind"))
    sort = request.args.get("sort", "new")
    # Животные (объявления о продаже) — бесплатная публикация.
    if kind != "services":
        query, conditions, params = "SELECT * FROM animals", ["status='active'"], []
        if city: conditions.append("city=?"); params.append(city)
        if animal_type: conditions.append("type=?"); params.append(animal_type)
        query += " WHERE " + " AND ".join(conditions)
        query += {"price_asc": " ORDER BY price ASC", "price_desc": " ORDER BY price DESC"}.get(sort, " ORDER BY id DESC")
        animals = [dict(row) for row in db().execute(query, params).fetchall()]
        if search:
            animals = [animal for animal in animals if animal_matches_search(animal, search)]
        for animal in animals:
            animal["kind"] = "animal"
    else:
        animals = []
    # Услуги для животных — платная публикация.
    if kind != "animals":
        query, conditions, params = "SELECT * FROM services", ["status='active'"], []
        if city: conditions.append("city=?"); params.append(city)
        query += " WHERE " + " AND ".join(conditions) + " ORDER BY id DESC"
        services = [dict(row) for row in db().execute(query, params).fetchall()]
        if search:
            search_lower = search.casefold()
            services = [s for s in services if search_lower in (s["title"] or "").casefold() or search_lower in (s["description"] or "").casefold()]
        for service in services:
            service["kind"] = "service"
    else:
        services = []
    items = sorted(list(animals) + list(services), key=lambda item: item["created_at"], reverse=True)[:24]
    return render_template("index.html", items=items, animals=animals, services=services,
                           cities=RUSSIAN_CITIES, selected_city=city, selected_type=animal_type,
                           search=search, sort=sort, kind=kind)


@app.route("/animal/<int:animal_id>")
def animal_detail(animal_id):
    animal = db().execute("SELECT * FROM animals WHERE id=? AND status='active'", (animal_id,)).fetchone()
    if not animal:
        abort(404)
    user = current_user()
    # Учёт просмотров: не считаем просмотры самого владельца объявления.
    if not (user and animal["user_id"] == user["id"]):
        today = utcnow()[:10]
        views_data = json.loads(animal["views_data"] or "{}")
        views_data[today] = views_data.get(today, 0) + 1
        db().execute("UPDATE animals SET views_total=views_total+1, views_data=? WHERE id=?",
                     (json.dumps(views_data), animal_id))
        db().commit()
    views_data = json.loads(animal["views_data"] or "{}")
    views_today = views_data.get(utcnow()[:10], 0)
    views_total = animal["views_total"] or 0
    if not (user and animal["user_id"] == user["id"]):
        views_today += 1
        views_total += 1
    owner = None
    if animal["user_id"]:
        owner = db().execute("SELECT name, telegram FROM users WHERE id=?", (animal["user_id"],)).fetchone()
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
                f"SELECT * FROM animals WHERE status='active' AND type=? AND city IN ({placeholders}) AND id!=? ORDER BY id DESC LIMIT ?",
                (animal["type"], *nearby_cities, animal_id, SIMILAR_LIMIT)).fetchall()
    return render_template("animal.html", animal=animal, owner=owner, similar=similar,
                           views_today=views_today, views_total=views_total, is_owner=is_owner)


@app.route("/services")
def services_list():
    """Каталог услуг для животных."""
    service_type, city, search = (request.args.get(key, "").strip() for key in ("type", "city", "q"))
    query, conditions, params = "SELECT * FROM services", ["status='active'"], []
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
    my_services_count = 0
    if current_user():
        my_services_count = db().execute("SELECT COUNT(*) AS c FROM services WHERE user_id=?", (current_user()["id"],)).fetchone()["c"]
    return render_template("services.html", services=services, cities=RUSSIAN_CITIES,
                           selected_type=service_type, selected_city=city, search=search, my_services_count=my_services_count)


@app.route("/service/<int:service_id>")
def service_detail(service_id):
    service = db().execute("SELECT * FROM services WHERE id=? AND status='active'", (service_id,)).fetchone()
    if not service:
        abort(404)
    owner = None
    my_service = False
    if service["user_id"]:
        owner = db().execute("SELECT name, telegram, city, phone FROM users WHERE id=?", (service["user_id"],)).fetchone()
        my_service = bool(current_user() and current_user()["id"] == service["user_id"])
    return render_template("service.html", service=service, owner=owner, my_service=my_service)


@app.route("/service/add", methods=["POST"])
@login_required
def add_service():
    user = current_user()
    data, file = request.form, request.files.get("photo")
    service_type = data.get("type", "")
    title = data.get("title", "").strip()
    if service_type not in SERVICE_TYPES or len(title) < 3:
        flash("Выберите тип услуги и укажите название не короче 3 символов.", "error")
        return redirect(url_for("services_list"))
    # Стоимость публикации: первый месяц 100 ₽, далее 250 ₽.
    count = db().execute("SELECT COUNT(*) AS c FROM services WHERE user_id=?", (user["id"],)).fetchone()["c"]
    cost = service_price(count)
    if user["balance"] < cost:
        flash(f"Недостаточно средств. Размещение услуги на месяц — {cost} ₽. Пополните баланс.", "error")
        return redirect(url_for("services_list"))
    pet_types = ",".join(data.getlist("pet_types")) if data.getlist("pet_types") else "Другие животные"
    city = data.get("city", "").strip()
    filename = None
    if file and file.filename:
        if not allowed_file(file.filename):
            flash("Подойдут только изображения PNG, JPG, GIF или WEBP.", "error")
            return redirect(url_for("services_list"))
        filename = f"{secrets.token_hex(8)}_{secure_filename(file.filename)}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    db().execute("""INSERT INTO services (type, title, pet_types, city, price, description, photo, user_id, status, created_at, expires_at, months_published)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                 (service_type, title, pet_types, city, data.get("price", 0) or 0, data.get("description", ""), filename,
                  user["id"], utcnow(), iso_after_days(SERVICE_DAYS), count + 1))
    db().execute("UPDATE users SET balance=balance-? WHERE id=?", (cost, user["id"]))
    db().commit()
    flash(f"Услуга опубликована на 30 дней. Списано {cost} ₽.", "success")
    return redirect(url_for("services_list"))


@app.route("/service/<int:service_id>/renew", methods=["POST"])
@login_required
def renew_service(service_id):
    service = db().execute("SELECT * FROM services WHERE id=? AND user_id=?", (service_id, current_user()["id"])).fetchone()
    if not service:
        abort(404)
    cost = service_price(service["months_published"])
    user = current_user()
    if user["balance"] < cost:
        flash(f"Недостаточно средств. Продление на месяц — {cost} ₽. Пополните баланс.", "error")
        return redirect(url_for("services_list"))
    db().execute("UPDATE users SET balance=balance-? WHERE id=?", (cost, user["id"]))
    db().execute("UPDATE services SET status='active', expires_at=?, months_published=months_published+1 WHERE id=?",
                 (iso_after_days(SERVICE_DAYS), service_id))
    db().commit()
    flash(f"Услуга продлена на 30 дней. Списано {cost} ₽. Следующее продление — {SERVICE_PRICE} ₽.", "success")
    return redirect(url_for("services_list"))


@app.route("/service/<int:service_id>/delete", methods=["POST"])
@login_required
def delete_service(service_id):
    service = db().execute("SELECT * FROM services WHERE id=? AND user_id=?", (service_id, current_user()["id"])).fetchone()
    if not service:
        abort(404)
    if service["photo"]:
        try:
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], service["photo"]))
        except OSError:
            pass
    db().execute("DELETE FROM services WHERE id=?", (service_id,))
    db().commit()
    flash("Услуга удалена.", "success")
    return redirect(url_for("services_list"))


@app.route("/add", methods=["POST"])
def add_animal():
    user, token = current_user(), guest_token()
    if not user:
        if token and db().execute("SELECT 1 FROM animals WHERE guest_token=? AND status='active'", (token,)).fetchone():
            flash("Без регистрации доступно только одно активное объявление. Зарегистрируйтесь, чтобы добавить ещё.", "error")
            return redirect(url_for("index", _anchor="publish"))
        token = token or secrets.token_urlsafe(24)
    data, file = request.form, request.files.get("photo")
    phone = normalize_russian_phone(data.get("contacts"))
    if not phone:
        flash("Укажите номер российского телефона в формате +7 999 123-45-67.", "error")
        return redirect(url_for("index", _anchor="publish"))
    filename = None
    if file and file.filename:
        if not allowed_file(file.filename):
            flash("Подойдут только изображения PNG, JPG, GIF или WEBP.", "error")
            return redirect(url_for("index", _anchor="publish"))
        filename = f"{secrets.token_hex(8)}_{secure_filename(file.filename)}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    deal_type = data.get("deal_type", "sale")
    if deal_type not in DEAL_TYPES:
        deal_type = "sale"
    db().execute("""INSERT INTO animals (type, breed, age, price, city, description, contacts, photo, user_id, guest_token, status, created_at, expires_at, deal_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                 (data["type"], data["breed"], data["age"], data["price"], data["city"], data.get("description", ""), phone, filename,
                  user["id"] if user else None, None if user else token, utcnow(), iso_after_days(FREE_DAYS), deal_type))
    db().commit()
    response = redirect(url_for("account") if user else url_for("index"))
    if not user and not guest_token(): response.set_cookie("zooland_guest", token, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user(): return redirect(url_for("account"))
    if request.method == "POST":
        name, email, password = request.form["name"].strip(), request.form["email"].lower().strip(), request.form["password"]
        phone = normalize_russian_phone(request.form.get("phone"))
        if len(name) < 2 or not is_valid_email(email) or len(password) < 6 or not phone:
            flash("Укажите имя, российский номер +7, email с @ на известном почтовом сервисе и пароль не короче 6 символов.", "error")
        elif db().execute("SELECT 1 FROM users WHERE email=? OR phone=?", (email, phone)).fetchone():
            flash("Этот email или номер уже зарегистрирован. Войдите в аккаунт.", "error")
        else:
            cursor = db().execute("INSERT INTO users (name, email, phone, password_hash, email_verified, created_at) VALUES (?, ?, ?, ?, 0, ?)", (name, email, phone, generate_password_hash(password), utcnow()))
            token = guest_token()
            if token: db().execute("UPDATE animals SET user_id=?, guest_token=NULL WHERE guest_token=?", (cursor.lastrowid, token))
            db().commit()
            session["user_id"] = cursor.lastrowid
            response = redirect(url_for("account")); response.delete_cookie("zooland_guest"); return response
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user(): return redirect(url_for("account"))
    if request.method == "POST":
        user = db().execute("SELECT * FROM users WHERE email=?", (request.form["email"].lower().strip(),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session["user_id"] = user["id"]; return redirect(url_for("account"))
        flash("Неверный email или пароль.", "error")
    return render_template("auth.html", mode="login")




@app.route("/logout", methods=["POST"])
def logout():
    session.clear(); return redirect(url_for("index"))


@app.route("/account")
@login_required
def account():
    user = current_user()
    active = db().execute("SELECT * FROM animals WHERE user_id=? AND status='active' ORDER BY id DESC", (user["id"],)).fetchall()
    archived = db().execute("SELECT * FROM animals WHERE user_id=? AND status='archived' ORDER BY archived_at DESC", (user["id"],)).fetchall()
    return render_template("account.html", active=active, archived=archived, tab="listings")


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html", tab="settings")


@app.route("/settings/profile", methods=["POST"])
@login_required
def settings_profile():
    user = current_user()
    name = request.form.get("name", "").strip()
    city = request.form.get("city", "").strip()
    gender = request.form.get("gender", "").strip()
    birth_date = request.form.get("birth_date", "").strip()
    about = request.form.get("about", "").strip()
    telegram = request.form.get("telegram", "").strip().lstrip("@")
    if len(name) < 2:
        flash("Имя должно быть не короче 2 символов.", "error")
        return redirect(url_for("settings"))
    avatar = user["avatar"]
    file = request.files.get("avatar")
    if file and file.filename:
        if not allowed_file(file.filename):
            flash("Подойдут только изображения PNG, JPG, GIF или WEBP.", "error")
            return redirect(url_for("settings"))
        filename = f"{secrets.token_hex(8)}_{secure_filename(file.filename)}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        if avatar:
            try:
                os.remove(os.path.join(app.config["UPLOAD_FOLDER"], avatar))
            except OSError:
                pass
        avatar = filename
    db().execute("UPDATE users SET name=?, city=?, gender=?, birth_date=?, about=?, avatar=?, telegram=? WHERE id=?",
                 (name, city or None, gender or None, birth_date or None, about or None, avatar, telegram or None, user["id"]))
    db().commit()
    flash("Профиль обновлён.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/email", methods=["POST"])
@login_required
def settings_email():
    user = current_user()
    email = request.form.get("email", "").lower().strip()
    if not is_valid_email(email):
        flash("Укажите корректный email на известном почтовом сервисе.", "error")
    elif db().execute("SELECT 1 FROM users WHERE email=? AND id!=?", (email, user["id"])).fetchone():
        flash("Этот email уже занят другим аккаунтом.", "error")
    else:
        db().execute("UPDATE users SET email=? WHERE id=?", (email, user["id"])); db().commit()
        flash("Email обновлён.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/phone", methods=["POST"])
@login_required
def settings_phone():
    user = current_user()
    phone = normalize_russian_phone(request.form.get("phone"))
    if not phone:
        flash("Укажите номер российского телефона в формате +7 999 123-45-67.", "error")
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
    # Удаляем фотографии объявлений пользователя вместе с записями.
    rows = db().execute("SELECT photo FROM animals WHERE user_id=?", (user["id"],)).fetchall()
    for row in rows:
        if row["photo"]:
            try:
                os.remove(os.path.join(app.config["UPLOAD_FOLDER"], row["photo"]))
            except OSError:
                pass
    if user["avatar"]:
        try:
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], user["avatar"]))
        except OSError:
            pass
    db().execute("DELETE FROM animals WHERE user_id=?", (user["id"],))
    db().execute("DELETE FROM users WHERE id=?", (user["id"],))
    # Наследование объявлений удалённого пользователя: гостевые токены остаются, записи удалены выше.
    db().commit()
    session.clear()
    flash("Аккаунт и все ваши объявления удалены.", "success")
    return redirect(url_for("index"))


def owned_animal(animal_id):
    animal = db().execute("SELECT * FROM animals WHERE id=?", (animal_id,)).fetchone()
    if not animal or not can_manage(animal): abort(404)
    return animal


@app.route("/edit/<int:animal_id>")
def edit_animal(animal_id):
    return render_template("edit.html", animal=owned_animal(animal_id))


@app.route("/update/<int:animal_id>", methods=["POST"])
def update_animal(animal_id):
    animal, data, file = owned_animal(animal_id), request.form, request.files.get("photo")
    phone = normalize_russian_phone(data.get("contacts"))
    if not phone:
        flash("Укажите номер российского телефона в формате +7 999 123-45-67.", "error")
        return redirect(url_for("edit_animal", animal_id=animal_id))
    filename = animal["photo"]
    if file and file.filename:
        if not allowed_file(file.filename): flash("Неподдерживаемый формат фото.", "error"); return redirect(url_for("edit_animal", animal_id=animal_id))
        filename = f"{secrets.token_hex(8)}_{secure_filename(file.filename)}"; file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    deal_type = data.get("deal_type", "sale")
    if deal_type not in DEAL_TYPES:
        deal_type = "sale"
    db().execute("UPDATE animals SET type=?, breed=?, age=?, price=?, city=?, description=?, contacts=?, photo=?, deal_type=? WHERE id=?", (data["type"], data["breed"], data["age"], data["price"], data["city"], data.get("description", ""), phone, filename, deal_type, animal_id)); db().commit()
    return redirect(url_for("account") if current_user() else url_for("index"))


@app.route("/archive/<int:animal_id>", methods=["POST"])
@login_required
def archive_animal(animal_id):
    animal = owned_animal(animal_id)
    if animal["status"] != "active": abort(400)
    db().execute("UPDATE animals SET status='archived', archived_at=? WHERE id=?", (utcnow(), animal_id)); db().commit()
    flash("Объявление убрано в архив.", "success")
    return redirect(url_for("account"))


@app.route("/extend/<int:animal_id>", methods=["POST"])
@login_required
def extend_animal(animal_id):
    animal = owned_animal(animal_id)
    if animal["status"] != "active": abort(400)
    user = current_user()
    if user["balance"] < EXTEND_PRICE:
        flash(f"Недостаточно средств. Продление на 7 дней стоит {EXTEND_PRICE} ₽. Пополните баланс.", "error")
        return redirect(url_for("account"))
    db().execute("UPDATE users SET balance=balance-? WHERE id=?", (EXTEND_PRICE, user["id"]))
    db().execute("UPDATE animals SET expires_at=? WHERE id=?", (iso_after_days(FREE_DAYS), animal_id)); db().commit()
    flash(f"Объявление продлено на 7 дней. Списано {EXTEND_PRICE} ₽.", "success")
    return redirect(url_for("account"))


@app.route("/reactivate/<int:animal_id>", methods=["POST"])
@login_required
def reactivate(animal_id):
    animal = owned_animal(animal_id)
    if animal["status"] != "archived": abort(400)
    user = current_user()
    if user["balance"] < EXTEND_PRICE:
        flash(f"Недостаточно средств. Размещение из архива стоит {EXTEND_PRICE} ₽. Пополните баланс.", "error")
        return redirect(url_for("account"))
    db().execute("UPDATE users SET balance=balance-? WHERE id=?", (EXTEND_PRICE, user["id"]))
    db().execute("UPDATE animals SET status='active', expires_at=?, archived_at=NULL WHERE id=?", (iso_after_days(FREE_DAYS), animal_id)); db().commit()
    flash(f"Объявление снова опубликовано на 7 дней. Списано {EXTEND_PRICE} ₽.", "success")
    return redirect(url_for("account"))


@app.route("/topup", methods=["POST"])
@login_required
def topup():
    user = current_user()
    try:
        amount = int(request.form.get("amount", 0))
    except ValueError:
        amount = 0
    if amount < 100 or amount > 100000:
        flash("Сумма пополнения — от 100 до 100 000 ₽.", "error")
        return redirect(url_for("account"))
    # Платёжная система будет подключена позже — сейчас баланс пополняется сразу.
    db().execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, user["id"])); db().commit()
    flash(f"Баланс пополнен на {amount} ₽.", "success")
    return redirect(url_for("account"))


@app.route("/delete/<int:animal_id>", methods=["POST"])
def delete_animal(animal_id):
    owned_animal(animal_id); db().execute("DELETE FROM animals WHERE id=?", (animal_id,)); db().commit()
    return redirect(url_for("account") if current_user() else url_for("index"))


# ---------- Зоопитание и сопутствующие товары ----------

@app.route("/food")
def food_list():
    """Каталог зоопитания и сопутствующих товаров."""
    category, city, search = (request.args.get(key, "").strip() for key in ("category", "city", "q"))
    query, conditions, params = "SELECT * FROM food", ["status='active'"], []
    if category:
        conditions.append("category=?")
        params.append(category)
    if city:
        conditions.append("city=?")
        params.append(city)
    query += " WHERE " + " AND ".join(conditions) + " ORDER BY id DESC"
    food = db().execute(query, params).fetchall()
    if search:
        search_lower = search.casefold()
        food = [f for f in food if search_lower in (f["title"] or "").casefold() or search_lower in (f["description"] or "").casefold()]
    return render_template("food.html", food=food, cities=RUSSIAN_CITIES,
                           selected_category=category, selected_city=city, search=search)


@app.route("/food/<int:food_id>")
def food_detail(food_id):
    item = db().execute("SELECT * FROM food WHERE id=? AND status='active'", (food_id,)).fetchone()
    if not item:
        abort(404)
    user = current_user()
    if not (user and item["user_id"] == user["id"]):
        today = utcnow()[:10]
        views_data = json.loads(item["views_data"] or "{}")
        views_data[today] = views_data.get(today, 0) + 1
        db().execute("UPDATE food SET views_total=views_total+1, views_data=? WHERE id=?",
                     (json.dumps(views_data), food_id))
        db().commit()
    owner = None
    my_item = False
    if item["user_id"]:
        owner = db().execute("SELECT name, telegram, city, phone FROM users WHERE id=?", (item["user_id"],)).fetchone()
        my_item = bool(user and user["id"] == item["user_id"])
    return render_template("food_item.html", item=item, owner=owner, my_item=my_item)


@app.route("/food/add", methods=["POST"])
@login_required
def add_food():
    user = current_user()
    data, file = request.form, request.files.get("photo")
    category = data.get("category", "")
    title = data.get("title", "").strip()
    if category not in FOOD_TYPES or len(title) < 3:
        flash("Выберите категорию и укажите название не короче 3 символов.", "error")
        return redirect(url_for("food_list"))
    city = data.get("city", "").strip()
    filename = None
    if file and file.filename:
        if not allowed_file(file.filename):
            flash("Подойдут только изображения PNG, JPG, GIF или WEBP.", "error")
            return redirect(url_for("food_list"))
        filename = f"{secrets.token_hex(8)}_{secure_filename(file.filename)}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    db().execute("""INSERT INTO food (category, title, city, price, description, photo, user_id, status, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                 (category, title, city, data.get("price", 0) or 0, data.get("description", ""), filename,
                  user["id"], utcnow(), iso_after_days(FREE_DAYS)))
    db().commit()
    flash("Объявление о зоопитании опубликовано.", "success")
    return redirect(url_for("food_list"))


@app.route("/food/<int:food_id>/delete", methods=["POST"])
@login_required
def delete_food(food_id):
    item = db().execute("SELECT * FROM food WHERE id=? AND user_id=?", (food_id, current_user()["id"])).fetchone()
    if not item:
        abort(404)
    if item["photo"]:
        try:
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], item["photo"]))
        except OSError:
            pass
    db().execute("DELETE FROM food WHERE id=?", (food_id,))
    db().commit()
    flash("Объявление удалено.", "success")
    return redirect(url_for("food_list"))


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


@app.route("/messages")
@login_required
def messages_list():
    """Список диалогов пользователя."""
    user = current_user()
    rows = db().execute("""SELECT m.*, u.name AS other_name, u.avatar AS other_avatar
                           FROM messages m
                           JOIN users u ON u.id = CASE WHEN m.sender_id=? THEN m.receiver_id ELSE m.sender_id END
                           WHERE m.sender_id=? OR m.receiver_id=?
                           ORDER BY m.id DESC""", (user["id"], user["id"], user["id"])).fetchall()
    # Группируем по диалогу (пара listing_type+listing_id+собеседник).
    dialogs = {}
    for row in rows:
        other_id = row["receiver_id"] if row["sender_id"] == user["id"] else row["sender_id"]
        key = (row["listing_type"], row["listing_id"], other_id)
        if key not in dialogs:
            dialogs[key] = {
                "listing_type": row["listing_type"],
                "listing_id": row["listing_id"],
                "other_id": other_id,
                "other_name": row["other_name"],
                "other_avatar": row["other_avatar"],
                "last_message": row["body"],
                "last_time": row["created_at"],
                "unread": 0,
            }
        if row["receiver_id"] == user["id"] and not row["read"]:
            dialogs[key]["unread"] += 1
    for dialog in dialogs.values():
        dialog["title"] = listing_title(dialog["listing_type"], dialog["listing_id"])
    return render_template("messages.html", dialogs=list(dialogs.values()), tab="messages")


@app.route("/messages/<listing_type>/<int:listing_id>")
@login_required
def message_thread(listing_type, listing_id):
    """Страница переписки по конкретному объявлению."""
    user = current_user()
    owner_id = listing_owner(listing_type, listing_id)
    if not owner_id:
        abort(404)
    other_id = owner_id if owner_id != user["id"] else None
    if not other_id:
        # Владелец смотрит свой диалог — найдём собеседника из сообщений.
        row = db().execute("""SELECT CASE WHEN sender_id=? THEN receiver_id ELSE sender_id END AS other_id
                              FROM messages WHERE listing_type=? AND listing_id=? AND (sender_id=? OR receiver_id=?)
                              ORDER BY id DESC LIMIT 1""",
                           (user["id"], listing_type, listing_id, user["id"], user["id"])).fetchone()
        other_id = row["other_id"] if row else None
    if not other_id:
        flash("Пока нет переписки по этому объявлению.", "error")
        return redirect(url_for("messages_list"))
    # Отмечаем входящие сообщения прочитанными.
    db().execute("UPDATE messages SET read=1 WHERE listing_type=? AND listing_id=? AND receiver_id=? AND read=0",
                 (listing_type, listing_id, user["id"]))
    db().commit()
    messages = db().execute("""SELECT m.*, u.name AS sender_name, u.avatar AS sender_avatar
                               FROM messages m JOIN users u ON u.id=m.sender_id
                               WHERE m.listing_type=? AND m.listing_id=?
                                 AND ((m.sender_id=? AND m.receiver_id=?) OR (m.sender_id=? AND m.receiver_id=?))
                               ORDER BY m.id ASC""",
                            (listing_type, listing_id, user["id"], other_id, other_id, user["id"])).fetchall()
    other = db().execute("SELECT id, name, avatar FROM users WHERE id=?", (other_id,)).fetchone()
    return render_template("thread.html", messages=messages, other=other,
                           listing_type=listing_type, listing_id=listing_id,
                           title=listing_title(listing_type, listing_id), tab="messages")


@app.route("/messages/<listing_type>/<int:listing_id>/send", methods=["POST"])
@login_required
def send_message(listing_type, listing_id):
    user = current_user()
    owner_id = listing_owner(listing_type, listing_id)
    if not owner_id:
        abort(404)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Сообщение не может быть пустым.", "error")
        return redirect(url_for("message_thread", listing_type=listing_type, listing_id=listing_id))
    receiver_id = owner_id if owner_id != user["id"] else None
    if not receiver_id:
        # Владелец отвечает последнему собеседнику.
        row = db().execute("""SELECT CASE WHEN sender_id=? THEN receiver_id ELSE sender_id END AS other_id
                              FROM messages WHERE listing_type=? AND listing_id=? AND (sender_id=? OR receiver_id=?)
                              ORDER BY id DESC LIMIT 1""",
                           (user["id"], listing_type, listing_id, user["id"], user["id"])).fetchone()
        receiver_id = row["other_id"] if row else None
    if not receiver_id:
        flash("Не удалось определить собеседника.", "error")
        return redirect(url_for("messages_list"))
    db().execute("INSERT INTO messages (listing_type, listing_id, sender_id, receiver_id, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (listing_type, listing_id, user["id"], receiver_id, body, utcnow()))
    db().commit()
    return redirect(url_for("message_thread", listing_type=listing_type, listing_id=listing_id))


init_db()
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
