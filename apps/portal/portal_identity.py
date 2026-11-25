def bind_account(cursor, user):
    cursor.execute("SELECT hub_admin.portal_principal(%s)", [str(user.pk)])
    principal = cursor.fetchone()[0]
    if principal is None:
        return None, False
    cursor.execute("SELECT set_config('hub.principal_id', %s, true)", [str(principal)])
    cursor.execute("SELECT platform_owner FROM hub_identity.users WHERE id = %s", [principal])
    return principal, cursor.fetchone()[0]
