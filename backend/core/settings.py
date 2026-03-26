"""
Django settings for HelloAgain backend.
"""
import os

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env FIRST so all os.getenv() calls below work correctly
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-*@dfj01=#wsdypza-k!r2s+wtq3w6al!$(=!zb2waoxv_nm-=f",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "True") == "True"

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if not DEBUG else ["*"]

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

_default_allowed_hosts = ["localhost", "127.0.0.1", "0.0.0.0", "10.0.2.2", "testserver"]
_extra_allowed_hosts = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",")
    if host.strip()
]
ALLOWED_HOSTS = _default_allowed_hosts + _extra_allowed_hosts


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    # Platform
    "voice_gateway",
    # Agent apps
    "apps.agent_core",
    "apps.agent_sessions",
    "apps.agent_plans",
    "apps.agent_policy",
    "apps.agent_executors",
    "apps.device_bridge",
    "apps.audit_log",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'corsheaders',
    # Project apps
    'recommendations',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'core.middleware.ApiJsonErrorsMiddleware',
    'corsheaders.middleware.CorsMiddleware',          # must be before CommonMiddleware
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"

# ── Database ─────────────────────────────────────────────────────────────────

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# ── Cache (Redis) ─────────────────────────────────────────────────────────────
# Used for transient execution state: current step, session lock, etc.

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "helloagain-default",
    },
    "sessions": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "helloagain-sessions",
    },
}

# Override with Redis when available
if os.environ.get("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
            },
        },
        "sessions": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
            },
        },
    }

# ── Celery ────────────────────────────────────────────────────────────────────

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/1")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_TRACK_STARTED = True

# ── DRF ──────────────────────────────────────────────────────────────────────

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "EXCEPTION_HANDLER": "rest_framework.views.exception_handler",
}

# ── Password validation ───────────────────────────────────────────────────────

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── i18n ─────────────────────────────────────────────────────────────────────

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── LLM configuration ─────────────────────────────────────────────────────────
#
# Default provider: transformers (Qwen/Qwen2.5-14B-Instruct loaded locally).
# Switch provider by setting LLM_PROVIDER env var — no code changes needed:
#
#   transformers  local HuggingFace model (default)
#                 Requires: pip install transformers torch accelerate
#                 Optional 4-bit quant: pip install bitsandbytes
#                 First run downloads ~28 GB from HuggingFace Hub.
#
#   ollama        local Ollama server  (no API key, fast iteration)
#                 Start with: ollama run qwen2.5:14b
#
#   groq          Groq cloud inference (fastest, free tier available)
#                 Set LLM_API_KEY to your key from console.groq.com
#
#   openai        OpenAI or any OpenAI-compatible endpoint (LM Studio, vLLM…)
#                 Set LLM_API_KEY + optionally LLM_BASE_URL
#
# Quick-start examples:
#   python manage.py runserver                                         # transformers, downloads on first use
#   LLM_PROVIDER=ollama python manage.py runserver                     # needs: ollama run qwen2.5:14b
#   LLM_PROVIDER=groq LLM_API_KEY=gsk_xxx python manage.py runserver

LLM_PROVIDER  = os.environ.get("LLM_PROVIDER",  "transformers")
LLM_MODEL     = os.environ.get("LLM_MODEL",     "Qwen/Qwen2.5-14B-Instruct")
LLM_API_KEY   = os.environ.get("LLM_API_KEY",   "")
LLM_BASE_URL  = os.environ.get("LLM_BASE_URL",  "")   # empty → use provider default
LLM_TIMEOUT   = int(os.environ.get("LLM_TIMEOUT", "60"))  # 60s for local model generation

# ── Logging ───────────────────────────────────────────────────────────────────

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "apps": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
    },
}
STATIC_URL = 'static/'

# ---------------------------------------------------------------------------
# CORS (allow all origins in development)
# ---------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = True

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
    'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser'],
}
