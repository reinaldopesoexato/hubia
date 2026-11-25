import os
from pathlib import Path

SECRET_KEY = os.environ["PORTAL_SESSION_SECRET"]
ALLOWED_HOSTS = ["hubia.psxt.io"]
ROOT_URLCONF = "views"
LANGUAGE_CODE = "pt-br"
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
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "DIRS": [Path(__file__).parent / "templates"],
    "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
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
    "SCOPES": {"hub.read": "Consultar catálogo, tarefas e notas do espaço. Ler sua caixa de entrada quando você autorizar a conexão a ler o e-mail.",
               "hub.mail.send": "Enviar e-mails pela sua caixa e editar a assinatura dos envios por IA quando você autorizar",
               "hub.notify": "Enviar avisos externos nos canais e destinos autorizados do espaço",
               "hub.reply": "Responder às tarefas da conexão", "offline_access": "Renovar a conexão"},
    "DEFAULT_SCOPES": ["hub.read"],
}
