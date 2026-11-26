from uuid import UUID
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from bridge.agent_catalog import register_connector
from bridge.avihub_client import PURPOSE
from bridge.models import AviHubAgent


class Command(BaseCommand):
    help = "Cadastra o AviHUB no catálogo da instituição."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=UUID, required=True)
        parser.add_argument("--origin", required=True)
        parser.add_argument("--if-present", action="store_true")

    @transaction.atomic
    def handle(self, *args, **opts):
        origin = opts["origin"].rstrip("/")
        url = urlsplit(origin)
        if (url.scheme != "https" or not url.hostname or origin != "https://" + url.hostname
                or any(c.isspace() for c in origin)):
            raise CommandError("Informe somente a origem HTTPS oficial do painel AviHUB.")
        tenant = opts["tenant"]
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM hub_tenancy.institutions WHERE tenant_id=%s", [tenant])
            if not cursor.fetchone():
                if opts["if_present"]:
                    return
                raise CommandError("Instituição indisponível.")
        agent, created = AviHubAgent.objects.get_or_create(tenant=tenant, defaults={"origin":origin})
        if agent.origin != origin:
            raise CommandError("Já existe um AviHUB com outra origem. As sessões vinculadas pertencem à origem original.")
        with connection.cursor() as cursor:
            register_connector(cursor, tenant, agent.pk, "AviHUB", "avihub", PURPOSE)
        if created:
            self.stdout.write("Agente AviHUB publicado na instituição.")
