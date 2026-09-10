# ZooLand

ZooLand — площадка объявлений о животных, услугах и кормах для России и
Грузии. Интерфейс переключается между русским, английским и грузинским
языками. Приложение написано на Flask и использует SQLite.

## Локальная разработка

Требуется Python 3.13.

```bash
python3.13 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Создайте локальный файл настроек, который не должен попадать в Git:

```bash
cp .env.example .env
openssl rand -hex 48
```

В `.env` укажите полученный ключ в `ZOOLAND_SECRET_KEY` и настройте локальный
режим:

```dotenv
ZOOLAND_ENV=development
ZOOLAND_DEBUG=0
ZOOLAND_DB_PATH=./zooland.db
ZOOLAND_UPLOAD_FOLDER=./static/uploads
ZOOLAND_PUBLIC_BASE_URL=http://127.0.0.1:5000
ZOOLAND_TRUSTED_HOSTS=127.0.0.1,localhost
ZOOLAND_TRUST_PROXY_HOPS=0
ZOOLAND_REQUIRE_ANTIVIRUS=0
```

Создайте или обновите схему базы и запустите сайт:

```bash
python -m flask --app app init-db
python app.py
```

Встроенный сервер Flask предназначен только для локальной разработки. Не
открывайте его в интернет и не используйте в production. Инструкция по
production-запуску через Nginx, systemd и Gunicorn находится в
[`DEPLOYMENT.md`](DEPLOYMENT.md).

## Проверки

```bash
python -m pip check
python -m py_compile app.py catalog.py image_security.py services.py wsgi.py
python -m unittest discover -v -p 'test_*.py'
```

Тесты создают изолированную временную SQLite-базу и не должны изменять рабочий
`zooland.db`.

Перед публикацией не добавляйте в Git `.env`, базы данных, загруженные файлы,
резервные копии, ключи, сертификаты и пользовательские данные.

Источники и условия использования справочника стран и городов описаны в
[`DATA_SOURCES.md`](DATA_SOURCES.md).
