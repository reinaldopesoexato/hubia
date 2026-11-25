import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta
from uuid import uuid4

from django.utils import timezone
from model_provider import cipher
from .models import Delivery, Subscription, Ticket
from .network import public_json, RemoteError

EVENT = "hub.task.created"
EMPTY_SCHEMA = {"type":"object","properties":{},"additionalProperties":False}
EVENT_INFO = {"name":EVENT,"description":"Uma tarefa foi disponibilizada na sua conexão privada do HUB.",
              "delivery":["webhook"],"inputSchema":EMPTY_SCHEMA,
              "payloadSchema":{"type":"object","properties":{"task_id":{"type":"string"}},
                               "required":["task_id"],"additionalProperties":False}}


def post_signed(url, secret, sub_id, event_id, body, *, empty=False):
    if not secret.startswith("whsec_"):
        raise ValueError("Chave de assinatura inválida.")
    key = base64.b64decode(secret[6:],validate=True)
    if not 16 <= len(key) <= 128:
        raise ValueError("Tamanho de chave inválido.")
    raw = json.dumps(body,ensure_ascii=False,separators=(",",":")).encode()
    stamp = str(int(time.time()))
    signature = hmac.digest(key, f"{event_id}.{stamp}.".encode()+raw,"sha256")
    return public_json(url,raw,{"Content-Type":"application/json","webhook-id":event_id,
        "webhook-timestamp":stamp,"webhook-signature":"v1,"+base64.b64encode(signature).decode(),
        "X-MCP-Subscription-Id":sub_id},empty=empty)


def subscribe(link, params, remove=False):
    delivery = params.get("delivery")
    if (params.get("name") != EVENT or params.get("arguments",{}) != {} or not isinstance(delivery,dict)
            or delivery.get("mode")!="webhook" or not isinstance(delivery.get("url"),str)
            or len(delivery["url"])>2048):
        raise ValueError("Assinatura inválida.")
    url = delivery["url"]
    sub_id = hashlib.sha256(f"{link.pk}:{EVENT}:{url}".encode()).hexdigest()
    existing = Subscription.objects.filter(link=link,pk=sub_id).first()
    if remove:
        if existing:
            existing.delete()
        return {}
    secret = delivery.get("secret")
    if not isinstance(secret,str) or len(secret)>256:
        raise ValueError("Chave de assinatura inválida.")
    stored = json.dumps([url,secret]).encode()
    now = timezone.now()
    if not existing or existing.expires <= now or not hmac.compare_digest(cipher().decrypt(bytes(existing.encrypted)),stored):
        nonce = secrets.token_urlsafe(32)
        result = post_signed(url,secret,sub_id,str(uuid4()),{"type":"verification","challenge":nonce})
        echo = result.get("challenge")
        if not isinstance(echo,str) or not hmac.compare_digest(echo.encode(),nonce.encode()):
            raise RemoteError("Verificação do destinatário falhou.")
    expires = now+timedelta(days=1)
    sub, _ = Subscription.objects.update_or_create(pk=sub_id,link=link,defaults={"encrypted":cipher().encrypt(stored),"expires":expires})
    # Uma assinatura nova ou renovada também pode receber avisos publicados enquanto estava offline.
    notices = Ticket.objects.filter(link=link, notice_agent__isnull=False, state="pending")
    missing = notices.exclude(delivery__subscription=sub)
    Delivery.objects.bulk_create([Delivery(ticket_id=i,subscription=sub,due=now)
                                  for i in missing.values_list("pk", flat=True)], ignore_conflicts=True)
    Delivery.objects.filter(subscription=sub,ticket__in=notices,state="stopped").update(state="pending",due=now)
    return {"id":sub_id,"refreshBefore":expires.isoformat(),"cursor":None,"truncated":False}
