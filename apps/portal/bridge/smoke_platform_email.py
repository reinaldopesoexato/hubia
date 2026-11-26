from unittest.mock import patch

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from . import platform_email as mail


def verify_platform_email(owner, ordinary, user, http):
    url = reverse("platform_email")
    csrf = owner.cookies["__Host-hubia-csrf"].value
    def post(action, **data):
        return owner.post(url, {"csrfmiddlewaretoken": csrf, "action": action, **data}, **http)

    assert ordinary.get(url, secure=True).status_code == 404
    assert owner.get(url, secure=True).status_code == 200
    fields = dict(address="hubia@example.org", sender_name="HUB IA", host="smtp.example.org", port=465,
                  tls_mode="ssl", password="fixture-smtp-password")
    assert post("save", **fields).status_code == 302
    profile = mail.PlatformMailSettings.objects.get(pk=1)
    assert bytes(profile.encrypted_password) != fields["password"].encode()
    assert fields["password"].encode() not in owner.get(url, secure=True).content
    fields["password"] = ""
    assert post("save", **fields).status_code == 302
    assert post("save", **{**fields, "host":"other.example.org"}).status_code == 400
    user.email = f"{user.username}@example.org"
    user.save(update_fields=["email"])
    public = Client(HTTP_HOST="hubia.psxt.io")

    with patch.object(mail, "SMTPBackend") as smtp:
        delivery = smtp.return_value.send_messages
        delivery.return_value = 1
        assert post("test").status_code == 302
        message = delivery.call_args.args[0][0]
        assert message.to == [fields["address"]] and fields["address"] in message.from_email
        assert smtp.call_args.kwargs["use_ssl"] and smtp.call_args.kwargs["password"] == "fixture-smtp-password"
        assert post("notice", recipient=user.pk, subject="Aviso", body="Manutenção prevista").status_code == 302
        assert delivery.call_args.args[0][0].to == [user.email]
        reset = reverse("password_reset")
        known = public.post(reset, {"email":user.email}, secure=True)
        unknown = public.post(reset, {"email":"inexistente@example.invalid"}, secure=True)
        assert known.status_code == unknown.status_code == 302 and known.url == unknown.url
        assert delivery.call_count == 2  # O envio de recuperação só ocorre no worker.
        job = mail.PasswordResetDelivery.objects.get(user=user)
        delivery.return_value = 0
        assert mail.deliver_reset(job.pk)
        job.refresh_from_db()
        assert job.attempts == 1 and job.due > timezone.now()
        delivery.return_value = 1
        job.due = timezone.now()
        job.save(update_fields=["due"])
        assert mail.deliver_reset(job.pk) and not mail.deliver_reset(job.pk)
        message = delivery.call_args.args[0][0]
        assert message.to == [user.email]
        path = next(line.removeprefix("https://hubia.psxt.io") for line in message.body.splitlines()
                    if line.startswith("https://hubia.psxt.io/conta/"))
        confirm = public.get(path, secure=True)
        assert confirm.status_code == 302 and confirm.url.endswith("/set-password/")
        new_password = "fixture-smoke-unique-9246"
        done = public.post(confirm.url, {"new_password1":new_password, "new_password2":new_password}, secure=True)
        assert done.status_code == 302 and done.url == reverse("password_reset_complete")
        user.refresh_from_db()
        assert user.check_password(new_password)
        assert "Link inválido" in public.get(path, secure=True).content.decode()
