# Production-запуск ZooLand

## 1. Переменные окружения

Скопируйте `.env.example` в `.env` и задайте собственные значения. Файл `.env`
содержит пароли и ключ приложения: он уже исключён из Git и не должен попадать
в репозиторий, архивы или скриншоты.

Минимальные production-настройки:

```dotenv
ZOOLAND_ENV=production
ZOOLAND_DEBUG=0
ZOOLAND_HOST=127.0.0.1
ZOOLAND_PORT=5000
ZOOLAND_PUBLIC_BASE_URL=https://zooland.example
ZOOLAND_TRUSTED_HOSTS=zooland.example,www.zooland.example
ZOOLAND_SECRET_KEY=длинный_случайный_секрет_не_менее_32_символов
ZOOLAND_TRUST_PROXY_HOPS=1
ZOOLAND_REQUIRE_ANTIVIRUS=1
ZOOLAND_CLAMAV_HOST=127.0.0.1
ZOOLAND_CLAMAV_PORT=3310
```

Задайте также SMTP-переменные из `.env.example`. Для Gmail используйте пароль
приложения, а не пароль от обычного входа. Доступ к `.env` должен быть только у
пользователя, под которым работает сервис (например, `chmod 600 .env`).

## 2. Установка и Gunicorn

```bash
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/gunicorn --workers 3 --bind 127.0.0.1:5000 wsgi:app
```

Не используйте `python app.py` как production-сервер. Его debug-режим берётся
только из `ZOOLAND_DEBUG` и принудительно выключен при `ZOOLAND_ENV=production`.

## 3. HTTPS reverse proxy

Разместите Nginx (или аналогичный proxy) перед Gunicorn: он принимает HTTPS,
передаёт запросы на `127.0.0.1:5000` и выставляет `Host`, `X-Forwarded-For` и
`X-Forwarded-Proto`. В таком случае оставьте `ZOOLAND_TRUST_PROXY_HOPS=1`.
Если proxy нет — установите `ZOOLAND_TRUST_PROXY_HOPS=0`.

Пример location для Nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:5000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

TLS-сертификат должен быть установлен на самом proxy. В production приложение
принимает только HTTPS-значение `ZOOLAND_PUBLIC_BASE_URL`; это адрес для писем
и ссылок восстановления пароля.

## 4. Антивирус для загрузок

В production ZooLand намеренно не принимает изображения, пока не доступен
ClamAV daemon (`clamd`) по указанному host/port. Это fail-closed защита: при
недоступности сканера загрузка отклоняется, а не сохраняется без проверки.
После проверки сервер всё равно полностью декодирует картинку и публикует
только новую WebP-копию без метаданных; исходный файл никогда не отдаётся
посетителям. Для локальной разработки без clamd явно задайте
`ZOOLAND_REQUIRE_ANTIVIRUS=0` только в локальном `.env`.
