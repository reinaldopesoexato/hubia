"""Exercise report delivery options without contacting any recipients."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.http import Http404

from model_provider import cipher
from . import avihub_report_delivery as delivery
from .platform_email import MailUnavailable


def verify_delivery():
    address = "cliente@example.invalid"
    user = Mock(is_active=True, email=address)
    report = SimpleNamespace(user=user, tenant=uuid4(), space=uuid4(), agent_id=uuid4(), pk=uuid4(), native_id="123",
                             state="ready", delivery="uncertain", channel="email_anexo", destination=address,
                             parameters={"formato":"csv_aves"}, session_id=8, link_id=None, from_mcp=False)
    space_context = {"tenant":report.tenant, "space":report.space}
    attachment = ("Peso Aves.csv", "text/csv; charset=utf-16le", "CV;uniformidade\r\n".encode("utf-16le"))

    with patch.object(delivery, "eligible_email", return_value=address), \
            patch.object(delivery, "whatsapp_destinations", return_value=[]):
        assert [o["canal"] for o in delivery.options(Mock(), user, {}, object(), report_format="xlsx")] == [
            "painel", "email", "email_anexo",
        ]
        assert [o["canal"] for o in delivery.options(Mock(), user, {}, object(), report_format="pdf")] == [
            "painel", "email", "email_anexo",
        ]
        assert [o["canal"] for o in delivery.options(Mock(), user, {}, None)] == ["painel"]

    with patch.object(delivery, "delivery_context", return_value=({}, object(), None)), \
            patch.object(delivery, "eligible_email", return_value=address), \
            patch.object(delivery.EmailMessage, "send", autospec=True, return_value=1) as smtp:
        assert delivery.deliver(report, attachment=attachment) == "accepted"
        message = smtp.call_args.args[0]
        smtp.assert_called_once()
        assert message.to == [address] and f"/relatorios/{report.pk}/arquivo/" in message.body
        part, = message.attachments
        assert part.get_filename() == attachment[0] and part.get_content_type() == "text/csv"
        assert part.get_content_charset() == "utf-16le" and part.get_payload(decode=True) == attachment[2]

        smtp.reset_mock()
        assert delivery.deliver(report, attachment=(attachment[0], "text/html", b"wrong")) == "failed"
        assert delivery.deliver(report, attachment=(attachment[0], "text/csv\nBcc: test", b"wrong")) == "failed"
        assert delivery.deliver(report, attachment=(attachment[0], attachment[1], b"x" * (delivery.EMAIL_ATTACHMENT_LIMIT + 1))) == "failed"
        assert delivery.deliver(report) == "failed"
        smtp.assert_not_called()

        report.destination = "outra@example.invalid"
        assert delivery.deliver(report, attachment=attachment) == "skipped"
        smtp.assert_not_called()
        report.destination = address
        report.parameters["formato"] = "pdf"
        pdf = ("Peso Aves.pdf", "application/pdf", b"%PDF-1.7\n%%EOF\n")
        assert delivery.deliver(report, attachment=pdf) == "accepted"
        smtp.assert_called_once()
        part, = smtp.call_args.args[0].attachments
        assert part.get_filename() == pdf[0] and part.get_content_type() == pdf[1]
        assert part.get_payload(decode=True) == pdf[2]

        report.channel = "email"
        smtp.reset_mock()
        smtp.side_effect = MailUnavailable()
        assert delivery.deliver(report) == "uncertain"
        smtp.assert_called_once()
        message = smtp.call_args.args[0]
        assert not message.attachments and f"/relatorios/{report.pk}/arquivo/" in message.body

    report.channel = "whatsapp"
    report.destination = "42"
    report.parameters["formato"] = "csv_aves"
    config = {"integration":"fixture", "key":"fixture-not-a-real-secret"}
    destination = SimpleNamespace(pk=42, channel=SimpleNamespace(encrypted=cipher().encrypt(json.dumps(config).encode())),
                                  name="Destino QA", address="5511999999999")
    with patch.object(delivery, "delivery_context", return_value=({}, object(), None)), \
            patch.object(delivery, "whatsapp_destinations", return_value=[destination]), \
            patch.dict(delivery.CHANNELS, {"whatsapp":("WhatsApp", Mock(return_value=("done", "fixture")))}):
        whatsapp = delivery.CHANNELS["whatsapp"][1]
        assert delivery.deliver(report) == "accepted"
        whatsapp.assert_called_once()
        _, recipient, text = whatsapp.call_args.args
        assert recipient == destination.address and f"/relatorios/{report.pk}/arquivo/" in text
        assert f"/reports/{report.native_id}" not in text
        whatsapp.reset_mock()
        report.destination = "41"
        assert delivery.deliver(report) == "skipped"
        whatsapp.assert_not_called()

    report.from_mcp = True
    report.link_id = None
    try:
        delivery.delivery_context(Mock(), report)
    except Http404:
        pass
    else:
        raise AssertionError("Excluir conexão OAuth não pode transformar solicitação MCP em portal.")

    report.link_id = uuid4()
    linked = Mock(writable=True)
    scopes = Mock()
    scopes.objects.filter().values_list.return_value = ["hub.read"]
    link_context = {"space_tenants": {report.space:report.tenant}}
    session = Mock()
    with patch.object(delivery.avihub, "personal") as personal, \
            patch.object(delivery.Link.objects, "filter") as links, \
            patch.object(delivery, "link_context", return_value=link_context), \
            patch.object(delivery, "get_access_token_model", return_value=scopes), \
            patch.object(delivery, "space_context", return_value=space_context), \
            patch.object(delivery, "active_agent", return_value={"kind":"avihub"}):
        personal.return_value.filter.return_value.first.return_value = session
        links.return_value.first.return_value = linked
        try:
            delivery.delivery_context(Mock(), report)
        except Http404:
            pass
        else:
            raise AssertionError("Enviar WhatsApp do MCP exige o escopo atual hub.notify.")
        scopes.objects.filter().values_list.return_value = ["hub.read hub.notify"]
        ctx, selected, rebound = delivery.delivery_context(Mock(), report)
        assert ctx["can_notify"] and selected == session and rebound == linked
    print("Entrega Peso Aves: SMTP com arquivo exato, WhatsApp com link privado, escopo e revogação verificados.")
