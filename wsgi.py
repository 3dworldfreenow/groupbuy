"""Production WSGI entry point for Gunicorn or Waitress.

    gunicorn -c deploy/gunicorn.conf.py wsgi:app
    waitress-serve --listen=127.0.0.1:8000 wsgi:app
"""
from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402

app = create_app()
