"""Django settings for HelloAgain backend."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env so os.environ values are available below.
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-*@dfj01=#wsdypza-k!r2s+wtq3w6al!$(=!zb2waoxv_nm-=f",
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get("DJANGO_DEBUG", "True") == "True"
ALLOWED_HOSTS = ["*"] if DEBUG else [
    host for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if host
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'corsheaders',
    # Platform
    'controller',
    'meetup',
    "apps.accounts",
    # Agent apps
    'apps.agent_core',
    'apps.agent_sessions',
    'apps.agent_plans',
    'apps.agent_policy',
    'apps.agent_executors',
    'apps.device_bridge',
    'apps.audit_log',
    # GAT Engine
    'recommendations',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'core.middleware.ApiJsonErrorsMiddleware',
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

# Database
USE_SQLITE = os.environ.get("USE_SQLITE", "False") == "True"

if USE_SQLITE:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                "timeout": int(os.environ.get("SQLITE_TIMEOUT", "20")),
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "app_db"),
            "USER": os.environ.get("POSTGRES_USER", "app_user"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "change_me"),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": int(os.environ.get("POSTGRES_PORT", "5432")),
            "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "60")),
        }
    }

# ── Cache (Redis) ─────────────────────────────────────────────────────────────
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

if os.environ.get("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        },
        "sessions": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        },
    }

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/1")
CELERY_RESULT_BACKEND = os.environ.get(
    "CELERY_RESULT_BACKEND", "redis://localhost:6379/1"
)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_TRACK_STARTED = True

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}

# ── CORS ──────────────────────────────────────────────────────────────────────
CORS_ALLOW_ALL_ORIGINS = True

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# i18n
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── LLM configuration ─────────────────────────────────────────────────────────
#
# Default provider: transformers (Qwen/Qwen3-14B loaded locally).
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
LLM_MODEL     = os.environ.get("LLM_MODEL",     "Qwen/Qwen3-14B")
LLM_API_KEY   = os.environ.get("LLM_API_KEY",   "")
LLM_BASE_URL  = os.environ.get("LLM_BASE_URL",  "")   # empty → use provider default
LLM_TIMEOUT   = int(os.environ.get("LLM_TIMEOUT", "60"))  # 60s for local model generation
LOCAL_LLM_PROVIDER = os.environ.get("LOCAL_LLM_PROVIDER", "transformers")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "Qwen/Qwen3-14B")
LOCAL_LLM_API_KEY = os.environ.get("LOCAL_LLM_API_KEY", "")
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "")
LOCAL_LLM_TIMEOUT = int(os.environ.get("LOCAL_LLM_TIMEOUT", str(LLM_TIMEOUT)))
OPENAI_LLM_MODEL = os.environ.get("OPENAI_LLM_MODEL", "gpt-5-mini")
OPENAI_LLM_API_KEY = os.environ.get(
    "OPENAI_LLM_API_KEY",
    os.environ.get("OPENAI_API_KEY", os.environ.get("LLM_API_KEY", "")),
)
OPENAI_LLM_BASE_URL = os.environ.get(
    "OPENAI_LLM_BASE_URL",
    os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
)
OPENAI_LLM_TIMEOUT = int(os.environ.get("OPENAI_LLM_TIMEOUT", str(LLM_TIMEOUT)))
LLM_TOKEN_BUDGET_SYSTEM_PROMPT = int(os.environ.get("LLM_TOKEN_BUDGET_SYSTEM_PROMPT", "2000"))
LLM_TOKEN_BUDGET_SCREEN_STATE  = int(os.environ.get("LLM_TOKEN_BUDGET_SCREEN_STATE", "6000"))
LLM_TOKEN_BUDGET_HISTORY       = int(os.environ.get("LLM_TOKEN_BUDGET_HISTORY", "2000"))
LLM_TOKEN_BUDGET_RESPONSE      = int(os.environ.get("LLM_TOKEN_BUDGET_RESPONSE", "500"))
LLM_MAX_CONTEXT                = int(os.environ.get("LLM_MAX_CONTEXT", "12000"))

# ── Logging ───────────────────────────────────────────────────────────────────
CORS_ALLOW_ALL_ORIGINS = True

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "transformers")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-14B-Instruct")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "60"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {module} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
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
