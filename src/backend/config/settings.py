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
VOC_POC_RANDOM_ASSIGNMENT = os.getenv("VOC_POC_RANDOM_ASSIGNMENT") == "1"
VOC_ASSIGNMENT_WORKER_TOKEN = secret("VOC_ASSIGNMENT_WORKER_TOKEN", "voc_assignment_worker_token")
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "")
MLFLOW_TRACKING_TOKEN = secret("MLFLOW_TRACKING_TOKEN", "mlflow_tracking_token")
ARGO_WORKFLOWS_URL = os.getenv("ARGO_WORKFLOWS_URL", "")
ARGO_WORKFLOWS_TOKEN = os.getenv("ARGO_WORKFLOWS_TOKEN", "")
ARGO_WORKFLOWS_TOKEN_FILE = os.getenv("ARGO_WORKFLOWS_TOKEN_FILE", "/run/secrets/argo_workflows_token")
ARGO_WORKFLOWS_NAMESPACE = os.getenv("ARGO_WORKFLOWS_NAMESPACE", "argo")
ARGO_WORKFLOWS_UI_URL = os.getenv("ARGO_WORKFLOWS_UI_URL", "")
MLFLOW_UI_URL = os.getenv("MLFLOW_UI_URL", "")
MINIO_CONSOLE_URL = os.getenv("MINIO_CONSOLE_URL", "")
VOC_GRAFANA_URL = os.getenv("VOC_GRAFANA_URL", "")
VOC_TRAINING_MODEL_NAME = os.getenv("VOC_TRAINING_MODEL_NAME", "voc-bert")
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
DATABASES = {"default": database_config(os.getenv("DATABASE_URL", "dbname=voc"))}
if not DATABASES["default"]["PASSWORD"]:
    DATABASES["default"]["PASSWORD"] = secret("PGPASSWORD", "postgres_password")

INSTALLED_APPS = ["django.contrib.staticfiles", "apps.accounts", "apps.tickets", "apps.workspace",
                  "apps.collaboration", "apps.analytics", "apps.preferences", "apps.followup", "apps.assignment"]
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
