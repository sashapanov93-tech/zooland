"""Production entry point for ZooLand (served by Gunicorn)."""

from app import app


if __name__ == "__main__":
    raise SystemExit("Запускайте production через Gunicorn: gunicorn wsgi:app")
