from django.apps import AppConfig


class BridgeConfig(AppConfig):
    name = "bridge"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in
        from languages import retain_language
        user_logged_in.connect(retain_language, dispatch_uid="hubia.retain_language")
