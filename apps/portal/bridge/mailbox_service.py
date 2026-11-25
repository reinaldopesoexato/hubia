import hashlib
import json
from uuid import UUID

from cryptography.fernet import InvalidToken
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.http import Http404
from model_provider import cipher
from . import mail_protocol
from .models import Mailbox, OutboundEmail
from .signature import signature_parts


def authorized(link):
    return Mailbox.objects.filter(provider_id=link.tenant, provider__enabled=True,
                                  user_id=link.user_id, links=link.pk)


def reference(box, validity, purpose, value):
    return signing.dumps([str(box.pk), validity, value], salt="hub.email." + purpose)


def resolve(box, validity, purpose, token):
    try:
        owner, epoch, uid = signing.loads(token, salt="hub.email." + purpose)
        if owner == str(box.pk) and epoch == validity and type(uid) is int and uid >= 0:
            return uid
    except (ValueError, TypeError, signing.BadSignature):
        pass
    raise ValueError("Referência inválida para esta caixa. Consulte a lista novamente sem marcador.")


def query(link, action, args):
    box = authorized(link).first()
    if box is None:
        raise ValueError("Acesso à caixa revogado.")
    try:
        password = cipher().decrypt(bytes(box.password_cipher)).decode()
    except InvalidToken:
        raise ValueError("Reconecte a caixa no portal.") from None
    with mail_protocol.inbox(box.address, password) as (client, count):
        validity = mail_protocol.number(client, "UIDVALIDITY")
        if action == "ler_email":
            return mail_protocol.read(client, resolve(box, validity, "mensagem", args["mensagem_id"]))
        latest = mail_protocol.number(client, "UIDNEXT") - 1
        after = resolve(box, validity, "cursor", args["marcador"]) if "marcador" in args else None
        messages, end = mail_protocol.page(client, count, latest, after)
        return {"mensagens":[{**item, "mensagem_id":reference(box, validity, "mensagem", uid)}
                             for uid, item in messages],
                "marcador":reference(box, validity, "cursor", end), "ha_mais":end < latest}


def sendable(link):
    return Mailbox.objects.filter(provider_id=link.tenant, provider__enabled=True,
                                  user_id=link.user_id, send_links=link.pk)


def signature(link, args):
    boxes = sendable(link)
    if "html" in args:
        html, _ = signature_parts(args["html"])
        if not boxes.update(signature_html=html):
            raise ValueError("Esta conexão não tem acesso à assinatura.")
    else:
        html = boxes.values_list("signature_html", flat=True).first()
        if html is None:
            raise ValueError("Esta conexão não tem acesso à assinatura.")
    return {"html":html}


def send(link, args):
    box = sendable(link).first()
    if box is None:
        raise ValueError("Envio de e-mail não autorizado para esta conexão.")
    recipient, subject, body = (args[key] for key in ("destinatario", "assunto", "mensagem"))
    try:
        subject.encode("utf-8")
        body.encode("utf-8")
        validate_email(recipient)
        request_id = UUID(args["request_id"])
    except (ValueError, ValidationError):
        raise ValueError("Informe um único destinatário e um request_id UUID válido.") from None
    if (not recipient.isascii() or len(recipient) > 254 or any(ord(c) < 33 or ord(c) == 127 for c in recipient)
            or not subject.strip() or len(subject) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in subject)
            or not body.strip() or len(body) > 8000 or "\0" in body):
        raise ValueError("Confira o endereço, o assunto (até 200 caracteres) e a mensagem (até 8.000).")
    digest = hashlib.sha256(json.dumps([str(box.pk), recipient, subject, body]).encode()).hexdigest()
    run, created = OutboundEmail.objects.get_or_create(link=link, request_id=request_id,
                                                       defaults={"fingerprint":digest})
    if run.fingerprint != digest:
        raise ValueError("Este request_id já foi usado para outro e-mail.")
    receipt = {"request_id":str(request_id), "estado":run.state}
    if created:
        # Persist the reservation before SMTP. An ambiguous delivery must never be retried automatically.
        def deliver():
            from .access import context
            with transaction.atomic():
                try:
                    context(link, writing=True, multi_space=True)
                    current = sendable(link).get(pk=box.pk)
                    password = cipher().decrypt(bytes(current.password_cipher)).decode()
                except (Http404, Mailbox.DoesNotExist, InvalidToken):
                    state = "bloqueado"
                else:
                    state = mail_protocol.submit(current.address, password, recipient, subject, body, current.signature_html)
                OutboundEmail.objects.filter(pk=run.pk).update(state=state)
                receipt["estado"] = state
        transaction.on_commit(deliver)
    return receipt
