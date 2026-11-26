"""Transactional mail sent with the platform owner's SMTP profile."""
from datetime import timedelta
from email.utils import formataddr
import logging
from smtplib import SMTPException

from cryptography.fernet import InvalidToken
from django import forms
from django.utils.translation import gettext as translate_text
from django.utils import translation
from django.utils.translation import gettext_lazy as tr
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.core.validators import DomainNameValidator
from django.db import connection, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from model_provider import cipher
from portal_identity import bind_account
from .models import AccountProfile, PasswordResetDelivery, PlatformMailSettings

logger = logging.getLogger(__name__)


class MailUnavailable(Exception):
    pass


class PlatformBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        profile = PlatformMailSettings.objects.filter(pk=1).first()
        try:
            if not profile or not profile.encrypted_password or profile.tls_mode not in ("ssl", "starttls"):
                raise MailUnavailable(translate_text("Configure o servidor e a senha SMTP da plataforma."))
            password = cipher().decrypt(bytes(profile.encrypted_password)).decode()
            backend = SMTPBackend(host=profile.host, port=profile.port, username=profile.address,
                                  password=password, use_ssl=profile.tls_mode == "ssl",
                                  use_tls=profile.tls_mode == "starttls", timeout=10, fail_silently=False)
            for message in email_messages:
                message.from_email = formataddr((profile.sender_name, profile.address))
            return backend.send_messages(email_messages)
        except (InvalidToken, SMTPException, OSError, ValueError, MailUnavailable):
            if self.fail_silently:
                return 0
            raise MailUnavailable(translate_text("Falha no envio. Verifique o servidor, a porta e as credenciais SMTP.")) from None


class SMTPForm(forms.ModelForm):
    host = forms.CharField(label=tr("Servidor SMTP"), max_length=253, validators=[DomainNameValidator()])
    port = forms.IntegerField(label=tr("Porta"), min_value=1, max_value=65535)
    tls_mode = forms.ChoiceField(label=tr("Criptografia"), choices=[("ssl", "SSL/TLS"), ("starttls", "STARTTLS")])
    password = forms.CharField(label=tr("Senha da caixa de e-mail"), required=False, strip=False, max_length=1024,
                               widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    class Meta:
        model = PlatformMailSettings
        fields = ["address", "sender_name", "host", "port", "tls_mode"]
        labels = {"address": tr("E-mail de envio e login SMTP"), "sender_name": tr("Nome do remetente")}

    def clean_sender_name(self):
        value = self.cleaned_data["sender_name"]
        if not value.isprintable():
            raise forms.ValidationError(translate_text("Informe o nome do remetente em uma única linha."))
        return value

    def clean(self):
        data = super().clean()
        if not data.get("password") and (not self.instance.encrypted_password or
                {"host", "address", "port", "tls_mode"}.intersection(self.changed_data)):
            self.add_error("password", translate_text("Informe a senha para vincular esta caixa ao servidor selecionado."))
        return data


class NoticeForm(forms.Form):
    recipient = forms.ModelChoiceField(label=tr("Usuário destinatário"), queryset=get_user_model().objects.none())
    subject = forms.CharField(label=tr("Assunto"), max_length=200)
    body = forms.CharField(label=tr("Mensagem"), max_length=10000, widget=forms.Textarea(attrs={"rows": 5}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].queryset = get_user_model().objects.filter(is_active=True).exclude(email="").order_by("username")


@never_cache
@login_required
@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def configure(request):
    with transaction.atomic(), connection.cursor() as cursor:
        _, owner = bind_account(cursor, request.user)
        if not owner:
            raise Http404
        action = request.POST.get("action") if request.method == "POST" else None
        if request.method == "POST" and action not in ("save", "test", "notice"):
            raise Http404
        if action == "save":
            profile, _ = PlatformMailSettings.objects.select_for_update().get_or_create(pk=1)
        else:
            profile = PlatformMailSettings.objects.filter(pk=1).first() or PlatformMailSettings()
        saved_address = profile.address
        smtp = SMTPForm(request.POST if action == "save" else None, instance=profile)
        if action == "save" and smtp.is_valid():
            if smtp.cleaned_data["password"]:
                profile.encrypted_password = cipher().encrypt(smtp.cleaned_data["password"].encode())
            smtp.save()
            request.session["platform_mail_status"] = translate_text("Configuração de e-mail salva.")
            return redirect("platform_email")
    notice = NoticeForm(request.POST if action == "notice" else None)
    error = None
    if action == "test" or (action == "notice" and notice.is_valid()):
        recipient, subject, body = profile.address, "Teste de envio do HUB IA", "O HUB IA enviou esta mensagem de teste pela configuração da plataforma."
        if action == "notice":
            recipient = notice.cleaned_data["recipient"].email
            subject, body = notice.cleaned_data["subject"], notice.cleaned_data["body"]
        try:
            if send_mail(subject, body, None, [recipient]) != 1:
                raise MailUnavailable(translate_text("O servidor SMTP não confirmou o envio."))
        except MailUnavailable as exc:
            error = str(exc)
        else:
            request.session["platform_mail_status"] = f"O servidor SMTP aceitou a mensagem para {recipient}."
            return redirect("platform_email")
    return render(request, "platform_email.html", {"is_owner": True, "form": smtp, "notice_form": notice,
                  "configured": bool(profile.encrypted_password), "address": saved_address, "error": error,
                  "status": request.session.pop("platform_mail_status", None)},
                  status=400 if error or smtp.errors or notice.errors else 200)


class QueuedResetForm(PasswordResetForm):
    def save(self, **kwargs):
        # Mail is delivered by the worker. An SMTP delay never identifies an account in the public response.
        users = list(self.get_users(self.cleaned_data["email"]))
        if len(users) == 1:
            user = users[0]
            token = default_token_generator.make_token(user)
            preferred = AccountProfile.objects.filter(user=user).values_list("language", flat=True).first()
            PasswordResetDelivery.objects.create(user=user, language=preferred or translation.get_language(), encrypted_token=cipher().encrypt(token.encode()),
                                                due=timezone.now())


@transaction.atomic
def deliver_reset(job_id=None):
    queue = PasswordResetDelivery.objects.all()
    if job_id is not None:
        queue = queue.filter(pk=job_id)
    job = (queue.select_for_update(skip_locked=True, of=("self",))
           .select_related("user").filter(due__lte=timezone.now()).order_by("due", "pk").first())
    if not job:
        return False
    user = job.user
    try:
        token = cipher().decrypt(bytes(job.encrypted_token)).decode()
    except InvalidToken:
        token = ""
    if user.is_active and user.has_usable_password() and default_token_generator.check_token(user, token):
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        path = reverse("password_reset_confirm", args=[uid, token])
        with translation.override(job.language):
            subject = translation.gettext("Recuperação de senha do HUB IA")
            greeting = translation.gettext("Foi solicitada uma nova senha para sua conta %(user)s no HUB IA.") % {"user": user.get_username()}
            action = translation.gettext("Abra o link para definir sua senha:")
            body = "\n\n".join((greeting, f"{action}\nhttps://hubia.psxt.io{path}",
                                translation.gettext("Se você não fez essa solicitação, pode ignorar esta mensagem.")))
        try:
            if send_mail(subject, body, None, [user.email]) != 1:
                raise MailUnavailable(translate_text("O servidor SMTP não confirmou o envio."))
        except MailUnavailable:
            job.attempts += 1
            if job.attempts < 5:
                job.due = timezone.now() + timedelta(minutes=2 ** job.attempts)
                job.save(update_fields=["due", "attempts"])
                return True
            logger.error("SMTP da plataforma indisponível após cinco tentativas de recuperação de senha.")
    job.delete()
    return True
