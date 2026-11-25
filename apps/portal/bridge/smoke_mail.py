import base64
import hashlib
import json
import secrets
from contextlib import nullcontext
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from uuid import uuid4

from django.urls import reverse
from django.test import TestCase
from oauth2_provider.models import get_access_token_model
from . import mcp
from .access import MCP
from .smoke import Hidden
from .models import Link, Mailbox, WorkspaceSelection


def verify_gemini(viewer, tenant, space, box):
    callback = "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-qa-hubia_psxt_io"
    def register(uris):
        return viewer.post("/conta/oauth/register/", {"redirect_uris":uris,
                           "token_endpoint_auth_method":"client_secret_post"}, content_type="application/json", secure=True)

    for invalid in (callback + "?other=yes", callback.replace(".com/", ".com.example.org/"),
                    "https://example.org/callback", callback + "/../other"):
        assert register([callback, invalid]).status_code == 400
    registration = register([callback, callback.replace("/r/", "/a/")])
    assert registration.status_code == 201
    app = registration.json()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id":app["client_id"], "redirect_uri":callback, "response_type":"code",
              "state":"gemini-test", "scope":"hub.read offline_access", "code_challenge":challenge,
              "code_challenge_method":"S256"}
    consent = "/conta/oauth/authorize/"
    picker = viewer.get(consent, params, secure=True)
    choice = f"{tenant}:{space}"
    all_choice = f"{tenant}:all"
    assert picker.status_code == 200 and choice.encode() in picker.content and all_choice.encode() in picker.content
    assert viewer.get(consent, {**params, "hub_workspace":f"{uuid4()}:all"}, secure=True).status_code == 404
    other_app = register([callback]).json()["client_id"]
    all_prompt = viewer.get(consent, {**params, "client_id":other_app, "hub_workspace":all_choice}, secure=True)
    assert "todos os espaços atuais e futuros" in all_prompt.content.decode()
    all_fields = Hidden()
    all_fields.feed(all_prompt.content.decode())
    assert all_fields.fields["hub_workspace"] == all_choice
    refusal = viewer.post(consent, all_fields.fields, secure=True)
    assert "error" in parse_qs(urlsplit(refusal["Location"]).query)
    assert not Link.objects.filter(client_id=other_app).exists()
    approval = viewer.post(consent, {**all_fields.fields, "allow":"Autorizar"}, secure=True)
    assert "code" in parse_qs(urlsplit(approval["Location"]).query)
    test_link = Link.objects.get(client_id=other_app, user=box.user, tenant=tenant)
    assert WorkspaceSelection.objects.get(link=test_link).spaces is None
    test_link.delete()
    assert viewer.get(consent, {**params, "hub_workspace":f"{tenant}:{uuid4()}"}, secure=True).status_code == 404
    prompt = viewer.get(consent, {**params, "hub_workspace":choice}, secure=True)
    hidden = Hidden()
    hidden.feed(prompt.content.decode())
    assert hidden.fields["mailbox_offer"] == str(box.pk) and hidden.fields["hub_workspace"] == choice
    reply = viewer.post(consent, {**hidden.fields, "allow":"Autorizar","allow_mail":"on"}, secure=True)
    assert hidden.fields["resource"] == MCP
    assert reply.status_code == 302, ("consentimento Gemini", reply.status_code)
    response = urlsplit(reply["Location"])
    assert reply["Location"].startswith(callback + "?") and parse_qs(response.query)["state"] == ["gemini-test"]
    token = viewer.post("/conta/oauth/token/", {"grant_type":"authorization_code",
                        "client_id":app["client_id"], "client_secret":app["client_secret"], "redirect_uri":callback,
                        "code":parse_qs(response.query)["code"][0], "code_verifier":verifier}, secure=True)
    assert token.status_code == 200
    result = viewer.post("/conta/mcp", {"jsonrpc":"2.0", "id":1, "method":"tools/list"},
                         content_type="application/json", secure=True, HTTP_AUTHORIZATION="Bearer "+token.json()["access_token"])
    assert "ler_email" in {tool["name"] for tool in result.json()["result"]["tools"]}
    assert not WorkspaceSelection.objects.filter(link__client_id=app["client_id"]).exists()
    print("Gemini: espaço individual ou todos os atuais e futuros, recusa, isolamento e PKCE verificados.")


def verify_activation(viewer, tenant, space, box, personal):
    url = reverse("integrations", args=[tenant, space])
    callback = "https://example.org/oauth/callback"
    form = {"action":"create", "name":"IA com leitura", "protocol":"mcp", "callback":callback}
    assert viewer.post(url, form, secure=True).status_code == 200
    link = Link.objects.get(user=box.user, name=form["name"], tenant=tenant)
    assert not box.links.filter(pk=link.pk).exists()

    auth = {"client_id":link.client_id, "redirect_uri":callback, "response_type":"code",
            "code_challenge_method":"S256", "scope":"hub.read", "resource":MCP,
            "code_challenge":base64.urlsafe_b64encode(hashlib.sha256(secrets.token_bytes(32)).digest()).decode().rstrip("=")}
    consent = "/conta/oauth/authorize/"
    def displayed(params):
        page = viewer.get(consent, params, secure=True)
        assert page.status_code == 200
        html = page.content.decode()
        assert 'name="allow_mail" checked' in html and box.address in html
        hidden = Hidden()
        hidden.feed(html)
        return hidden.fields

    fields = displayed(auth)
    denied = viewer.post(consent, fields, secure=True)
    assert "error" in parse_qs(urlsplit(denied["Location"]).query)
    assert not box.links.filter(pk=link.pk).exists()
    accepted = viewer.post(consent, {**fields, "allow":"Autorizar", "allow_mail":"on"}, secure=True)
    assert "code" in parse_qs(urlsplit(accepted["Location"]).query)
    assert box.links.filter(pk=link.pk).exists()
    assert viewer.post(personal, {"action":"grant"}, secure=True).status_code == 302
    viewer.post(consent, {**fields, "allow":"Autorizar", "allow_mail":"on"}, secure=True)
    assert not box.links.filter(pk=link.pk).exists()

    form["name"] = "IA sem leitura"
    assert viewer.post(url, form, secure=True).status_code == 200
    second = Link.objects.get(user=box.user, name=form["name"], tenant=tenant)
    fields = displayed({**auth, "client_id":second.client_id})
    accepted = viewer.post(consent, {**fields, "allow":"Autorizar"}, secure=True)
    assert "code" in parse_qs(urlsplit(accepted["Location"]).query)
    assert not box.links.filter(pk=second.pk).exists()
    print("Consentimento de e-mail na ativação MCP: padrão marcado, recusa, desmarcação e revogação verificados.")


def verify_send_consent(viewer, tenant, space, box, *, email_only=False):
    callback = "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-mail-send-qa"
    prefix = "/conta/email/oauth" if email_only else "/conta/oauth"
    endpoint = "/conta/mcp/email" if email_only else "/conta/mcp"
    app = viewer.post(prefix + "/register/", {"redirect_uris":[callback], "token_endpoint_auth_method":"none"},
                      content_type="application/json", secure=True).json()
    verifier = secrets.token_urlsafe(48)
    auth = {"client_id":app["client_id"], "redirect_uri":callback, "response_type":"code",
            "scope":"hub.read hub.mail.send", "hub_workspace":f"{tenant}:all" if email_only else f"{tenant}:{space}", "code_challenge_method":"S256",
            "code_challenge":base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
    consent = prefix + "/authorize/"
    def fields(checked):
        reply = viewer.get(consent, auth, secure=True)
        assert reply.status_code == 200
        html = reply.content.decode()
        assert 'name="allow_mail_send"' in html and ('name="allow_mail_send" checked' in html) == checked
        hidden = Hidden()
        hidden.feed(html)
        return hidden.fields

    denied = viewer.post(consent, {**fields(False), "allow_mail_send":"on"}, secure=True)
    assert "error" in parse_qs(urlsplit(denied["Location"]).query)
    assert not Link.objects.filter(client_id=app["client_id"]).exists()
    grant = {**fields(False), "allow":"Autorizar", "allow_mail_send":"on"}
    accepted = viewer.post(consent, grant, secure=True)
    code = parse_qs(urlsplit(accepted["Location"]).query)["code"][0]
    token = viewer.post(prefix + "/token/", {"client_id":app["client_id"], "redirect_uri":callback,
                        "grant_type":"authorization_code", "code":code, "code_verifier":verifier}, secure=True)
    assert token.status_code == 200
    def tools():
        reply = viewer.post(endpoint, {"jsonrpc":"2.0","id":1,"method":"tools/list"},
                            content_type="application/json", secure=True,
                            HTTP_AUTHORIZATION="Bearer " + token.json()["access_token"])
        return {spec["name"] for spec in reply.json()["result"]["tools"]}
    assert "enviar_email" in tools() and "ler_email" not in tools()
    if email_only:
        headers = {"HTTP_AUTHORIZATION":"Bearer " + token.json()["access_token"]}
        def rpc(method, params=None):
            return viewer.post(endpoint, {"jsonrpc":"2.0","id":1,"method":method,"params":params or {}},
                               content_type="application/json", secure=True, **headers).json()
        info = rpc("initialize")["result"]
        assert info["serverInfo"]["name"].startswith("Meu Email ") and "events" not in info["capabilities"]
        assert tools() == {"enviar_email", "editar_assinatura_email", "consultar_assinatura_email"}
        assert "error" in rpc("tools/call", {"name":"list_agents"})
        assert "error" in rpc("events/list")
        assert viewer.post("/conta/mcp", {"jsonrpc":"2.0","id":1,"method":"tools/list"},
                           content_type="application/json", secure=True, **headers).status_code == 401
        test_link = Link.objects.get(client_id=app["client_id"], user=box.user)
        box.links.add(test_link)
        assert {"listar_emails_novos", "ler_email"} <= tools()
        box.links.remove(test_link)
        metadata = viewer.get("/.well-known/oauth-protected-resource" + endpoint, secure=True).json()
        assert metadata["resource"].endswith(endpoint) and "hub.notify" not in metadata["scopes_supported"]
        server = viewer.get("/.well-known/oauth-authorization-server/conta/email", secure=True).json()
        assert metadata["authorization_servers"] == [server["issuer"]]
    assert viewer.post(consent, {**fields(True), "allow":"Autorizar"}, secure=True).status_code == 302
    assert "enviar_email" not in tools() and viewer.post(consent, grant, secure=True).status_code == 400
    assert not box.send_links.filter(client_id=app["client_id"]).exists()
    Link.objects.get(client_id=app["client_id"]).delete()
    if email_only:
        print("MCP de e-mail: descoberta, registro, isolamento de tokens, catálogo exclusivo e revogação verificados.")
    print("Envio no consentimento MCP: aceite, recusa, envio sem leitura e revogação verificados com token OAuth.")


def verify_outbound(manager, viewer, link, box, space):
    permissions = reverse("ai_workspaces")
    membership = f"/conta/espacos/{link.tenant}/{space}/gestao/membros/"
    def role(value):
        response = manager.post(membership, {"action":"role", "username":link.user.username, "role":value}, secure=True)
        assert response.status_code == 302
    def save(client, enabled, initial):
        fields = {"link":str(link.pk), f"mode-{link.pk}":"selected", f"spaces-{link.pk}":str(space),
                  f"initial-{link.pk}":str(space), f"mailbox-{link.pk}":str(box.pk),
                  f"initial-read-{link.pk}":"0", f"initial-send-{link.pk}":str(int(initial))}
        if enabled:
            fields[f"send-{link.pk}"] = "on"
        return client.post(permissions, fields, secure=True).status_code

    token = get_access_token_model().objects.get(application__client_id=link.client_id, user=link.user)
    def rpc(method, params=None):
        return viewer.post("/conta/mcp", {"jsonrpc":"2.0", "id":1, "method":method, "params":params or {}},
                           content_type="application/json", secure=True, HTTP_AUTHORIZATION="Bearer " + token.token).json()
    def listed():
        return {spec["name"] for spec in rpc("tools/list")["result"]["tools"]}
    args = {"destinatario":"qa@example.org","assunto":"Teste","mensagem":"Olá","request_id":str(uuid4())}
    def send(**overrides):
        return rpc("tools/call", {"name":"enviar_email", "arguments":{**args, **overrides}})

    personal = reverse("my_email", args=[link.tenant])
    def signature_form():
        hidden = Hidden()
        hidden.feed(viewer.get(personal, secure=True).content.decode())
        return {"mailbox":str(box.pk), "revision":hidden.fields["revision"]}
    def signature_call(name, args=None):
        return rpc("tools/call", {"name":name, "arguments":args or {}})
    signature = "<b>Equipe</b><br>Desenvolvimento"
    fields = signature_form()
    updates = {"action":"signature", "signature_html":signature, **fields}
    assert manager.post(personal, updates, secure=True).status_code == 400
    preview = viewer.post(personal, {**updates, "action":"preview_signature"}, secure=True)
    assert preview.json()["html"] == signature
    box.refresh_from_db()
    assert not box.signature_html
    assert viewer.post(personal, updates, secure=True).status_code == 302
    assert viewer.post(personal, {**updates, "signature_html":"antiga"}, secure=True).status_code == 400
    assert "error" in signature_call("editar_assinatura_email", {"html":"Sem permissão"})

    assert save(manager, True, False) == 404
    assert save(viewer, True, False) == 403
    role("member")
    verify_send_consent(viewer, link.tenant, space, box)
    verify_send_consent(viewer, link.tenant, space, box, email_only=True)
    assert "enviar_email" not in listed()
    assert save(viewer, True, False) == 302
    assert box.send_links.filter(pk=link.pk).exists() and not box.links.filter(pk=link.pk).exists()
    assert "enviar_email" not in listed() and "error" in send()  # Original read-only token.
    token.scope += " hub.mail.send"
    token.save(update_fields=["scope"])
    assert "enviar_email" in listed() and "ler_email" not in listed()
    assert {"consultar_assinatura_email", "editar_assinatura_email"} <= listed()
    def get_signature():
        return json.loads(signature_call("consultar_assinatura_email")["result"]["content"][0]["text"])["html"]
    assert get_signature() == signature
    assert "error" in signature_call("editar_assinatura_email", {"html":"<form>Inválido</form>"})
    final_signature = "<strong>Equipe técnica</strong>"
    assert "result" in signature_call("editar_assinatura_email", {"html":final_signature})
    assert get_signature() == final_signature
    assert "Equipe técnica" in viewer.get(personal, secure=True).content.decode()
    peer = Link.objects.create(user=link.user, tenant=link.tenant, space=space, protocol="mcp",
                               name="Outra conexão SMTP", client_id=str(uuid4()))
    assert "enviar_email" not in {t["name"] for t in mcp.available_tools(peer, {"can_send_mail":True}, False)}
    with patch("bridge.mail_protocol.submit", return_value="aceito_pelo_smtp") as smtp:
        assert "error" in send(assunto="Teste\r\nCc: copia@example.org")
        assert "error" in send(destinatario="a@example.org, b@example.org")
        with TestCase.captureOnCommitCallbacks(execute=True):
            assert "result" in send()
        smtp.assert_called_once_with(box.address, "test-only-fixture", args["destinatario"], args["assunto"], args["mensagem"], final_signature)
        assert "result" in signature_call("editar_assinatura_email", {"html":""})
        assert get_signature() == ""
        assert json.loads(send()["result"]["content"][0]["text"])["estado"] == "aceito_pelo_smtp"
        assert "error" in send(mensagem="Outra")
        role("viewer")
        assert "enviar_email" not in listed() and "error" in send()
        assert "error" in signature_call("editar_assinatura_email", {"html":"Sem permissão"})
        assert save(viewer, False, True) == 302 and not box.send_links.exists()
        assert smtp.call_count == 1
    print("Envio MCP: permissão por IA, escopo OAuth, isolamento, revogação e idempotência testados com SMTP simulado.")


def verify_mail(manager, viewer, tenant, space, user):
    config = reverse("email_connector", args=[tenant])
    personal = reverse("my_email", args=[tenant])
    config_data = {"domain":"example.org", "enabled":"on"}
    assert viewer.post(config, config_data, secure=True).status_code == 404
    assert manager.post(config, config_data, secure=True).status_code == 302
    form = {"action":"connect", "address":"qa@example.org", "password":"test-only-fixture"}
    assert viewer.post(personal, {**form, "address":"qa@other.org"}, secure=True).status_code == 400
    with patch("bridge.mail_views.inbox", return_value=nullcontext()):
        assert viewer.post(personal, form, secure=True).status_code == 302
    box = Mailbox.objects.get(user=user, provider_id=tenant)
    assert form["password"].encode() not in bytes(box.password_cipher)
    assert form["password"].encode() not in viewer.get(personal, secure=True).content
    assert box.address.encode() not in manager.get(personal, secure=True).content
    verify_gemini(viewer, tenant, space, box)
    verify_activation(viewer, tenant, space, box, personal)
    verify_outbound(manager, viewer, Link.objects.get(user=user, tenant=tenant, name="Gemini"), box, space)
    link = Link.objects.create(user=user, tenant=tenant, space=space, protocol="mcp", name="IA QA", client_id=str(uuid4()))

    def granted():
        return {tool["name"] for tool in mcp.available_tools(link, {}, False)} & {"listar_emails_novos", "ler_email"}

    assert not granted()
    assert viewer.post(personal, {"action":"grant", "links":str(uuid4())}, secure=True).status_code == 400
    assert viewer.post(personal, {"action":"grant", "links":str(link.pk)}, secure=True).status_code == 302
    assert granted() == {"listar_emails_novos", "ler_email"}
    with patch("bridge.mail_views.inbox", side_effect=ValueError("Falha no IMAP")):
        assert viewer.post(personal, form, secure=True).status_code == 400
        assert granted()
    assert manager.post(config, {"domain":"example.org"}, secure=True).status_code == 302
    assert not granted()
    assert viewer.post(personal, {"action":"disconnect"}, secure=True).status_code == 302
    assert not Mailbox.objects.filter(pk=box.pk).exists()
    print("E-mail institucional: caixa individual, senha cifrada, consentimento por conexão, isolamento e revogação verificados.")
