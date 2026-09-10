# Production-развёртывание ZooLand

Эта инструкция рассчитана на один Linux-сервер с Nginx, systemd, Gunicorn,
SQLite и локальным `clamd`. Команды и конфиги используют тестовое имя
`zooland.example`: до запуска замените его своим доменом. Секреты и реальные
реквизиты оператора персональных данных в Git не добавляйте.

## 1. Схема каталогов и системный пользователь

Рекомендуемая схема отделяет код от изменяемых данных:

```text
/opt/zooland/current/                 checkout приложения, только чтение для сервиса
/opt/zooland/venv/                    заново созданное виртуальное окружение
/etc/zooland/zooland.env              production-переменные, не в Git
/var/lib/zooland/zooland.db           рабочая SQLite-база
/var/lib/zooland/uploads/             загруженные и очищенные WebP
/var/backups/zooland/                 резервные копии, не в Git
```

Создайте отдельного пользователя без shell-входа и каталоги с закрытыми
правами:

```bash
sudo useradd --system --home /var/lib/zooland --shell /usr/sbin/nologin zooland
sudo install -d -o root -g root -m 0755 /opt/zooland
sudo install -d -o root -g zooland -m 0750 /etc/zooland
sudo install -d -o zooland -g zooland -m 0710 /var/lib/zooland
sudo install -d -o zooland -g zooland -m 0750 /var/lib/zooland/uploads
sudo install -d -o zooland -g zooland -m 0700 /var/backups/zooland
```

Разместите checkout в `/opt/zooland/current`. Код и шаблоны должны
принадлежать администратору развёртывания, а не пользователю `zooland`; сервису
нужен только доступ на чтение. Не копируйте локальные `.env`, `zooland.db`,
`venv` и содержимое `static/uploads` на сервер как часть релиза.

Приложение пишет изображения прямо в каталог из
`ZOOLAND_UPLOAD_FOLDER=/var/lib/zooland/uploads`; bind-mount или symlink внутри
checkout не нужен. Nginx отдаёт этот каталог только по URL `/uploads/`, а
исторический `/static/uploads/` в production закрыт.

Чтобы worker Nginx мог читать только опубликованные изображения, не получая
доступа к базе, добавьте его пользователя в группу `zooland`. На Debian/Ubuntu
это обычно `www-data`:

```bash
sudo usermod -a -G zooland www-data
sudo find /var/lib/zooland/uploads -type f -name '*.webp' -exec chown zooland:zooland {} +
sudo find /var/lib/zooland/uploads -type f -name '*.webp' -exec chmod 0640 {} +
sudo systemctl restart nginx
```

Корень `/var/lib/zooland` остаётся `0710`, каталог uploads — `0750`,
опубликованные WebP — `0640`, база — `0600`. Поэтому группа Nginx может пройти
к изображениям, но не может просматривать корень данных или читать
`zooland.db`. После первой тестовой загрузки обязательно проверьте режим нового
файла: он тоже должен быть `0640`.

## 2. Новое виртуальное окружение

Виртуальное окружение нельзя переносить с другого компьютера или из другого
пути: shebang в его исполняемых файлах содержит абсолютный путь. Для каждого
сервера создайте его заново и запускайте Gunicorn как модуль Python:

```bash
sudo python3 -m venv /opt/zooland/venv
sudo /opt/zooland/venv/bin/python -m pip install --upgrade pip
sudo /opt/zooland/venv/bin/python -m pip install -r /opt/zooland/current/requirements.txt
sudo /opt/zooland/venv/bin/python -m pip check
```

Не используйте `python app.py` в production.

## 3. Production-переменные и fail-closed проверки

Скопируйте `.env.example` не в checkout, а в закрытый systemd-файл:

```bash
sudo install -o root -g zooland -m 0640 /opt/zooland/current/.env.example /etc/zooland/zooland.env
sudoedit /etc/zooland/zooland.env
```

У файла формат `KEY=value` без `export`. Значения с пробелами заключайте в
двойные кавычки. Обязательно замените домен и задайте:

- `ZOOLAND_ENV=production` и `ZOOLAND_DEBUG=0`;
- абсолютный `ZOOLAND_DB_PATH=/var/lib/zooland/zooland.db`;
- абсолютный `ZOOLAND_UPLOAD_FOLDER=/var/lib/zooland/uploads`;
- HTTPS-адрес в `ZOOLAND_PUBLIC_BASE_URL` и точные домены в
  `ZOOLAND_TRUSTED_HOSTS`;
- `ZOOLAND_TRUST_PROXY_HOPS=1`, только если между клиентом и Gunicorn ровно
  один доверенный Nginx;
- новый `ZOOLAND_SECRET_KEY`, созданный командой `openssl rand -hex 48`;
- реальные `ZOOLAND_OPERATOR_NAME`, `ZOOLAND_OPERATOR_ADDRESS` и
  `ZOOLAND_PRIVACY_EMAIL`;
- рабочие SMTP-параметры с `ZOOLAND_SMTP_TLS=1`;
- `ZOOLAND_REQUIRE_ANTIVIRUS=1` и адрес доступного `clamd`.

Production entry point `wsgi.py` принудительно включает production-режим.
Импорт приложения завершится ошибкой при шаблонном/коротком ключе, HTTP вместо
HTTPS, пустых реквизитах оператора или неготовой SMTP-конфигурации. Проверка
ClamAV работает fail-closed на каждой загрузке: если сканер недоступен, файл
отклоняется. Systemd дополнительно останавливает старт, если постоянный каталог
изображений отсутствует или недоступен на запись.

Не передавайте порт Gunicorn наружу. Он слушает только `127.0.0.1:5000`, а
снаружи должны быть открыты лишь HTTPS/HTTP (для перенаправления и выпуска
сертификата) и административный SSH.

## 4. Инициализация и миграции SQLite отдельным процессом

Схема не изменяется при импорте `wsgi.py`. До первого запуска и после каждого
релиза, содержащего миграции, выполните одноразовую команду
`flask --app app init-db`. Не запускайте её параллельно с Gunicorn.

Установите предоставленные unit-файлы:

```bash
sudo install -o root -g root -m 0644 deploy/zooland.service /etc/systemd/system/zooland.service
sudo install -o root -g root -m 0644 deploy/zooland-init-db.service /etc/systemd/system/zooland-init-db.service
sudo install -o root -g root -m 0644 deploy/zooland-notifications.service /etc/systemd/system/zooland-notifications.service
sudo install -o root -g root -m 0644 deploy/zooland-notifications.timer /etc/systemd/system/zooland-notifications.timer
sudo systemctl daemon-reload
```

Для существующего сайта сначала остановите timer. Команда `is-active` должна
показать `inactive`; если worker ещё имеет статус `active`, дождитесь его
завершения и проверьте снова. Только после этого остановите веб-процесс,
запустите одноразовую миграцию и убедитесь, что она завершилась успешно:

```bash
sudo systemctl stop zooland-notifications.timer
sudo systemctl is-active zooland-notifications.service
sudo systemctl stop zooland.service
sudo systemctl restart zooland-init-db.service
sudo systemctl status zooland-init-db.service --no-pager
sudo journalctl -u zooland-init-db.service -n 50 --no-pager
```

Ожидаемая запись: `Схема ZooLand готова: /var/lib/zooland/zooland.db`. Unit
имеет тип `oneshot`, не включается в автозапуск и выполняет только:

```bash
/opt/zooland/venv/bin/python -m flask --app app init-db
```

Проверьте владельца и права созданной базы:

```bash
sudo chown zooland:zooland /var/lib/zooland/zooland.db
sudo chmod 0600 /var/lib/zooland/zooland.db
sudo -u zooland sqlite3 /var/lib/zooland/zooland.db 'PRAGMA integrity_check;'
sudo -u zooland sqlite3 /var/lib/zooland/zooland.db 'PRAGMA foreign_key_check;'
```

Ответ проверки должен быть ровно `ok`. Пустой вывод команды
`PRAGMA foreign_key_check;` означает, что нарушений внешних ключей нет.

## 5. Gunicorn под systemd

После успешной миграции включите сервис:

```bash
sudo systemctl enable --now zooland.service
sudo systemctl enable --now zooland-notifications.timer
sudo systemctl status zooland.service --no-pager
sudo systemctl status zooland-notifications.timer --no-pager
sudo systemctl list-timers zooland-notifications.timer --no-pager
sudo journalctl -u zooland.service -n 100 --no-pager
```

`deploy/zooland.service` запускает
`/opt/zooland/venv/bin/python -m gunicorn`, выставляет `UMask=0077`, не даёт
процессу изменять checkout и разрешает запись только в `/var/lib/zooland`.
Stdout, error log и сокращённый access log Gunicorn отправляются в journald.
Access log намеренно не содержит URI: одноразовый токен смены пароля находится
в пути запроса и не должен попадать в журнал. Полный журнал обычных запросов
остаётся у Nginx в `/var/log/nginx/zooland.access.log`, но не содержит query
string, Referer или cookie; для `/password/reset/...` он полностью отключён.
Просмотр в реальном времени:

```bash
sudo journalctl -u zooland.service -f
```

Срок хранения журналов настройте до запуска: IP-адреса из
`/var/log/nginx/zooland.access.log` должны автоматически удаляться не позднее
чем через 30 дней (проверьте действующий `/etc/logrotate.d/nginx`), а лимиты
journald — в `journald.conf`. Дистрибутивные настройки отличаются, поэтому
проверьте фактическую ротацию командой
`sudo logrotate --debug /etc/logrotate.conf`. Не копируйте журналы, токены восстановления и
пользовательские данные в Git.

### Очередь почтовых уведомлений

Уведомления по сохранённым поискам не отправляются из Gunicorn. Timer раз в
минуту запускает отдельный oneshot-процесс от пользователя `zooland`:

```bash
/opt/zooland/venv/bin/python -m flask --app app process-notifications --max-jobs 10
```

Один и тот же systemd-service не запускается параллельно: если предыдущая
партия ещё работает, новый запуск не создаёт второй процесс. На каждую
итерацию берётся не более 10 задач, поэтому очередь обрабатывается небольшими
порциями и не отнимает ресурсы у сайта. Ограничение выполнения — 15 минут;
неуспешная партия останется в SQLite для следующего запуска.

Для внепланового запуска используйте
`sudo systemctl start zooland-notifications.service`. Не вызывайте CLI-команду
напрямую одновременно с timer: два независимых процесса могут выбрать одну и
ту же задачу.

Проверка расписания, результатов и размера очереди:

```bash
sudo systemctl list-timers zooland-notifications.timer --no-pager
sudo journalctl -u zooland-notifications.service -n 100 --no-pager
sudo -u zooland sqlite3 /var/lib/zooland/zooland.db "SELECT COUNT(*) AS pending FROM saved_search_jobs WHERE processed_at IS NULL AND attempts < 10;"
sudo -u zooland sqlite3 /var/lib/zooland/zooland.db "SELECT COUNT(*) AS exhausted FROM saved_search_jobs WHERE processed_at IS NULL AND attempts >= 10;"
```

Ненулевой `exhausted` требует проверки SMTP и журнала; такие задачи уже не
берутся автоматически после десяти неудачных попыток. Не меняйте их состояние
вручную, пока не выяснена причина, иначе пользователи могут получить повторные
письма.

## 6. Nginx, HTTPS и ограничения на входе

Файл `deploy/nginx-zooland.conf` содержит:

- безусловный redirect с HTTP на HTTPS;
- TLS reverse proxy на `127.0.0.1:5000`;
- `client_max_body_size 50m`, совпадающий с общим лимитом Flask;
- корректные `Host`, `X-Real-IP`, `X-Forwarded-For`,
  `X-Forwarded-Proto` и `X-Forwarded-Host`;
- общий rate limit, более строгий limit для входа/регистрации/восстановления
  пароля и лимит одновременных соединений;
- отдельную обработку `/static/`, чтобы загрузка CSS и картинок не тратила
  лимит динамических запросов;
- прямую отдачу `/uploads/` из постоянного каталога и безусловный запрет
  старого `/static/uploads/`.

Сначала получите TLS-сертификат для своего домена. Затем замените все
`zooland.example` в конфиге, установите его в каталог, подключаемый внутри
`http { ... }`, и проверьте синтаксис:

```bash
sudo install -o root -g root -m 0644 deploy/nginx-zooland.conf /etc/nginx/conf.d/zooland.conf
sudo nginx -t
sudo systemctl reload nginx
```

Не запускайте конфиг с тестовым доменом или путями к несуществующему
сертификату. После первого успешного HTTPS-запуска можно включать HSTS; пример
уже содержит заголовок без `includeSubDomains`, чтобы случайно не сломать
другие поддомены.

Если перед Nginx появится CDN или ещё один proxy, нельзя просто доверять всем
`X-Forwarded-*`. Сначала ограничьте trusted proxy IP на уровне Nginx, затем
пересчитайте `ZOOLAND_TRUST_PROXY_HOPS`. Иначе атакующий сможет подменить IP и
обойти ограничения.

## 7. SMTP smoke-test

До открытия регистрации проверьте весь пользовательский путь, а не только TCP
соединение с почтовым сервером:

1. Убедитесь, что сервис стартует с production-конфигурацией без SMTP-ошибок.
2. В браузере запросите восстановление пароля для отдельного контролируемого
   тестового аккаунта.
3. Убедитесь, что письмо действительно пришло, поле From корректно, а ссылка
   начинается с настроенного `https://...`.
4. Откройте ссылку один раз и убедитесь, что повторное использование уже
   невозможно. Не помещайте сам токен в тикет, чат или журнал проверки.
5. Проверьте отсутствие ошибок отправки:

```bash
sudo journalctl -u zooland.service --since '10 minutes ago' --no-pager
sudo journalctl -u zooland-notifications.service --since '10 minutes ago' --no-pager
```

Для Gmail используйте пароль приложения, а не основной пароль аккаунта. Если
исходящие соединения сервера ограничены firewall, разрешите SMTP только до
выбранного провайдера и его TLS-порта.

Отдельно проверьте очередь: создайте сохранённый поиск в контролируемом
аккаунте, одобрите одно подходящее тестовое объявление, дождитесь запуска timer
и убедитесь, что пришло ровно одно письмо, а `pending` не вырос.

## 8. Резервная копия, проверка и восстановление

База и изображения образуют один набор данных. Самый простой согласованный
backup делается в коротком окне обслуживания: остановите приложение, сохраните
SQLite через встроенную `.backup`, затем упакуйте uploads. Не копируйте
работающую SQLite-базу обычным `cp`.

Пример ручной копии (замените метку времени одним одинаковым значением в двух
именах). После остановки timer команда `is-active` должна показать
`inactive`; если worker активен, дождитесь окончания отправки и только затем
останавливайте Gunicorn и копируйте данные:

```bash
sudo systemctl stop zooland-notifications.timer
sudo systemctl is-active zooland-notifications.service
sudo systemctl stop zooland.service
sudo -u zooland sqlite3 /var/lib/zooland/zooland.db ".backup '/var/backups/zooland/zooland-YYYYMMDDTHHMMSSZ.db'"
sudo -u zooland sqlite3 /var/backups/zooland/zooland-YYYYMMDDTHHMMSSZ.db 'PRAGMA integrity_check;'
sudo -u zooland tar -C /var/lib/zooland -czf /var/backups/zooland/uploads-YYYYMMDDTHHMMSSZ.tar.gz uploads
sudo chmod 0600 /var/backups/zooland/zooland-YYYYMMDDTHHMMSSZ.db /var/backups/zooland/uploads-YYYYMMDDTHHMMSSZ.tar.gz
sudo systemctl start zooland.service
sudo systemctl start zooland-notifications.timer
```

Если `integrity_check` не вернул ровно `ok`, не считайте этот backup рабочим и
не удаляйте предыдущую проверенную копию. Храните зашифрованную копию на другом
хосте/носителе и регулярно проверяйте восстановление. Периодически запускайте
на текущей базе `PRAGMA quick_check;`.

Восстановление сначала репетируйте вне production. Для реального
восстановления также сначала остановите timer и дождитесь неактивного worker:

```bash
sudo systemctl stop zooland-notifications.timer
sudo systemctl is-active zooland-notifications.service
sudo systemctl stop zooland.service
sudo -u zooland sqlite3 /var/backups/zooland/zooland-YYYYMMDDTHHMMSSZ.db 'PRAGMA integrity_check;'
sudo mv /var/lib/zooland/zooland.db /var/lib/zooland/zooland.db.before-restore
sudo install -o zooland -g zooland -m 0600 /var/backups/zooland/zooland-YYYYMMDDTHHMMSSZ.db /var/lib/zooland/zooland.db
sudo mv /var/lib/zooland/uploads /var/lib/zooland/uploads.before-restore
sudo install -d -o zooland -g zooland -m 0750 /var/lib/zooland/uploads
sudo -u zooland tar -C /var/lib/zooland -xzf /var/backups/zooland/uploads-YYYYMMDDTHHMMSSZ.tar.gz
sudo find /var/lib/zooland/uploads -type f -name '*.webp' -exec chmod 0640 {} +
sudo systemctl restart zooland-init-db.service
sudo systemctl start zooland.service
sudo systemctl start zooland-notifications.timer
```

После восстановления проверьте `PRAGMA integrity_check;`, главную страницу,
вход, отображение нескольких изображений и отправку письма. Каталоги
`.before-restore` оставлены намеренно для быстрого отката; удаляйте их только
после подтверждения целостности и отдельного проверенного backup.

## 9. Финальная проверка релиза

Перед публикацией:

```bash
sudo systemctl is-active zooland.service nginx
sudo systemctl is-active zooland-notifications.timer
curl --fail --silent --show-error https://zooland.example/healthz
curl --fail --silent --show-error --head https://zooland.example/
curl --fail --silent --show-error --head https://zooland.example/register
curl --fail --silent --show-error --head https://zooland.example/terms
curl --fail --silent --show-error --head https://zooland.example/privacy
sudo -u zooland sqlite3 /var/lib/zooland/zooland.db 'PRAGMA quick_check;'
```

Проверьте также регистрацию без двух согласий (она должна быть отклонена),
регистрацию с обоими согласиями, загрузку безопасного изображения, отклонение
слишком большого/неподдерживаемого файла, восстановление пароля и доступ к
сайту с телефона. Автоматические тесты и чистый `git status` проверяются до
копирования релиза на сервер.
