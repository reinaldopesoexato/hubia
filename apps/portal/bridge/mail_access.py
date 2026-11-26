from uuid import UUID

from django.db import connection, transaction
from django.db.models import Q
from portal_identity import bind_account


INSTITUTION = UUID("e7eb1e21-00c7-4abd-8ada-641ce20a91b3")


def allowed(cursor, tenant):
    """Check the principal already bound to this transaction."""
    if tenant != INSTITUTION:
        return False
    cursor.execute("SELECT set_config('hub.tenant_id', %s, true)", [str(tenant)])
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM hub_tenancy.memberships m
            JOIN hub_tenancy.tenants t ON t.id=m.tenant_id
            WHERE m.tenant_id=%s AND m.active AND m.internal_mail AND t.status='ACTIVE'
              AND m.user_id=nullif(current_setting('hub.principal_id', true),'')::uuid
        )""", [tenant])
    return cursor.fetchone()[0]


def for_link(link):
    if str(link.tenant) != str(INSTITUTION) or not link.user.is_active:
        return False
    with transaction.atomic(), connection.cursor() as cursor:
        principal, _ = bind_account(cursor, link.user)
        return bool(principal and allowed(cursor, INSTITUTION))


def visible_links(links, eligible):
    if eligible:
        return links.filter(Q(email_only=False) | Q(tenant=INSTITUTION))
    return links.filter(email_only=False)
