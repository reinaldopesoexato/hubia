from uuid import uuid4

from django import forms
from django.utils.translation import gettext as translate_text
from django.utils.translation import gettext_lazy as tr
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.core.paginator import Paginator
from django.db import connection, transaction, IntegrityError, DataError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django.views.decorators.debug import sensitive_post_parameters

from portal_identity import bind_account
from bridge.models import AccountProfile


class RecoveryAddressForm(forms.ModelForm):
    email = forms.EmailField(label=tr("E-mail de recuperação"), required=False, max_length=254)

    class Meta:
        model = get_user_model()
        fields = ["email"]

    def clean_email(self):
        email = self.cleaned_data["email"]
        from registration import lock_email
        if email:
            lock_email(email)
        others = get_user_model().objects.exclude(pk=self.instance.pk)
        if email and (others.filter(email__iexact=email).exists() or others.filter(username__iexact=email).exists()):
            raise forms.ValidationError(translate_text("Esse e-mail já está vinculado a outro usuário."))
        return email.lower()


class PortalUserCreationForm(UserCreationForm, RecoveryAddressForm):
    name = forms.CharField(label=tr('Nome'),max_length=150,required=False)
    company = forms.CharField(label=tr('Empresa'),max_length=150,required=False)
    phone = forms.CharField(label=tr('Telefone'),max_length=32,required=False)

    def clean_username(self):
        value = super().clean_username()
        if value and '@' in value:
            from registration import lock_email
            lock_email(value)
            if get_user_model().objects.filter(email__iexact=value).exists():
                raise forms.ValidationError(translate_text('Este endereço já está vinculado a uma conta.'))
        return value

    class Meta(UserCreationForm.Meta):
        fields = ["username", "email"]


@sensitive_post_parameters("password1","password2")
@never_cache
@login_required
@require_http_methods(["GET","POST"])
def people(request):
    form = PortalUserCreationForm()
    error = None
    with transaction.atomic(), connection.cursor() as cursor:
        _, owner = bind_account(cursor,request.user)
        if not owner:
            raise Http404
        if request.method=="POST":
            action = request.POST.get("action")
            if action=="create":
                form = PortalUserCreationForm(request.POST)
                if form.is_valid():
                    try:
                        with transaction.atomic():
                            user = form.save(commit=False)
                            user.first_name = form.cleaned_data['name']
                            user.save()
                            AccountProfile.objects.create(user=user,company=form.cleaned_data['company'],phone=form.cleaned_data['phone'])
                            cursor.execute("SELECT hub_admin.register_portal_user(%s,%s)",[str(user.pk),(user.get_full_name() or user.username)[:120]])
                        return redirect("people")
                    except IntegrityError as exc:
                        if exc.__cause__.sqlstate != "23505":
                            raise
                        form.add_error("username",translate_text("Este usuário já está cadastrado."))
            elif action in {"enable","disable","email"}:
                ident = request.POST.get("account","")
                if not ident.isascii() or not ident.isdecimal() or len(ident)>18:
                    raise Http404
                account = get_user_model().objects.select_for_update().filter(pk=int(ident)).first()
                if not account or (action != "email" and account.pk == request.user.pk):
                    raise Http404
                if action == "email":
                    address = RecoveryAddressForm(request.POST, instance=account)
                    if address.is_valid():
                        account.save(update_fields=["email"])
                        return redirect("people")
                    error = address.errors["email"][0]
                else:
                    try:
                        with transaction.atomic():
                            enabled = action=="enable"
                            cursor.execute("SELECT hub_admin.set_portal_user_active(%s,%s)",[ident,enabled])
                            account.is_active=enabled
                            account.save(update_fields=["is_active"])
                    except DataError as exc:
                        if exc.__cause__.sqlstate!="22023":
                            raise
                        error = translate_text("A conta não pode ser alterada.")
                    else:
                        return redirect("people")
            else:
                error = translate_text("Operação inválida.")
        search = request.GET.get('q','').strip()[:254]
        users = get_user_model().objects.select_related("accountprofile").order_by("first_name","username")
        if search:
            users = users.filter(Q(username__icontains=search) | Q(email__icontains=search) |
                                 Q(first_name__icontains=search) | Q(last_name__icontains=search) |
                                 Q(accountprofile__company__icontains=search))
        users = Paginator(users,30).get_page(request.GET.get('pagina'))
        cursor.execute("SELECT * FROM hub_admin.technical_events()")
        events = cursor.fetchall()
    return render(request,"people.html",{"is_owner":True,"form":form,"users":users,"search":search,"events":events,"error":error},
                  status=400 if error or form.errors else 200)


class AccountEditForm(RecoveryAddressForm):
    name = forms.CharField(label=tr('Nome'),max_length=150,required=False)
    phone = forms.CharField(label=tr('Telefone'),max_length=32,required=False)
    company = forms.CharField(label=tr('Empresa'),max_length=150,required=False)


def access_options(cursor, account):
    cursor.execute("SELECT * FROM hub_admin.account_options(%s) ORDER BY category,lower(label),id",[account.pk])
    groups, options = {}, []
    for kind,tenant,ident,label,selected,locked in cursor.fetchall():
        key = str(tenant)
        row = dict(kind=kind,tenant=key,id=str(ident),name=label,selected=selected,locked=locked)
        options.append(row)
        group = groups.setdefault(key,dict(name='Espaço pessoal',spaces=[],agents=[]))
        if kind=='institution':
            group.update(name=label,institution=row)
        else:
            group['spaces' if kind=='space' else 'agents'].append(row)
    ordered = sorted(groups.values(),key=lambda g:g['name'].casefold())
    return options, ordered


def access_revision(account,profile,options,internal_mail):
    import json
    from django.utils.crypto import salted_hmac
    state = [account.email,account.get_full_name(),account.is_active,profile.phone,profile.company,profile.guide_enabled,internal_mail,options]
    return salted_hmac('hubia.account.edit',json.dumps(state,sort_keys=True)).hexdigest()


def save_access(cursor, account, post, options):
    from bridge.models import Ticket
    choices = {kind:set(post.getlist(kind)) for kind in ('institution','space','agent')}
    for kind, ids in choices.items():
        if ids - {row['id'] for row in options if row['kind']==kind and not row['locked']}:
            raise ValueError(translate_text('Uma das permissões mudou. Reabra a edição.'))
    institutions = choices['institution'] | {r['id'] for r in options if r['kind']=='institution' and r['locked']}
    for row in options:
        if row['kind']=='space' and row['id'] in choices['space'] and row['tenant'] not in institutions:
            raise ValueError(translate_text('Marque a instituição de cada espaço selecionado.'))
    for row in sorted(options,key=lambda row:row['kind']!='institution'):
        if row['locked']:
            continue
        enabled = row['id'] in choices[row['kind']]
        if row['kind']=='agent':
            enabled = not enabled  # The checkbox explicitly labels blocked agents.
        if enabled != row['selected']:
            cursor.execute("SELECT hub_admin.change_account_access(%s,%s,%s,%s)",
                           [account.pk,row['kind'],row['id'],enabled])
            if row['kind']=='agent' and not enabled:
                Ticket.objects.filter(link__user=account,notice_agent=row['id'],state='pending').update(state='stopped',notice_due=None)


@never_cache
@login_required
@require_http_methods(['GET','POST'])
def edit_person(request, ident, deleting=False):
    with transaction.atomic(), connection.cursor() as cursor:
        _,owner = bind_account(cursor,request.user)
        if not owner:
            raise Http404
        account = get_user_model().objects.select_for_update().filter(pk=ident).first()
        if not account:
            raise Http404
        ctx = dict(is_owner=True,account=account,deleting=deleting)
        if deleting:
            if account.pk==request.user.pk:
                raise Http404
            if request.method=='POST':
                try:
                    with transaction.atomic():
                        cursor.execute("SELECT hub_admin.account_target(%s,true)",[ident])
                        cursor.execute("SELECT hub_admin.set_portal_user_active(%s,false)",[str(ident)])
                        from bridge.models import PendingRegistration
                        PendingRegistration.objects.filter(email__iexact=account.email).delete()
                        account.delete()
                    return redirect('people')
                except DataError as exc:
                    if exc.__cause__.sqlstate!='22023':
                        raise
                    ctx['error'] = exc.__cause__.diag.message_primary
            return render(request,'person_edit.html',ctx,status=400 if ctx.get('error') else 200)
        profile = AccountProfile.objects.filter(user=account).first() or AccountProfile(user=account)
        ctx['guide_enabled'] = profile.guide_enabled
        options,ctx['groups'] = access_options(cursor,account)
        from bridge import mail_access
        ctx['mail_institution'] = str(mail_access.INSTITUTION)
        cursor.execute('SELECT hub_admin.account_internal_mail(%s)', [account.pk])
        ctx['internal_mail'] = cursor.fetchone()[0]
        ctx['revision'] = access_revision(account,profile,options,ctx['internal_mail'])
        old_email, old_name = account.email, account.get_full_name()
        form = AccountEditForm(request.POST if request.method=='POST' else None,instance=account,
                    initial=dict(name=account.get_full_name(),phone=profile.phone,company=profile.company))
        ctx['form'] = form
        if request.method=='POST' and request.POST.get('revision')!=ctx['revision']:
            ctx['error'] = translate_text('Este cadastro mudou. Reabra a edição para carregar a versão atual.')
            return render(request,'person_edit.html',ctx,status=409)
        if request.method=='POST' and form.is_valid():
            try:
                with transaction.atomic():
                    save_access(cursor,account,request.POST,options)
                    internal_mail = request.POST.get('internal_mail') == 'on'
                    has_mail_option = any(row['kind'] == 'institution' and row['id'] == ctx['mail_institution'] for row in options)
                    if has_mail_option and internal_mail != ctx['internal_mail']:
                        cursor.execute('SELECT hub_admin.account_internal_mail(%s,%s)', [account.pk, internal_mail])
                    active = request.POST.get('active')=='on'
                    if account.pk!=request.user.pk and active!=account.is_active:
                        cursor.execute("SELECT hub_admin.set_portal_user_active(%s,%s)",[str(ident),active])
                        account.is_active = active
                    account.first_name,account.last_name = form.cleaned_data['name'],''
                    if old_email and old_email.casefold()==account.username.casefold() and old_email!=account.email:
                        account.username = account.email if 0<len(account.email)<=150 else 'cliente-'+uuid4().hex
                    if account.get_full_name()!=old_name:
                        cursor.execute("SELECT hub_admin.change_account_name(%s,%s)",[account.pk,(account.get_full_name() or account.username)[:120]])
                    account.save(update_fields=['username','first_name','last_name','email','is_active'])
                    profile.phone,profile.company = form.cleaned_data['phone'],form.cleaned_data['company']
                    profile.guide_enabled = not request.POST.get("block_guide")
                    if profile._state.adding:
                        profile.save()
                    else:
                        profile.save(update_fields=["phone", "company", "guide_enabled"])
                return redirect('people')
            except ValueError as exc:
                ctx['error'] = str(exc)
            except DataError as exc:
                if exc.__cause__.sqlstate!='22023':
                    raise
                ctx['error'] = exc.__cause__.diag.message_primary
        return render(request,'person_edit.html',ctx,status=400 if form.errors or ctx.get('error') else 200)
