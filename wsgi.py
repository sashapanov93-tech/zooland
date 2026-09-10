"""Production entry point for ZooLand (served by Gunicorn)."""

import os


# Production entry point всегда включает строгие cookie, HTTPS/config checks
# и fail-closed проверку загрузок, даже если в локальном .env остался dev mode.
os.environ["ZOOLAND_ENV"] = "production"

from app import app


if __name__ == "__main__":
    raise SystemExit("Запускайте production через Gunicorn: gunicorn wsgi:app")
