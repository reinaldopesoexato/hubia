"""Read-only AviHUB and SMTP fixtures for the IAves subscriptions flow."""
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from .avihub_client import Panel
from .iaves_subscriptions import Kind, MailStatus, Status, check_due, deliver_notice
from .models import AviHubAgent, AviHubSession, IAvesNotice, IAvesSubscription, PlatformMailSettings
from .platform_email import MailUnavailable


class SubscriptionPanel(Panel):
    def __init__(self):
        super().__init__("https://avihub-fixture.invalid")
        self.active = "1"
        self.offline = True
        self.summary_params = None

    def request(self, *args, **kwargs):
        raise AssertionError("O fixture permite apenas consultas simuladas ao AviHUB.")

    def document(self, path):
        menu = f"""
            <nav class="navbar-body"><a href="/equipment"></a><form action="/users/sign_out"></form></nav>
            <header><form action="/user_settings/switch_unit"><select name="unit_id">
            <option value="1" {"selected" if self.active == "1" else ""}>Unidade QA</option>
            <option value="2" {"selected" if self.active == "2" else ""}>Outra unidade</option></select></form></header>
        """
        assert path == "/notifications", path
        return BeautifulSoup(menu, "html.parser")

    def options(self, target, scope):
        if target == "nuclei_id":
            assert scope == {"units": "1"}
            return {"3": "Núcleo QA"}
        assert target == "batch_id" and scope == {"units": "1", "nucleis": "3"}
        return {"4": "2026-42(Núcleo QA)", "5": "Lote Não informado(Núcleo QA)"}

    def page(self, path, params, unit_id, module):
        assert module == "/equipment" and unit_id == "1"
        self.require_context(self.home(), unit_id, module)
        if path == "/weights":
            assert params["nuclei_id"] == "3" and params["batch_id"] == "4"
            self.summary_params = params
            return BeautifulSoup(f"""
                <form action="/weights"><input name="start_date" value="{params['start_date']}">
                <input name="end_date" value="{params['end_date']}"></form>
                <div class="list-component"><div class="list-component-header"><div>Equipamento</div>
                <div>Núcleo</div><div>Lote</div><div>Pesagem</div><div>Data</div></div>
                <div class="list-component-row"><a class="list-component-row-link">
                <div>PSSEL/123</div><div>Núcleo QA</div><div>2026-42</div><div>2,4 Kg</div><div>25/11/2026</div></a></div>
                <div class="list-component-row"><a class="list-component-row-link">
                <div>PSSEL/456</div><div>Núcleo QA</div><div>2026-42</div><div>1600 g</div><div>25/11/2026</div></a></div>
                <div class="list-component-row"><a class="list-component-row-link">
                <div>CSFC/987</div><div>Núcleo QA</div><div>2026-42</div><div>5000 g</div><div>25/11/2026</div></a></div></div>
            """, "html.parser")
        assert path == "/equipment" and params == {"page": 1}
        color = "text-yellow-500" if self.offline else "text-success-500"
        return BeautifulSoup(f"""
            <div data-equipment-realtime-unit-id-value="1"><div class="equipment-card"
                data-equipment-id="42" data-equipment-model="PSSEL" data-equipment-serie="123">
            <div class="equipment-location">Núcleo QA</div>
            <turbo-frame id="equipment_42_last_connection_frame"><span class="{color}">Ontem</span></turbo-frame>
            </div></div>
        """, "html.parser")


def verify_subscriptions(buyer, peer, stranger, customer, tenant, space, agent, connector):
    url = reverse("iaves_subscriptions", args=[tenant, space, agent])
    avi = AviHubAgent.objects.create(pk=connector, tenant=tenant, origin="https://avihub-fixture.invalid")
    account = AviHubSession.objects.create(agent=avi, user=customer, email=customer.email, ready=True, encrypted=b"fixture")
    PlatformMailSettings.objects.update_or_create(pk=1, defaults={"encrypted_password": b"fixture"})
    params = {"action": "save", "account": account.account_ref, "kind": Kind.SUMMARY,
              "unit": "1", "nucleus": "3", "batch": "4", "frequency": "weekly", "channel": "portal"}
    panel = SubscriptionPanel()
    with patch("bridge.avihub.restore", return_value=panel), patch("bridge.iaves_subscriptions.send_mail") as smtp:
        assert buyer.get(url, secure=True).status_code == 200
        assert stranger.get(url, secure=True).status_code == 404
        source = urlencode({"account": account.account_ref, "unit": "1", "nucleus": "3"})
        assert b"2026-42" in buyer.get(f"{url}?{source}", secure=True).content
        assert buyer.post(url, {**params, "unit": "2"}, secure=True).status_code == 400
        assert buyer.post(url, {**params, "batch": "9"}, secure=True).status_code == 400
        assert buyer.post(url, {**params, "batch": "5"}, secure=True).status_code == 400
        assert peer.post(url, params, secure=True).status_code == 400
        assert buyer.post(url, params, secure=True).status_code == 302
        assert buyer.post(url, params, secure=True).status_code == 302
        summary = IAvesSubscription.objects.get(user=customer, kind=Kind.SUMMARY)
        now = timezone.now()
        assert not check_due(now=now, subscription_id=summary.pk)
        assert check_due(now=summary.next_due, subscription_id=summary.pk)
        assert not check_due(now=summary.next_due, subscription_id=summary.pk)
        assert panel.summary_params["start_date"] == (timezone.localdate(summary.next_due) - timedelta(days=7)).isoformat()
        assert b"2,000 kg (2 de 3 registros)" in buyer.get(url, secure=True).content
        peer_inbox = peer.get(url, secure=True)
        assert peer_inbox.status_code == 200 and b"3 registros de pesagem" not in peer_inbox.content
        assert peer.post(url, {"action": "pause", "subscription": summary.pk}, secure=True).status_code == 404
        notice = IAvesNotice.objects.get(subscription=summary, mail_status=MailStatus.PORTAL)
        assert notice.scheduled_for == summary.next_due
        assert not deliver_notice()
        smtp.assert_not_called()

        customer.email = "avisos-fixture@example.invalid"
        customer.save(update_fields=["email"])
        account.email = customer.email
        account.save(update_fields=["email"])
        assert buyer.post(url, {**params, "kind": Kind.OFFLINE, "frequency": "daily", "channel": "email"},
                          secure=True).status_code == 302
        alarm = IAvesSubscription.objects.get(user=customer, kind=Kind.OFFLINE)
        assert check_due(now=alarm.next_due, subscription_id=alarm.pk)
        smtp.side_effect = MailUnavailable
        assert deliver_notice() and not deliver_notice()
        assert smtp.call_count == 1
        assert smtp.call_args.args[-1] == [customer.email]
        assert "PSSEL" not in smtp.call_args.args[1]
        assert IAvesNotice.objects.filter(subscription=alarm, mail_status=MailStatus.UNCERTAIN).count() == 1

        alarm.refresh_from_db()
        assert check_due(now=alarm.next_due, subscription_id=alarm.pk)
        assert IAvesNotice.objects.filter(subscription=alarm).count() == 1
        alarm.refresh_from_db()
        panel.active = "2"
        assert check_due(now=alarm.next_due, subscription_id=alarm.pk)
        alarm.refresh_from_db()
        assert alarm.status == Status.BLOCKED and IAvesNotice.objects.filter(subscription=alarm).count() == 1
        assert panel.active == "2"
        panel.active, panel.offline = "1", False
        assert check_due(now=alarm.next_due, subscription_id=alarm.pk)
        alarm.refresh_from_db()
        panel.offline = True
        assert check_due(now=alarm.next_due, subscription_id=alarm.pk)
        assert IAvesNotice.objects.filter(subscription=alarm).count() == 2
        assert buyer.post(url, {"action": "pause", "subscription": alarm.pk}, secure=True).status_code == 302
        assert not deliver_notice()
        assert smtp.call_count == 1

        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("DELETE FROM hub_tenancy.agent_spaces WHERE tenant_id=%s AND workspace_id=%s AND agent_id=%s",
                           [tenant, space, connector])
            cursor.execute("SET LOCAL ROLE hub_portal")
        summary.refresh_from_db()
        assert check_due(now=summary.next_due, subscription_id=summary.pk)
        summary.refresh_from_db()
        assert summary.status == Status.BLOCKED
        assert b"3 registros de pesagem" not in buyer.get(url, secure=True).content
        assert buyer.post(url, {"action": "delete", "subscription": summary.pk}, secure=True).status_code == 302
    print("Avisos IAves: lote, unidade ativa, vínculo, inbox privado, deduplicação e SMTP simulado verificados.")
