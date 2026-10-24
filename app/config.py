"""Configuration classes. All secrets come from environment variables."""
import logging
import os
import secrets
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


def _bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url(default):
    url = os.environ.get("DATABASE_URL") or default
    # Some hosting providers still hand out the legacy postgres:// scheme.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


class BaseConfig:
    ENV_NAME = "base"
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = _database_url(
        "sqlite:///" + os.path.join(INSTANCE_DIR, "groupbuy.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    APP_NAME = os.environ.get("APP_NAME", "FreshCircle")
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Asia/Kolkata")
    DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "INR")
    CURRENCY_SYMBOL = os.environ.get("CURRENCY_SYMBOL", "₹")
    CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "support@example.com")

    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER") or os.path.join(INSTANCE_DIR, "uploads")
    MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "5"))
    # Total request size; a few files per request are allowed.
    MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024 * 4

    PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_DURATION = timedelta(days=14)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 6

    BEHIND_PROXY = _bool("BEHIND_PROXY")
    RATELIMIT_ENABLED = True
    LOGIN_MAX_FAILURES = 5
    LOGIN_LOCK_MINUTES = 15

    @classmethod
    def validate(cls):
        """Hook for environment-specific checks."""


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False

    @classmethod
    def validate(cls):
        if not cls.SECRET_KEY:
            # A per-process random key is acceptable for local development only.
            cls.SECRET_KEY = secrets.token_hex(32)
            logging.getLogger(__name__).warning(
                "SECRET_KEY is not set; using a temporary random key (sessions reset on restart)."
            )


class TestingConfig(BaseConfig):
    ENV_NAME = "testing"
    TESTING = True
    SECRET_KEY = "test-secret-key-not-for-production"
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    RATELIMIT_ENABLED = False


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", True)
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    PREFERRED_URL_SCHEME = "https"

    @classmethod
    def validate(cls):
        problems = []
        if not cls.SECRET_KEY or len(cls.SECRET_KEY) < 32:
            problems.append("SECRET_KEY must be set to a random value of at least 32 characters")
        if not os.environ.get("DATABASE_URL"):
            problems.append("DATABASE_URL must be set (PostgreSQL recommended)")
        if problems:
            raise RuntimeError("Invalid production configuration: " + "; ".join(problems))


CONFIGS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name=None):
    name = name or os.environ.get("FLASK_ENV") or os.environ.get("APP_ENV") or "development"
    return CONFIGS.get(name, DevelopmentConfig)
