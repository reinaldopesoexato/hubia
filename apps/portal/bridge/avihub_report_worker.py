"""Run native AviHUB report requests and delivery outside the portal request."""
import os

import django


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    django.setup()
    from .avihub_reports import run

    run()


if __name__ == "__main__":
    main()
