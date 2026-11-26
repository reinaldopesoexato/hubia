from datetime import datetime, time, timedelta
from heapq import nlargest

from django.utils import timezone
from django.db.models import Count, Q, Sum
from bridge import mail_access
from bridge.models import Link, OutboundEmail, Ticket


def accessible_tenants(cursor):
    cursor.execute("SELECT tenant_id FROM hub_tenancy.memberships ORDER BY created_at, tenant_id")
    for (tenant,) in cursor.fetchall():
        cursor.execute("SELECT set_config('hub.tenant_id', %s, true)", [str(tenant)])
        yield tenant


DEFAULT_PURPOSE = {
    "knowledge": "Pesquisa informações na base de conhecimento deste espaço.",
    "external": "Abre o portal externo vinculado ao agente.",
    "connector": "Encaminha sua consulta ao serviço conectado a este espaço.",
    "notifications": "Envia avisos pelos canais habilitados para a sua conta.",
}


ROLE_NAMES = {"personal_owner": "Titular", "institution_owner": "Responsável institucional",
              "institution_admin": "Administrador institucional", "member": "Membro", "viewer": "Leitor"}


def space_guide(cursor, tenant, allowed_spaces=None):
    """Read the current user's visible workspaces, optionally narrowed to an integration's grant."""
    sql = """
        SELECT w.id AS space_id,w.name AS workspace,i.name AS organization,m.role,
               a.id AS agent_id,a.name AS agent_name,a.kind,a.description
        FROM hub_tenancy.workspaces w
        JOIN hub_tenancy.memberships m ON m.tenant_id=w.tenant_id AND m.active
            AND m.user_id=current_setting('hub.principal_id')::uuid
        LEFT JOIN hub_tenancy.institutions i ON i.tenant_id=w.tenant_id
        LEFT JOIN hub_tenancy.workspace_agents a ON (a.tenant_id,a.workspace_id)=(w.tenant_id,w.id) AND a.active
        WHERE w.tenant_id=%s"""
    params = [tenant]
    if allowed_spaces is not None:
        sql += " AND w.id=ANY(%s::uuid[])"
        params.append(list(allowed_spaces))
    cursor.execute(sql + " ORDER BY lower(w.name),w.id,lower(a.name),a.id", params)
    groups = {}
    for space, workspace, organization, role, agent, name, kind, description in cursor.fetchall():
        if space not in groups:
            groups[space] = {"tenant":str(tenant), "space":str(space), "workspace":workspace,
                             "organization":organization or "Espaço pessoal", "role":ROLE_NAMES.get(role,role),
                             "agents":[]}
        if agent is not None:
            groups[space]["agents"].append({"id":str(agent), "name":name,
                "purpose":description.strip() or DEFAULT_PURPOSE.get(kind, "Finalidade ainda não cadastrada.")})
    return list(groups.values())


def agent_directory(cursor, include_empty=False):
    groups = [group for tenant in accessible_tenants(cursor) for group in space_guide(cursor,tenant)
              if include_empty or group["agents"]]
    return sorted(groups, key=lambda group: (group["organization"].casefold(), group["workspace"].casefold()))


MAIL_STATES = {
    "registrado": ("running", "Registrado"),
    "aceito_pelo_smtp": ("done", "Aceito pelo servidor de e-mail"),
    "recusado": ("failed", "Recusado"),
    "bloqueado": ("failed", "Bloqueado"),
    "incerto": ("unconfirmed", "Sem confirmação"),
}


def connection_summary(account_id, spaces, start, end, internal_mail):
    organizations = {space["tenant"]: space["org"] or "Espaço pessoal" for space in spaces}
    links = Link.objects.filter(user_id=account_id, tenant__in=organizations)
    links = mail_access.visible_links(links, internal_mail)
    usage_period = Q(daily_usage__day__gte=start.date(), daily_usage__day__lt=end.date())
    rows = list(links.values("id", "tenant", "name", "protocol", "email_only").annotate(
        queries=Sum("daily_usage__queries", filter=usage_period, default=0)))
    # Remote peers answer tasks sent by the user. Polling the same task is not another query.
    peers = [row["id"] for row in rows if row["protocol"] == "peer"]
    answered = dict(Ticket.objects.filter(link_id__in=peers, notice_agent__isnull=True, state="answered",
                                          created__gte=start, created__lt=end).values("link_id")
                    .annotate(total=Count("pk")).values_list("link_id", "total"))
    for row in rows:
        row["organization"] = organizations[row["tenant"]]
        if row["protocol"] == "peer":
            row.update(protocol_label="A2A externo", queries=answered.get(row["id"], 0))
        else:
            row["protocol_label"] = "MCP de e-mail" if row["email_only"] else row["protocol"].upper()
    return sorted(rows, key=lambda row: (-row["queries"], row["name"].casefold(), row["organization"], row["id"]))


def tenant_workspaces(cursor, tenant):
    cursor.execute("""
            SELECT w.id, w.name, i.name,
                   (SELECT count(*) FROM hub_tenancy.workspace_agents a
                    WHERE a.tenant_id = w.tenant_id AND a.workspace_id = w.id AND a.active)
            FROM hub_tenancy.workspaces w
            LEFT JOIN hub_tenancy.institutions i ON i.tenant_id = w.tenant_id
            WHERE w.tenant_id = %s ORDER BY lower(w.name), w.id
        """, [tenant])
    return [{"id": space, "tenant": tenant, "name": name, "org": org, "agents": agents}
            for space, name, org, agents in cursor.fetchall()]


def overview(cursor, principal, period, account_id):
    today = timezone.localdate()
    start_date = today - timedelta(days=period - 1)
    zone = timezone.get_current_timezone()
    start = datetime.combine(start_date, time.min, zone)
    end = datetime.combine(today + timedelta(days=1), time.min, zone)
    days = {start_date + timedelta(days=i): 0 for i in range(period)}
    institutions = {}
    spaces = []
    latest = []
    agent_count = 0
    for tenant in accessible_tenants(cursor):
        accessible = tenant_workspaces(cursor, tenant)
        if not accessible:
            continue
        spaces.extend(accessible)
        agent_count += sum(space["agents"] for space in accessible)
        if accessible[0]["org"] is not None:
            institutions[tenant] = accessible[0]["org"]
        cursor.execute("""
            SELECT (r.created_at AT TIME ZONE %s)::date AS day, count(*)
            FROM hub_tenancy.agent_runs r
            WHERE r.tenant_id = %s AND r.user_id = %s AND r.created_at >= %s AND r.created_at < %s
            GROUP BY day
        """, [str(zone), tenant, principal, start, end])
        for day, count in cursor.fetchall():
            days[day] += count
        cursor.execute("""
            SELECT r.created_at, w.name, a.name, r.status, w.id, a.id, assigned.active,
                   r.status = 'running' AND r.created_at < now() - interval '2 minutes' AS unconfirmed
            FROM hub_tenancy.agent_runs r
            JOIN hub_tenancy.workspaces w ON (w.tenant_id,w.id) = (r.tenant_id,r.workspace_id)
            LEFT JOIN hub_tenancy.agents a ON (a.tenant_id,a.id)=(r.tenant_id,r.agent_id)
            LEFT JOIN hub_tenancy.workspace_agents assigned ON (assigned.tenant_id,assigned.workspace_id,assigned.id)=
                (r.tenant_id,r.workspace_id,r.agent_id)
            WHERE r.tenant_id = %s AND r.user_id = %s AND r.created_at >= %s AND r.created_at < %s
            ORDER BY r.created_at DESC, r.id DESC LIMIT 5
        """, [tenant, principal, start, end])
        for stamp, workspace, agent, status, wid, aid, active, unconfirmed in cursor.fetchall():
            latest.append({"time": stamp, "workspace": workspace, "agent": agent or "Agente desativado",
                           "status": "unconfirmed" if unconfirmed else status, "tenant": tenant,
                           "space_id": wid, "agent_id": aid if active else None})
    internal_mail = mail_access.INSTITUTION in institutions and mail_access.allowed(cursor, mail_access.INSTITUTION)
    emails = OutboundEmail.objects.filter(
        link__user_id=account_id, link__tenant__in=[mail_access.INSTITUTION] if internal_mail else [],
        created_at__gte=start, created_at__lt=end,
    ).order_by("-created_at", "-pk").values_list("created_at", "link__tenant", "link__name", "recipient", "state")[:5]
    for stamp, tenant, client, recipient, state in emails:
        status, label = MAIL_STATES.get(state, MAIL_STATES["incerto"])
        latest.append({"time":stamp, "icon":"mail", "agent":f"E-mail para {recipient}",
                       "workspace":f"{institutions[tenant]} · {client}", "status":status, "label":label})

    peak = max(days.values()) or 1
    bars = [{"day": day, "count": count, "height": round(100 * count / peak, 2),
             "label": period == 7 or index in (0, 7, 14, 21, 29)}
            for index, (day, count) in enumerate(days.items())]
    connections = connection_summary(account_id, spaces, start, end, internal_mail)
    return {"internal_mail": internal_mail, "connections": connections, "connection_queries": sum(row["queries"] for row in connections),
            "spaces": spaces, "institutions": len(institutions), "agents": agent_count,
            "runs": sum(days.values()), "bars": bars, "period": period, "today": today,
            "latest": nlargest(5, latest, key=lambda event: event["time"])}
