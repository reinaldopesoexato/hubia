"""Public enrollment and Google OpenID Connect for personal HUB accounts."""
import base64
import hashlib
import json
import secrets
import time
from datetime import timedelta
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import jwt
from django import forms
from django.utils.translation import gettext as translate_text
from django.utils import translation
from django.utils.translation import gettext_lazy as tr
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login
from django.contrib.auth.forms import AuthenticationForm, UsernameField
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import connection, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from auth_limits import allow_auth_attempt
from profile_photos import google_photo_url
from bridge.models import ProfilePhoto
from bridge.models import AccountProfile, PendingRegistration, PlatformMailSettings
from bridge.platform_email import MailUnavailable
from portal_identity import bind_account

ORIGIN = 'https://hubia.psxt.io'
SIGNUP_SALT = 'hubia.personal.activation'
ACTIVATION_AGE = 24 * 3600
google_keys = jwt.PyJWKClient('https://www.googleapis.com/oauth2/v3/certs', timeout=6)


def lock_email(email):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 3101))", [email.casefold()])


def lock_google_subject(subject):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,3102))",[subject])


class EmailLoginForm(AuthenticationForm):
    username = UsernameField(max_length=254, label=tr("Usuário ou e-mail"))

    def clean(self):
        identifier = self.cleaned_data.get('username', '')
        users = get_user_model().objects
        if '@' in identifier and not users.filter(username=identifier).exists():
            matches = list(users.filter(email__iexact=identifier).values_list('username', flat=True)[:2])
            if len(matches) == 1:
                self.cleaned_data['username'] = matches[0]
        return super().clean()


class RegistrationForm(forms.Form):
    name = forms.CharField(label=tr('Nome'), max_length=150, widget=forms.TextInput(attrs={'autocomplete':'name'}))
    email = forms.EmailField(label=tr('E-mail'), max_length=254, widget=forms.EmailInput(attrs={'autocomplete':'email'}))
    phone = forms.RegexField(label=tr('Telefone'), max_length=32, regex=r'^\+?[\d() .-]{7,32}$',
                            widget=forms.TextInput(attrs={'type':'tel','autocomplete':'tel'}))
    company = forms.CharField(label=tr('Empresa'), max_length=150, widget=forms.TextInput(attrs={'autocomplete':'organization'}))
    password = forms.CharField(label=tr('Senha'), min_length=12, max_length=256, strip=False,
                               widget=forms.PasswordInput(attrs={'autocomplete':'new-password'}))

    def clean_phone(self):
        value = self.cleaned_data['phone']
        if not 7 <= sum(c.isdigit() for c in value) <= 15:
            raise forms.ValidationError(tr('Informe um telefone com DDD.'))
        return value

    def clean(self):
        data = super().clean()
        if 'password' in data:
            user = get_user_model()(email=data.get('email',''),first_name=data.get('name',''))
            try:
                validate_password(data['password'], user)
            except ValidationError as exc:
                self.add_error('password',exc)
        return data


def create_personal(email, name, *, password_hash=None, phone='', company='', google_subject=None):
    username = email if len(email) <= 150 else 'cliente-'+uuid4().hex
    user = get_user_model()(username=username, email=email, first_name=name[:150])
    if password_hash:
        user.password = password_hash
    else:
        user.set_unusable_password()
    user.save(force_insert=True)
    AccountProfile.objects.create(user=user,phone=phone,company=company,google_subject=google_subject)
    with connection.cursor() as cursor:
        cursor.execute("SELECT hub_admin.register_personal_account(%s,%s)", [user.pk,(name or email)[:120]])
    return user


def page(request, **context):
    return render(request, 'registration.html', {**context,'google_enabled':google_enabled()}, status=context.get('status',200))


def limited(request, key, limit):
    ip = request.META.get('HTTP_X_REAL_IP') or request.META.get('REMOTE_ADDR','')
    return not allow_auth_attempt(key, ip, limit)


def too_many():
    return HttpResponse(translate_text('Muitas tentativas. Tente novamente em cinco minutos.'),status=429)


def email_in_use(email):
    users = get_user_model().objects
    return users.filter(email__iexact=email).exists() or users.filter(username__iexact=email).exists()


@never_cache
@sensitive_post_parameters('password')
@require_http_methods(['GET','POST'])
def signup(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = RegistrationForm(request.POST if request.method=='POST' else None)
    if request.method=='POST' and limited(request,'public-signup',8):
        return too_many()
    if request.method=='POST' and form.is_valid():
        data = form.cleaned_data
        email = data['email'].lower()
        if not allow_auth_attempt('signup-address',email,3):
            return too_many()
        if not activation_mail_ready():
            form.add_error(None,tr('O envio de e-mails de ativação está indisponível. Tente novamente mais tarde.'))
            return page(request,form=form,mode='signup',status=503)
        if not email_in_use(email):
            PendingRegistration.objects.create(language=translation.get_language(),name=data['name'],email=email,phone=data['phone'],
                company=data['company'],password_hash=make_password(data['password']),delivery_due=timezone.now())
        return page(request,mode='sent')
    return page(request,mode='signup',form=form,status=400 if form.errors else 200)


def activation_mail_ready():
    return PlatformMailSettings.objects.filter(pk=1).exclude(encrypted_password=b'').exists()


@transaction.atomic
def deliver_activation(job_id=None):
    now = timezone.now()
    cutoff = now-timedelta(seconds=ACTIVATION_AGE)
    queue = PendingRegistration.objects.all()
    if job_id is not None:
        queue = queue.filter(pk=job_id)
    queue.filter(created__lt=cutoff).delete()
    pending = queue.select_for_update(skip_locked=True).filter(
        delivery_due__lte=now).order_by('delivery_due').first()
    if not pending:
        return False
    if email_in_use(pending.email):
        pending.delete()
        return True
    token = signing.dumps(str(pending.pk),salt=SIGNUP_SALT)
    url = ORIGIN + reverse('activate_registration',args=[token])
    try:
        with translation.override(pending.language):
            subject = translation.gettext("Ative sua conta no HUB IA da Peso Exato")
            body = "\n\n".join((translation.gettext("Confirme seu cadastro usando este link. Ele vale por 24 horas a partir da solicitação."),
                                url, translation.gettext("Se você não fez este cadastro, ignore esta mensagem.")))
            if send_mail(subject, body, None, [pending.email]) != 1:
                raise MailUnavailable
    except MailUnavailable:
        pending.attempts += 1
        pending.delivery_due = now + timedelta(minutes=min(60,2**min(pending.attempts,6)))
    else:
        pending.delivery_due = None
    pending.save(update_fields=['attempts','delivery_due'])
    return True


@never_cache
@sensitive_post_parameters('password')
@require_http_methods(['GET','POST'])
def activate(request, token):
    try:
        key = signing.loads(token,salt=SIGNUP_SALT,max_age=ACTIVATION_AGE)
    except signing.BadSignature:
        return page(request,mode='expired',status=400)
    if request.method=='POST' and limited(request,'activation',12):
        return too_many()
    with transaction.atomic():
        pending = PendingRegistration.objects.filter(pk=key,created__gte=timezone.now()-timedelta(seconds=ACTIVATION_AGE)).first()
        if not pending:
            return page(request,mode='expired',status=400)
        if request.method=='GET':
            return page(request,mode='activate')
        lock_email(pending.email)
        pending = PendingRegistration.objects.select_for_update().filter(pk=key).first()
        if not pending or email_in_use(pending.email):
            return page(request,mode='expired',status=400)
        if not check_password(request.POST.get('password',''),pending.password_hash):
            return page(request,mode='activate',error='Confira a senha informada no cadastro.',status=400)
        user = create_personal(pending.email,pending.name,password_hash=pending.password_hash,
                               phone=pending.phone,company=pending.company)
        PendingRegistration.objects.filter(email__iexact=pending.email).delete()
    login(request,user)
    return redirect('dashboard')


def google_enabled():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def destination(request):
    url = request.POST.get('next','')
    return url if url_has_allowed_host_and_scheme(url,{request.get_host()},require_https=True) else reverse('dashboard')


@never_cache
@require_POST
def google_start(request):
    if not google_enabled():
        return page(request,mode='google',error='O login com Google ainda não foi configurado.',status=503)
    if limited(request,'google-start',30):
        return too_many()
    flow = {k:secrets.token_urlsafe(48) for k in ('state','nonce','verifier')}
    flow.update(start=time.time(),next=destination(request))
    request.session['google_flow'] = flow
    challenge = base64.urlsafe_b64encode(hashlib.sha256(flow['verifier'].encode()).digest()).decode().rstrip('=')
    params = dict(client_id=settings.GOOGLE_CLIENT_ID,redirect_uri=ORIGIN+reverse('google_callback'),
                  response_type='code',scope='openid email profile',state=flow['state'],nonce=flow['nonce'],
                  code_challenge=challenge,code_challenge_method='S256')
    return redirect('https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params))


def google_identity(code, flow):
    data = urlencode(dict(code=code,client_id=settings.GOOGLE_CLIENT_ID,client_secret=settings.GOOGLE_CLIENT_SECRET,
                          grant_type='authorization_code',redirect_uri=ORIGIN+reverse('google_callback'),
                          code_verifier=flow['verifier'])).encode()
    with urlopen(Request('https://oauth2.googleapis.com/token',data=data), timeout=8) as response:
        token = json.loads(response.read(65536))['id_token']
    claims = jwt.decode(token,google_keys.get_signing_key_from_jwt(token).key,algorithms=['RS256'],
        audience=settings.GOOGLE_CLIENT_ID,issuer=['https://accounts.google.com','accounts.google.com'],
        options={'require':['exp','iat','iss','sub','aud','nonce','email']})
    if (not constant_time_compare(claims['nonce'],flow['nonce']) or claims.get('email_verified') is not True
            or claims.get('azp',settings.GOOGLE_CLIENT_ID) != settings.GOOGLE_CLIENT_ID
            or not 0 < len(claims['sub']) <= 255 or len(claims['email']) > 254):
        raise ValueError
    validate_email(claims['email'])
    return claims


def signed_in(request,user,target):
    with transaction.atomic(), connection.cursor() as cursor:
        if not user.is_active or not bind_account(cursor,user)[0]:
            return page(request,mode='google',error='Esta conta está desativada.',status=403)
    login(request,user)
    return redirect(target)


@never_cache
@require_GET
def google_callback(request):
    flow = request.session.pop('google_flow',None)
    if (not flow or not constant_time_compare(flow['state'],request.GET.get('state',''))
            or time.time()-flow['start']>600 or not request.GET.get('code') or not google_enabled()):
        return page(request,mode='google',error='A entrada pelo Google foi cancelada ou expirou.',status=400)
    try:
        claims = google_identity(request.GET['code'],flow)
    except (URLError,OSError,ValueError,KeyError,TypeError,ValidationError,jwt.PyJWTError):
        return page(request,mode='google',error='Não foi possível confirmar a identidade com o Google.',status=400)
    email,sub = claims['email'].lower(),claims['sub']
    picture = google_photo_url(claims.get('picture'))
    with transaction.atomic():
        lock_google_subject(sub)
        lock_email(email)
        profile = AccountProfile.objects.select_related('user').filter(google_subject=sub).first()
        if profile:
            user = profile.user
        elif email_in_use(email):
            matches = list(get_user_model().objects.filter(email__iexact=email,is_active=True)[:2])
            if len(matches)!=1 or not matches[0].has_usable_password():
                return page(request,mode='google',error='Entre com o método já vinculado a esta conta.',status=403)
            request.session['google_link'] = dict(user=matches[0].pk,sub=sub,email=email,picture=picture,at=time.time(),next=flow['next'])
            return page(request,mode='link')
        else:
            user = create_personal(email,claims.get('name') or email,google_subject=sub)
            PendingRegistration.objects.filter(email__iexact=email).delete()
        ProfilePhoto.objects.update_or_create(user=user, defaults={"google_url": picture})
    return signed_in(request,user,flow['next'])


@never_cache
@sensitive_post_parameters('password')
@require_POST
def google_link(request):
    claim = request.session.get('google_link')
    if not claim or time.time()-claim['at']>600:
        return page(request,mode='expired',status=400)
    if limited(request,'google-link',10):
        return too_many()
    with transaction.atomic():
        lock_google_subject(claim['sub'])
        lock_email(claim['email'])
        user = get_user_model().objects.select_for_update().filter(pk=claim['user'],is_active=True,email__iexact=claim['email']).first()
        if not user or not authenticate(request,username=user.username,password=request.POST.get('password','')):
            return page(request,mode='link',error='Confira sua senha e tente novamente.',status=400)
        profile,_ = AccountProfile.objects.get_or_create(user=user)
        if profile.google_subject not in (None,claim['sub']) or AccountProfile.objects.filter(google_subject=claim['sub']).exclude(user=user).exists():
            return page(request,mode='google',error='Esta conta já possui outro vínculo.',status=409)
        profile.google_subject = claim['sub']
        profile.save(update_fields=['google_subject'])
        ProfilePhoto.objects.update_or_create(user=user, defaults={"google_url": claim.get("picture", "")})
        request.session.pop('google_link')
    return signed_in(request,user,claim['next'])
