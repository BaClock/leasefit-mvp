import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DEBUG", "0") == "1"
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set SECRET_KEY (see .env.example)")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
CSRF_TRUSTED_ORIGINS = list(
    filter(None, os.getenv("CSRF_TRUSTED_ORIGINS", "").split(","))
)
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "leasefit",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "leasefit.context.navigation",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
if os.getenv("DB_ENGINE") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "local.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "leasefit"),
            "USER": os.getenv("POSTGRES_USER", "leasefit"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
            "HOST": os.getenv("POSTGRES_HOST", "db"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Asia/Novosibirsk"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login"
LOGIN_REDIRECT_URL = "/app/requests"
LOGOUT_REDIRECT_URL = "/login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "0") == "1"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# Only enable behind the trusted reverse proxy; the application port must stay private.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "1") == "1"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@example.invalid")
DATA_UPLOAD_MAX_MEMORY_SIZE = 23 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 21 * 1024 * 1024
PDF_FONT = os.getenv("PDF_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "")
