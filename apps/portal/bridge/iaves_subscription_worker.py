"""Start the scheduled IAves notification worker."""
import os

import django


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    django.setup()
    from .iaves_subscriptions import run

    run()


if __name__ == "__main__":
    main()
