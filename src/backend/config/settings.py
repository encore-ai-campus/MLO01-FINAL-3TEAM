import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .database import database_config

BASE_DIR = Path(__file__).resolve().parent.parent


def secret(name, filename):
    value = os.environ.get(name)
    if value:
        return value
    path = Path("/run/secrets") / filename
    return path.read_text().strip() if path.is_file() else ""


SECRET_KEY = secret("DJANGO_SECRET_KEY", "django_secret_key")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY or mount /run/secrets/django_secret_key")
DEBUG = False
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
DATABASES = {"default": database_config(os.getenv("DATABASE_URL", "dbname=voc"))}
if not DATABASES["default"]["PASSWORD"]:
    DATABASES["default"]["PASSWORD"] = secret("PGPASSWORD", "postgres_password")

INSTALLED_APPS = ["django.contrib.staticfiles", "apps.accounts", "apps.tickets", "apps.workspace",
                  "apps.collaboration", "apps.analytics", "apps.preferences", "apps.followup"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "config.http.DeskMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
}]
CSRF_FAILURE_VIEW = "config.http.csrf_failure"
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
USE_TZ = True
TIME_ZONE = "Asia/Seoul"
LANGUAGE_CODE = "ko"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
