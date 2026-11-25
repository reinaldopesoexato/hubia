import hashlib
import json
import secrets
from uuid import UUID, uuid4
from django import forms
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from oauth2_provider.models import get_application_model, get_access_token_model, get_refresh_token_model, get_grant_model

from agent_runtime import https_url
from model_provider import cipher
from space_views import space_context
from .access import MCP, MAIL_MCP, email_title, context
from .a2a import CARD, query_peer
from .models import Link, Ticket, Subscription, Delivery
from .network import RemoteError


class ConnectionForm(forms.Form):
    name = forms.CharField(label="Nome da conexão",max_length=100)
    protocol = forms.ChoiceField(label="Tipo",choices=[
        ("mcp","Dot ou outro cliente MCP (OAuth)"), ("mail","MCP exclusivo de e-mail (OAuth)"),("a2a","Outro agente consulta o HUB (A2A)"),
        ("peer","HUB consulta um agente remoto (A2A)")])
    callback = forms.URLField(label="URI de retorno OAuth do cliente MCP",required=False,assume_scheme="https")
    card = forms.URLField(label="URL do Agent Card remoto",required=False,assume_scheme="https")
    token = forms.CharField(label="Bearer do agente remoto, se exigido",required=False,max_length=2048,widget=forms.PasswordInput)
    writable = forms.BooleanField(label="Permitir que o cliente MCP responda às minhas tarefas",required=False)

    def clean(self):
        data = super().clean()
        field = {"mcp":"callback","mail":"callback","peer":"card"}.get(data.get("protocol"))
        if field:
            try:
                https_url(data.get(field,""))
            except ValueError:
                self.add_error(field,"Informe a URL HTTPS exata informada pelo serviço.")
        return data


def create_connection(user,tenant,space,data):
    app_cls = get_application_model()
    email_only = data["protocol"] == "mail"
    protocol = "mcp" if email_only else data["protocol"]
    secret = secrets.token_urlsafe(48)
    link = Link(user=user,tenant=tenant,space=space,name=data["name"],protocol=protocol,
                client_id=str(uuid4()),email_only=email_only,writable=protocol=="mcp" and not email_only and data["writable"])
    if protocol=="mcp":
        app = app_cls.objects.create(user=user,name=data["name"],client_type="confidential",
                    authorization_grant_type="authorization-code",redirect_uris=data["callback"],client_secret=secret)
        link.client_id=app.client_id
    elif protocol=="a2a":
        link.key_digest=hashlib.sha256(secret.encode()).hexdigest()
    else:
        secret = None
        link.peer_config=cipher().encrypt(json.dumps({"card_url":data["card"],"token":data["token"]}).encode())
    link.save()
    return link,secret


def revoke(link):
    app = get_application_model().objects.filter(client_id=link.client_id).first() if link.protocol=="mcp" else None
    if app and app.user_id==link.user_id:
        app.delete()
    elif app:
        for model in (get_refresh_token_model(),get_access_token_model(),get_grant_model()):
            model.objects.filter(application=app,user=link.user).delete()
    link.delete()


@sensitive_post_parameters("token")
@login_required
@never_cache
@require_http_methods(["GET","POST"])
def integrations(request, tenant, space):
    remote_task = None
    secret = None
    created_link = None
    error = None
    form = ConnectionForm()
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = space_context(cursor,request.user,tenant,space)
        cursor.execute("SELECT role FROM hub_tenancy.memberships WHERE tenant_id=%s",[tenant])
        viewer = cursor.fetchone()[0]=="viewer"
        links = Link.objects.filter(tenant=tenant,space=space,user=request.user)
        if request.method=="POST":
            action = request.POST.get("action")
            if action=="create":
                form = ConnectionForm(request.POST)
                if form.is_valid():
                    if viewer and (form.cleaned_data["writable"] or form.cleaned_data["protocol"]=="peer"):
                        form.add_error(None,"Seu papel permite somente consultas.")
                    else:
                        created_link,secret=create_connection(request.user,tenant,space,form.cleaned_data)
                        if not secret:
                            return redirect("integrations",tenant,space)
                        form=ConnectionForm()
            else:
                try:
                    link = links.select_for_update().get(pk=UUID(request.POST.get("link","")))
                except (Link.DoesNotExist,ValueError):
                    raise Http404
                if action=="revoke":
                    revoke(link)
                    return redirect("integrations",tenant,space)
                if viewer:
                    raise Http404
                if action=="send" and not link.email_only and link.protocol in ("mcp","peer"):
                    question = request.POST.get("question","").strip()
                    try:
                        req = UUID(request.POST.get("request_id",""))
                    except ValueError:
                        raise Http404
                    if 1<=len(question)<=2000:
                        ticket,new = Ticket.objects.get_or_create(link=link,request_id=str(req),defaults={"question":question})
                        if new and link.protocol=="peer":
                            remote_task=(link,ticket)
                        elif new:
                            subs = Subscription.objects.filter(link=link,expires__gt=timezone.now())
                            Delivery.objects.bulk_create([Delivery(ticket=ticket,subscription=sub,due=timezone.now()) for sub in subs])
                        if not remote_task:
                            return redirect("integrations",tenant,space)
                    else:
                        error="Informe uma tarefa de até 2.000 caracteres."
                elif action=="poll" and link.protocol=="peer":
                    try:
                        ticket=Ticket.objects.get(link=link,pk=UUID(request.POST.get("ticket","")),state__in=["waiting","attention"])
                    except (ValueError,Ticket.DoesNotExist):
                        raise Http404
                    remote_task=(link,ticket)
                else:
                    raise Http404
        ctx.update(form=form,links=list(links.order_by("name")),secret=secret,created_link=created_link,
                   mcp_url=MCP,mail_mcp=MAIL_MCP,mail_title=email_title(ctx["organization"]),card_url=CARD,viewer=viewer,error=error)
        for link in ctx["links"]:
            link.jobs = list(Ticket.objects.filter(link=link).order_by("-created")[:20])
            link.subscribed = Subscription.objects.filter(link=link,expires__gt=timezone.now()).exists()
            link.nonce = uuid4()
            for job in link.jobs:
                job.deliveries = list(job.delivery_set.values_list("state",flat=True))
    if remote_task:
        link,ticket=remote_task
        try:
            conf = json.loads(cipher().decrypt(bytes(link.peer_config)))
            state,answer,remote_id=query_peer(conf,ticket.question,ticket.request_id,ticket.remote_id or None)
        except (RemoteError,ValueError,TypeError,KeyError,AttributeError) as exc:
            state,answer,remote_id=("waiting" if ticket.remote_id else "failed"),"Sem confirmação de resposta do agente remoto.",ticket.remote_id
        with transaction.atomic():
            context(link,writing=True)
            Ticket.objects.filter(pk=ticket.pk,state__in=["pending","waiting","attention"]).update(
                state=state,answer=answer[:16000],remote_id=remote_id)
        return redirect("integrations",tenant,space)
    return render(request,"integrations.html",ctx,status=400 if error or form.errors else 200)
