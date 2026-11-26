from django.db import connection
from django.db.models import Q
from django.db.models.expressions import RawSQL
from .models import Ticket


def register_connector(cursor, tenant, agent, name, kind, description):
    cursor.execute("""INSERT INTO hub_tenancy.agents(id,tenant_id,name,kind,description)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""", [agent,tenant,name,kind,description])


def assigned(tenant, space, kind):
    with connection.cursor() as cursor:
        cursor.execute("""SELECT EXISTS(SELECT FROM hub_tenancy.workspace_agents
            WHERE tenant_id=%s AND workspace_id=%s AND kind=%s AND active)""", [tenant,space,kind])
        return cursor.fetchone()[0]

def visible_tasks(link):
    catalog = RawSQL("SELECT id FROM hub_tenancy.workspace_agents WHERE tenant_id=%s AND workspace_id=%s AND active",
                     [link.tenant,link.space])
    return Ticket.objects.filter(link=link).filter(Q(notice_agent__isnull=True) | Q(notice_agent__in=catalog))
