from django.db import connection
from django.utils.crypto import salted_hmac


def allow_auth_attempt(namespace, identifier, limit):
    key = salted_hmac(namespace, identifier).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM login_limits WHERE started_at < now() - interval '5 minutes'")
        cursor.execute("""
            INSERT INTO login_limits (source) VALUES (%s)
            ON CONFLICT (source) DO UPDATE SET attempts = login_limits.attempts + 1
            WHERE login_limits.attempts < %s RETURNING attempts
        """, [key, limit])
        return cursor.fetchone() is not None
