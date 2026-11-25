"""Entrega notificações duráveis. Um retry usa sempre o mesmo ID de evento."""
import json
import time
from datetime import timedelta
import django

django.setup()
from cryptography.fernet import InvalidToken
from django.db import close_old_connections, transaction
from django.http import Http404
from django.utils import timezone
from model_provider import cipher
from .access import context
from .events import EVENT, post_signed
from .models import Delivery, Ticket
from .a2a import query_peer
from .network import RemoteError


@transaction.atomic
def deliver_one(ticket_id=None):
    now = timezone.now()
    queue = Delivery.objects.filter(ticket_id=ticket_id) if ticket_id else Delivery.objects.all()
    item = (queue.select_related("subscription__link__user","ticket")
            .select_for_update(skip_locked=True).filter(state="pending",due__lte=now).order_by("due").first())
    if not item:
        return False
    sub = item.subscription
    item.attempts += 1
    try:
        if sub.expires<=now or item.ticket.state!="pending" or item.ticket.link_id!=sub.link_id:
            raise Http404
        context(sub.link)
        url,secret = json.loads(cipher().decrypt(bytes(sub.encrypted)))
        event = {"eventId":str(item.pk),"name":EVENT,"timestamp":item.ticket.created.isoformat(),
                 "data":{"task_id":str(item.ticket_id)},"cursor":None}
        post_signed(url,secret,sub.pk,str(item.pk),event,empty=True)
        item.state = "sent"
    except (Http404, InvalidToken, ValueError):
        item.state = "stopped"
    except RemoteError:
        if item.attempts>=6:
            item.state = "failed"
        item.due = now + timedelta(seconds=min(600, 5**item.attempts))
    item.save(update_fields=["attempts","due","state"])
    return True


@transaction.atomic
def deliver_peer_notice(ticket_id=None):
    now = timezone.now()
    queue = Ticket.objects.filter(link__protocol="peer", notice_agent__isnull=False,
                                  state="pending", notice_due__lte=now)
    if ticket_id:
        queue = queue.filter(pk=ticket_id)
    item = queue.select_related("link__user").select_for_update(skip_locked=True).order_by("notice_due").first()
    if not item:
        return False
    item.notice_attempts += 1
    try:
        context(item.link, writing=True)
        config = json.loads(cipher().decrypt(bytes(item.link.peer_config)))
        state, answer, remote = query_peer(config, item.question, item.request_id)
        item.state, item.answer, item.remote_id = state, answer[:16000], remote
        item.notice_due = None
    except (Http404, InvalidToken, ValueError, KeyError, TypeError, AttributeError):
        item.state, item.notice_due = "stopped", None
    except RemoteError:
        if item.notice_attempts >= 6:
            item.state, item.notice_due = "failed", None
        else:
            item.notice_due = now + timedelta(seconds=min(600, 5**item.notice_attempts))
    item.save(update_fields=["notice_due","notice_attempts","state","answer","remote_id"])
    return True


if __name__=="__main__":
    while True:
        close_old_connections()
        callback_sent = deliver_one()
        peer_sent = deliver_peer_notice()
        if not callback_sent and not peer_sent:
            time.sleep(3)
