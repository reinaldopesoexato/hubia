from datetime import datetime, time, timedelta
from heapq import nlargest

from django.utils import timezone
from bridge import library


def accessible_tenants(cursor):
    cursor.execute("SELECT tenant_id FROM hub_tenancy.memberships ORDER BY created_at, tenant_id")
    for (tenant,) in cursor.fetchall():
        cursor.execute("SELECT set_config('hub.tenant_id', %s, true)", [str(tenant)])
        yield tenant


DEFAULT_PURPOSE = {
    "guide": "Orienta sobre os agentes disponíveis e o seu acesso ao espaço.",
    "knowledge": "Pesquisa informações na base de conhecimento deste espaço.",
    "external": "Abre o portal externo vinculado ao agente.",
    "connector": "Encaminha sua consulta ao serviço conectado a este espaço.",
    "notifications": "Envia avisos pelos canais habilitados para a sua conta.",
}


def agent_directory(cursor):
    groups = []
    for tenant in accessible_tenants(cursor):
        cursor.execute("""
            SELECT i.name, w.id, w.name, a.id, a.name, a.kind, a.description
            FROM hub_tenancy.workspaces w
            JOIN LATERAL (
                SELECT id,name,kind,description FROM hub_tenancy.agents
                WHERE tenant_id=w.tenant_id AND workspace_id=w.id AND active
                UNION ALL
                SELECT id,name,'library',%s FROM bridge_notelibrary WHERE connection_id=w.tenant_id
            ) a ON true
            LEFT JOIN hub_tenancy.institutions i ON i.tenant_id = w.tenant_id
            WHERE w.tenant_id = %s
            ORDER BY lower(w.name), w.id, lower(a.name), a.id
        """, [library.PURPOSE,tenant])
        for org, space, workspace, agent, name, kind, description in cursor.fetchall():
            if not groups or (tenant, space) != (groups[-1]["tenant"], groups[-1]["space"]):
                groups.append({"tenant":tenant, "space":space, "organization":org or "Espaço pessoal",
                               "workspace":workspace, "agents":[]})
            groups[-1]["agents"].append({"id":agent, "name":name,
                "purpose":description.strip() or DEFAULT_PURPOSE.get(kind, "Finalidade ainda não cadastrada.")})
    return sorted(groups, key=lambda group: (group["organization"].casefold(), group["workspace"].casefold()))


def overview(cursor, principal, period):
    today = timezone.localdate()
    start_date = today - timedelta(days=period - 1)
    zone = timezone.get_current_timezone()
    start = datetime.combine(start_date, time.min, zone)
    end = datetime.combine(today + timedelta(days=1), time.min, zone)
    days = {start_date + timedelta(days=i): 0 for i in range(period)}
    institutions = set()
    spaces = []
    latest = []
    agent_count = 0
    for tenant in accessible_tenants(cursor):
        cursor.execute("""
            SELECT w.id, w.name, i.name,
                   (SELECT count(*) FROM hub_tenancy.agents a
                    WHERE a.tenant_id = w.tenant_id AND a.workspace_id = w.id AND a.active)
            FROM hub_tenancy.workspaces w
            LEFT JOIN hub_tenancy.institutions i ON i.tenant_id = w.tenant_id
            WHERE w.tenant_id = %s ORDER BY lower(w.name), w.id
        """, [tenant])
        accessible = cursor.fetchall()
        if not accessible:
            continue
        has_library = bool(library.library_agent(tenant))
        agent_count += int(has_library)
        for space, name, org, agents in accessible:
            spaces.append({"id": space, "tenant": tenant, "name": name, "org": org, "agents": agents + int(has_library)})
            agent_count += agents
            if org is not None:
                institutions.add(tenant)
        cursor.execute("""
            SELECT (r.created_at AT TIME ZONE %s)::date AS day, count(*)
            FROM hub_tenancy.agent_runs r
            WHERE r.tenant_id = %s AND r.user_id = %s AND r.created_at >= %s AND r.created_at < %s
            GROUP BY day
        """, [str(zone), tenant, principal, start, end])
        for day, count in cursor.fetchall():
            days[day] += count
        cursor.execute("""
            SELECT r.created_at, w.name, a.name, r.status, w.id, a.id, a.active,
                   r.status = 'running' AND r.created_at < now() - interval '2 minutes' AS unconfirmed
            FROM hub_tenancy.agent_runs r
            JOIN hub_tenancy.workspaces w ON (w.tenant_id,w.id) = (r.tenant_id,r.workspace_id)
            LEFT JOIN hub_tenancy.agents a ON (a.tenant_id,a.workspace_id,a.id) =
                (r.tenant_id,r.workspace_id,r.agent_id)
            WHERE r.tenant_id = %s AND r.user_id = %s AND r.created_at >= %s AND r.created_at < %s
            ORDER BY r.created_at DESC, r.id DESC LIMIT 5
        """, [tenant, principal, start, end])
        for stamp, workspace, agent, status, wid, aid, active, unconfirmed in cursor.fetchall():
            latest.append({"time": stamp, "workspace": workspace, "agent": agent or "Agente desativado",
                           "status": "unconfirmed" if unconfirmed else status, "tenant": tenant,
                           "space_id": wid, "agent_id": aid if active else None})
    peak = max(days.values()) or 1
    bars = [{"day": day, "count": count, "height": round(100 * count / peak, 2),
             "label": period == 7 or index in (0, 7, 14, 21, 29)}
            for index, (day, count) in enumerate(days.items())]
    return {"spaces": spaces, "institutions": len(institutions), "agents": agent_count,
            "runs": sum(days.values()), "bars": bars, "period": period, "today": today,
            "latest": nlargest(5, latest, key=lambda event: event["time"])}
