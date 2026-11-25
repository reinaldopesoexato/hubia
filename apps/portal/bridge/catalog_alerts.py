"""Avisos do catálogo na transação que torna o agente disponível."""
from uuid import uuid4

from django.urls import reverse
from django.utils import timezone
from .models import CatalogAlerts, Delivery, Link, Subscription, Ticket


def announce(tenant, agent, name, *, space=None, library_access=False):
    from .access import ORIGIN

    enabled = CatalogAlerts.objects.filter(tenant=tenant)
    if space is not None:
        enabled = enabled.filter(space=space)
    links = Link.objects.filter(tenant=tenant, space__in=enabled.values("space"), email_only=False, user__is_active=True)
    now = timezone.now()
    event = str(uuid4())
    for link in links:
        url = reverse("use_agent", args=[tenant, link.space, agent])
        label = "Leitura de notas disponível" if library_access else "Agente disponível"
        ticket = Ticket.objects.create(link=link, request_id=event, notice_agent=agent,
            notice_due=now if link.protocol=="peer" else None,
            question=f"{label} no catálogo deste espaço: {name}\n{ORIGIN}{url}")
        if link.protocol == "mcp":
            subs = Subscription.objects.filter(link=link, expires__gt=now)
            Delivery.objects.bulk_create([Delivery(ticket=ticket,subscription=sub,due=now) for sub in subs])


def cancel_pending(tenant, space, agent=None):
    pending = Ticket.objects.filter(link__tenant=tenant,link__space=space,notice_agent__isnull=False,state="pending")
    if agent is not None:
        pending = pending.filter(notice_agent=agent)
    pending.update(state="stopped",notice_due=None)
