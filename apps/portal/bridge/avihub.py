import json
import logging
import re
from datetime import timedelta

from cryptography.fernet import InvalidToken
from django.utils.translation import gettext as translate_text
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from auth_limits import allow_auth_attempt
from django.db import IntegrityError, transaction
from django.urls import reverse
from uuid import UUID
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from model_provider import cipher
from .avihub_client import Panel, SessionExpired, EXPIRED, PURPOSE
from . import avihub_api as api
from .models import AviHubAgent, AviHubSession

logger = logging.getLogger(__name__)
PESO_EXATO_ORIGIN = "https://avihub.pesoexato.com"


def personal(tenant, user):
    valid = Q(ready=True, expires__isnull=True) | Q(expires__gt=timezone.now())
    return AviHubSession.objects.filter(valid, agent__tenant=tenant, user=user)


def dashboard_setup(cursor, user, spaces):
    tenant = UUID("e7eb1e21-00c7-4abd-8ada-641ce20a91b3")
    allowed = [s["id"] for s in spaces if s["tenant"] == tenant]
    if not allowed:
        return None
    cursor.execute("SELECT set_config('hub.tenant_id', %s, true)", [str(tenant)])
    cursor.execute("""SELECT a.workspace_id, a.id FROM hub_tenancy.workspace_agents a
        JOIN hub_tenancy.workspaces w ON w.tenant_id=a.tenant_id AND w.id=a.workspace_id
        WHERE a.tenant_id=%s AND a.workspace_id=ANY(%s::uuid[]) AND a.active AND a.kind='avihub'
        ORDER BY w.client_onboarding DESC, w.name, w.id LIMIT 1""", [tenant, allowed])
    target = cursor.fetchone()
    if target is None:
        return None
    space, agent = target
    if not AviHubAgent.objects.filter(pk=agent, tenant=tenant, origin=PESO_EXATO_ORIGIN).exists():
        return None
    sessions = personal(tenant, user)
    session = sessions.order_by("-ready", "-pk").only("id", "ready").first()
    url = reverse("use_agent", args=[tenant, space, agent])
    return {"url": url, "ready": bool(session and session.ready),
            "pending_url": f"{url}?conta={session.account_ref}&origem=painel" if session and not session.ready else None,
            "integrations": reverse("integrations", args=[tenant, space])}


def encrypt(panel):
    return cipher().encrypt(json.dumps(panel.cookies).encode())


def restore(session):
    try:
        cookies = json.loads(cipher().decrypt(bytes(session.encrypted)))
    except (InvalidToken, ValueError):
        raise SessionExpired(EXPIRED) from None
    return Panel(session.agent.origin, cookies)


def accounts_for(sessions):
    return sessions.order_by("pk").only("id", "label", "email", "ready")


def account_id(ref):
    match = re.fullmatch(r"conta-([1-9][0-9]{0,18})", ref or "")
    if match and int(match[1]) <= 2**63 - 1:
        return int(match[1])
    raise api.AccessDenied(translate_text("Identificador de conta AviHUB inválido."))


def locked_session(sessions, ref):
    session = sessions.select_related("agent").select_for_update(of=("self",)).filter(pk=account_id(ref)).first()
    if session:
        return session
    raise api.AccessDenied(translate_text("Esta sessão AviHUB está indisponível. Escolha uma conta vinculada."))


def selected_accounts(sessions, refs):
    if not refs:
        raise ValueError(translate_text("Selecione pelo menos uma conta para excluir."))
    ids = {account_id(ref) for ref in refs}
    accounts = list(accounts_for(sessions.filter(pk__in=ids)).select_for_update())
    if len(accounts) != len(ids):
        raise api.AccessDenied(translate_text("A lista de contas mudou. Confira os vínculos e selecione novamente."))
    return accounts


def use_session(session, callback, *args):
    try:
        panel = restore(session)
        result = callback(panel, *args)
    except SessionExpired:
        name = session.account_name
        session.delete()
        raise SessionExpired(f"A sessão AviHUB de {name} expirou. Vincule essa conta novamente.") from None
    session.encrypted = encrypt(panel)
    session.save(update_fields=["encrypted"])
    return result


def linking_help(url, *, pending=False, expired=False):
    reason = ("Sua sessão no AviHUB expirou. " if expired else "")
    reason += "Para consultar operações do AviHUB pela IA, você precisa vincular sua conta do portal AviHUB ao HUB IA."
    if pending:
        step = "Abra a conta que aguarda verificação e confirme o código no HUB IA."
    else:
        step = "Abra o link, entre no HUB IA e use o e-mail e a senha do portal AviHUB. Se solicitado, confirme o código nessa página."
    return {"estado": "vinculo_necessario", "url_vinculo": url,
            "mensagem": f"{reason} {step} Depois, repita a consulta.",
            "instrucao_para_ia": "Mostre ao cliente a mensagem e o link de vínculo."}


def linked_account(tenant, user, agent_id, selection, *, link_url):
    sessions = personal(tenant, user).filter(agent_id=agent_id)
    ready = sessions.filter(ready=True)
    accounts = [session.account_info() for session in accounts_for(ready)]
    if not accounts:
        pending = sessions.filter(ready=False).exists()
        return None, linking_help(link_url if pending else link_url + "?nova=1", pending=pending)
    selected = api.select_account(accounts, selection)
    if selected.get("estado") == "escolher_conta":
        return None, {**selected, "contas":[account_choice(item) for item in selected["contas"]]}
    session = locked_session(ready, selected["id"])
    return session, {"conta":account_choice(selected), "contas":[account_choice(item) for item in accounts]}


def account_choice(account):
    return {"id":account["id"], "nome":account["nome"]}


def query(tenant, user, args, operation, *, link_url):
    sessions = personal(tenant, user)
    ready = sessions.filter(ready=True)
    accounts = [s.account_info() for s in accounts_for(ready)]
    if not accounts:
        pending = sessions.filter(ready=False).exists()
        help_data = linking_help(link_url if pending else link_url + "?nova=1", pending=pending)
        if operation == api.ACCOUNTS:
            help_data["contas"] = []
        return help_data
    if operation == api.ACCOUNTS:
        return {"contas": accounts}
    selected = api.select_account(accounts, args.get("conta"))
    if selected.get("estado") == "escolher_conta":
        return selected
    session = locked_session(ready, selected["id"])
    try:
        result = use_session(session, api.unit_query, operation, args)
    except SessionExpired:
        return linking_help(link_url + "?nova=1", expired=True)
    return {**result, "conta": session.account_info()}


def request_password_reset(agent, user, email):
    email = email.strip()
    try:
        validate_email(email)
        if len(email) > 254:
            raise ValidationError("email")
    except ValidationError:
        raise ValueError("Informe um e-mail válido do AviHUB.") from None
    if not allow_auth_attempt("avihub-reset-user", str(user.pk), 10):
        raise ValueError("Aguarde alguns minutos antes de solicitar outro link.")
    if allow_auth_attempt("avihub-reset-email", email.casefold(), 2):
        Panel(agent.origin).request_password_reset(email)
    return translate_text("Se o e-mail estiver cadastrado no AviHUB, você receberá um link para redefinir a senha.")


def account_label(post):
    label = post.get("apelido", "").strip()
    if len(label) > 80 or (label and not label.isprintable()) or re.fullmatch(r"conta-[0-9]+", label, re.I):
        raise ValueError(translate_text("Escolha um apelido de até 80 caracteres diferente de um ID de conta."))
    return label


def start_session(agent, user, post):
    email = post.get("email", "").strip()
    password = post.get("password", "")
    label = account_label(post)
    if not email or not password or len(email) > 254 or len(password) > 1024:
        raise ValueError("Informe o e-mail e a senha da sua conta AviHUB.")
    panel = Panel(agent.origin)
    ready = panel.authenticate(email=email, password=password)
    now = timezone.now()
    try:
        with transaction.atomic():
            AviHubSession.objects.filter(agent=agent, user=user, ready=False, expires__lte=now).delete()
            return AviHubSession.objects.create(agent=agent, user=user, email=email, label=label,
                ready=ready, encrypted=encrypt(panel), expires=None if ready else now + timedelta(minutes=5))
    except IntegrityError as exc:
        # A raw PostgreSQL error can include the entire session row.
        diag = getattr(exc.__cause__, "diag", None)
        logger.error("Falha ao gravar vínculo AviHUB: SQLSTATE=%s coluna=%s restrição=%s",
                     getattr(diag, "sqlstate", None), getattr(diag, "column_name", None),
                     getattr(diag, "constraint_name", None))
        raise ValueError(translate_text("O HUB IA não conseguiu salvar o vínculo. Peça à administração para verificar o banco do HUB IA.")) from None


def page(request, ctx, agent):
    sessions = personal(agent.tenant, request.user)
    notice_key = f"avihub-accounts-{agent.pk}"
    ctx.update(agent=agent, notice=request.session.pop(notice_key, None))
    from_dashboard = request.GET.get("origem") == "painel"
    verification_suffix = "&origem=painel" if from_dashboard else ""
    mode = "list"
    session = None
    try:
        if request.method == "GET":
            if "conta" in request.GET:
                session = accounts_for(sessions).filter(pk=account_id(request.GET["conta"])).first()
                if not session:
                    raise api.AccessDenied(translate_text("Esta conta está indisponível."))
                mode = "edit"
            elif "recuperar" in request.GET:
                mode = "reset"
            elif "nova" in request.GET:
                mode = "add"
        else:
            post = request.POST
            action = post.get("action")
            if action == "reset_password":
                mode = "reset"
                request.session[notice_key] = request_password_reset(agent, request.user, post.get("email", ""))
                return redirect(f"{request.path}?recuperar=1{verification_suffix}")
            if action == "login":
                mode = "add"
                session = start_session(agent, request.user, post)
                if not session.ready:
                    return redirect(f"{request.path}?conta={session.account_ref}{verification_suffix}")
                if from_dashboard:
                    return redirect("dashboard")
                request.session[notice_key] = "Conta vinculada."
                return redirect(request.path)
            if action in ("review_delete", "delete"):
                selected = selected_accounts(sessions, post.getlist("contas"))
                if action == "delete":
                    sessions.filter(pk__in=[account.pk for account in selected]).delete()
                    count = len(selected)
                    request.session[notice_key] = "Vínculo excluído." if count == 1 else f"{count} vínculos excluídos."
                    return redirect(request.path)
                ctx["selected"] = selected
                mode = "confirm"
            elif action in ("rename", "verify", "sms", "whatsapp"):
                session = locked_session(sessions, post.get("conta"))
                mode = "edit"
                if action == "rename":
                    session.label = account_label(post)
                    session.save(update_fields=["label"])
                    request.session[notice_key] = "Apelido atualizado."
                    return redirect(request.path)
                if session.ready:
                    raise ValueError("Esta conta já está vinculada.")
                if action == "verify":
                    code = post.get("code", "").strip()
                    if not code or len(code) > 64:
                        raise ValueError("Informe o código de verificação.")
                    if not use_session(session, lambda panel: panel.authenticate(code=code)):
                        raise ValueError("O AviHUB ainda requer a verificação da conta.")
                    session.ready = True
                    session.expires = None
                    session.save(update_fields=["ready", "expires"])
                    if from_dashboard:
                        return redirect("dashboard")
                    request.session[notice_key] = "Conta verificada e vinculada."
                    return redirect(request.path)
                use_session(session, Panel.send_code, action)
                request.session[notice_key] = "O AviHUB confirmou o envio do código."
                return redirect(f"{request.path}?conta={session.account_ref}{verification_suffix}")
            else:
                raise ValueError(translate_text("Ação de conta indisponível."))
    except SessionExpired as exc:
        mode = "list"
        session = None
        ctx["error"] = str(exc)
    except ValueError as exc:
        ctx["error"] = str(exc)
    if mode == "list":
        ctx["accounts"] = accounts_for(sessions)
    ctx.update(mode=mode, session=session, from_dashboard=from_dashboard)
    return render(request, "avihub_agent.html", ctx)
