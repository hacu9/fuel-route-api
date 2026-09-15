"""Django settings for the fuel route API."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["*"]),
    DJANGO_SECRET_KEY=(str, "insecure-dev-key-change-me"),
    DATABASE_URL=(str, f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
    OSRM_BASE_URL=(str, "https://router.project-osrm.org"),
    OSRM_TIMEOUT_SECONDS=(float, 25.0),
    NOMINATIM_BASE_URL=(str, "https://nominatim.openstreetmap.org"),
    NOMINATIM_USER_AGENT=(str, "fuel-route-api/1.0 (backend assessment)"),
    NOMINATIM_TIMEOUT_SECONDS=(float, 10.0),
    VEHICLE_MAX_RANGE_MILES=(float, 500.0),
    VEHICLE_MILES_PER_GALLON=(float, 10.0),
    MAX_DETOUR_MILES=(float, 15.0),
    MIN_STOP_SPACING_MILES=(float, 10.0),
    ORIGIN_FALLBACK_RADIUS_MILES=(float, 75.0),
    ROUTE_SAMPLE_STRIDE_METERS=(float, 250.0),
)

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "fuelroute",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves the collected static files in production. In DEBUG the
    # staticfiles app serves them and no collectstatic run has happened yet.
    *([] if DEBUG else ["whitenoise.middleware.WhiteNoiseMiddleware"]),
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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": env.db("DATABASE_URL")}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # The manifest backend requires a collectstatic run, which is pointless
        # during development and in the test suite.
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "EXCEPTION_HANDLER": "fuelroute.exceptions.api_exception_handler",
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuel-route-cache",
        "TIMEOUT": 60 * 60 * 24,
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# ---------------------------------------------------------------------------
# Domain configuration. The assessment fixes the vehicle numbers, but they stay
# configurable so the same service can price a different vehicle.
# ---------------------------------------------------------------------------
FUEL_ROUTE = {
    "MAX_RANGE_MILES": env("VEHICLE_MAX_RANGE_MILES"),
    "MILES_PER_GALLON": env("VEHICLE_MILES_PER_GALLON"),
    "MAX_DETOUR_MILES": env("MAX_DETOUR_MILES"),
    "MIN_STOP_SPACING_MILES": env("MIN_STOP_SPACING_MILES"),
    "ORIGIN_FALLBACK_RADIUS_MILES": env("ORIGIN_FALLBACK_RADIUS_MILES"),
    "ROUTE_SAMPLE_STRIDE_METERS": env("ROUTE_SAMPLE_STRIDE_METERS"),
    "OSRM_BASE_URL": env("OSRM_BASE_URL").rstrip("/"),
    "OSRM_TIMEOUT_SECONDS": env("OSRM_TIMEOUT_SECONDS"),
    "NOMINATIM_BASE_URL": env("NOMINATIM_BASE_URL").rstrip("/"),
    "NOMINATIM_USER_AGENT": env("NOMINATIM_USER_AGENT"),
    "NOMINATIM_TIMEOUT_SECONDS": env("NOMINATIM_TIMEOUT_SECONDS"),
}
