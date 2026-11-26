import os
from pathlib import Path

SECRET_KEY = os.environ["PORTAL_SESSION_SECRET"]
ALLOWED_HOSTS = ["hubia.psxt.io"]
ROOT_URLCONF = "views"
LANGUAGE_CODE = "pt-br"
LANGUAGES = [("pt-br", "Português"), ("en", "English"), ("es", "Español")]
LANGUAGE_COOKIE_NAME = "hubia_language"
LOCALE_PATHS = [Path(__file__).parent / "locale"]
USE_TZ = True
TIME_ZONE = "America/Sao_Paulo"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "oauth2_provider", "bridge",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "languages.AccountLocaleMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "DIRS": [Path(__file__).parent / "templates"],
    "OPTIONS": {"context_processors": ["django.template.context_processors.request", "profile_photos.photo_context"]},
}]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql", "NAME": "hubia",
    "HOST": "postgres", "USER": os.environ.get("PGUSER", "hub_portal"),
    "PASSWORD": os.environ["PGPASSWORD"],
    "OPTIONS": {"options": "-c search_path=hub_accounts,pg_catalog", "connect_timeout": 5},
}}
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_NAME = "__Host-hubia"
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 8 * 3600
CSRF_COOKIE_NAME = "__Host-hubia-csrf"
LOGIN_URL = "/conta/entrar/"
LOGIN_REDIRECT_URL = "/conta/"
LOGOUT_REDIRECT_URL = "/"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
]

OAUTH2_PROVIDER = {
    "PKCE_REQUIRED": True,
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
    "ROTATE_REFRESH_TOKEN": True,
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "SCOPES": {"hub.read": "Consultar catálogo, tarefas e notas do espaço.",
               "hub.mail.send": "Enviar e-mails pela sua caixa e editar a assinatura dos envios por IA quando você autorizar",
               "hub.notify": "Enviar avisos externos nos canais e destinos autorizados do espaço",
               "hub.reply": "Responder às tarefas da conexão", "offline_access": "Renovar a conexão"},
    "DEFAULT_SCOPES": ["hub.read"],
}

EMAIL_BACKEND = "bridge.platform_email.PlatformBackend"
PASSWORD_RESET_TIMEOUT = 3600

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID","")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET","")
