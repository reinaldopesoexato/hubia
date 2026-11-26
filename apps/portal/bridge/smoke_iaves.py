"""IAves end-to-end fixture on the disposable portal database."""
import io
import json
import os
import secrets
import tarfile
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

from bs4 import BeautifulSoup
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model

from create_user import create_account
from iaves_brand import NAME_MARKDOWN, display_name
from portal_identity import bind_account
from . import iaves, iaves_metrics, library, library_sync
from .models import (GitHubConnection, IAVES_AGENT_ID, IAvesFeedback, IAvesMemory, IAvesSettings, Link,
                     NoteLibrary, PESO_EXATO_TENANT_ID)


def verify_iaves(owner, outsider):
    literal = "`consultar_iaves` Business/IAves/Pesagem.md https://example.org/iaves hub.iaves.memory"
    assert display_name(f"**IAVES** e iaves. {literal}") == f"IAves e IAves. {literal}"
    assert display_name(f"**IA**ves e iAVES. {literal}", markdown=True) == f"{NAME_MARKDOWN} e {NAME_MARKDOWN}. {literal}"
    tenant, agent = PESO_EXATO_TENANT_ID, IAVES_AGENT_ID
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute("SELECT hub_admin.portal_principal(%s)", [str(owner.pk)])
        principal = cursor.fetchone()[0]
        cursor.execute("INSERT INTO hub_tenancy.tenants(id,kind) VALUES(%s,'ORGANIZATION')", [tenant])
        cursor.execute("INSERT INTO hub_tenancy.memberships(tenant_id,user_id,role) VALUES(%s,%s,'institution_owner')",
                       [tenant,principal])
        cursor.execute("""INSERT INTO hub_tenancy.institutions(tenant_id,slug,name,owner_id)
            VALUES(%s,'peso-exato-qa','Peso Exato QA',%s)""", [tenant,principal])
        workspace_ids = {}
        for slug, name, clients in (("principal", "Principal", False), ("clientes-peso-exato", "Clientes Peso Exato", True)):
            cursor.execute("""INSERT INTO hub_tenancy.workspaces(tenant_id,slug,name,client_onboarding)
                VALUES(%s,%s,%s,%s) RETURNING id""", [tenant,slug,name,clients])
            workspace_ids[slug] = cursor.fetchone()[0]
        common, space = workspace_ids["principal"], workspace_ids["clientes-peso-exato"]
        cursor.execute("INSERT INTO hub_tenancy.workspace_members(tenant_id,workspace_id,user_id) VALUES(%s,%s,%s)",
                       [tenant,common,principal])
        cursor.execute("SELECT workspace_id FROM hub_tenancy.agent_spaces WHERE tenant_id=%s AND agent_id=%s", [tenant,agent])
        assert {row[0] for row in cursor.fetchall()} == {common,space}
        customer = create_account("iaves-qa-" + secrets.token_hex(8), secrets.token_urlsafe(32))
        other_customer = create_account("iaves-qa-" + secrets.token_hex(8), secrets.token_urlsafe(32))
        cursor.execute("""SELECT role FROM hub_tenancy.memberships WHERE tenant_id=%s
            AND user_id=hub_admin.portal_principal(%s)""", [tenant,str(customer.pk)])
        assert cursor.fetchone() == ("viewer",)
        cursor.execute("SET LOCAL ROLE hub_portal")

    def signed_in(user):
        browser = Client(HTTP_HOST="hubia.psxt.io")
        browser.force_login(user)
        return browser

    buyer, staff, stranger = map(signed_in, (customer, owner, outsider))
    agent_url = reverse("use_agent", args=[tenant,space,agent])
    memory_url = reverse("iaves_memory", args=[tenant,space,agent])
    staff_memory = reverse("iaves_memory", args=[tenant,common,agent])
    config_url = reverse("institution_agent_edit", args=[tenant,agent])
    dashboard = buyer.get(reverse("dashboard"), secure=True)
    assert dashboard.status_code == 200 and memory_url.encode() in dashboard.content and agent_url.encode() in dashboard.content
    assert b'<b>IA</b>ves' in dashboard.content
    assert buyer.get(reverse("use_agent", args=[tenant,common,agent]), secure=True).status_code == 404
    assert stranger.get(memory_url, secure=True).status_code == 404
    assert buyer.get(config_url, secure=True).status_code == 404
    assert iaves.DEFAULT_MODEL.encode() in staff.get(config_url, secure=True).content

    repository = {"id":library.IAVES_REPOSITORY_ID,"name":"pesoexatobalancas/Obsidian","branch":"main"}
    source = GitHubConnection.objects.create(tenant=tenant,organization="pesoexatobalancas",app_id=961721,
                                             slug="iaves-fixture",private_key=b"fixture",repositories=[repository])
    snapshot = NoteLibrary.objects.create(connection=source,name="Biblioteca QA",repository_id=repository["id"])
    source_label = "Fonte base de conhecimento IAves"
    filename = "Pesagem CV e Uniformidade.md"
    note_path = f"Business/IAves/Pesagem/{filename}"
    revision = "4" * 40
    source_details = [{"arquivo":filename, "revisao":revision}]
    private_source = f"https://github.com/pesoexatobalancas/Obsidian/blob/{revision}/{quote(note_path)}"
    private_raw = f"https://raw.githubusercontent.com/pesoexatobalancas/Obsidian/{revision}/{quote(note_path)}"
    private_model = "https://github.com/reinaldopesoexato/hubia"
    public_reference = "https://example.org/manejo"
    wiki_reference = "Business/IAves/Outra nota relacionada"
    wiki_manual = "EQUIPMENTS/Manuais/Referência relacionada"
    excerpt = (f"Uniformidade do plantel depende de pesos representativos. [Nota de manejo]({private_source}) "
               f"Cópia: {private_raw}. [Referência pública]({public_reference}). "
               f"Registro interno: {note_path}, revisão {revision}. Veja [[{wiki_reference}]] e [[{wiki_manual}]].")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        root = tarfile.TarInfo("obsidian/")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, body in ((note_path, excerpt),
                           ("Business/IAves2/Restrito.md", "Uniformidade do plantel. Conteúdo de outra pasta.")):
            data = body.encode()
            member = tarfile.TarInfo("obsidian/" + name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    archive.seek(0)

    def mcp_connection(name):
        application = get_application_model().objects.create(user=customer, name=name, client_type="public",
                                                                authorization_grant_type="authorization-code")
        link = Link.objects.create(user=customer, tenant=tenant,space=space,name=name,protocol="mcp",
                                   client_id=application.client_id)
        token = get_access_token_model().objects.create(application=application,user=customer,
                                                        token=secrets.token_urlsafe(32),scope="hub.read",
                                                        expires=timezone.now() + timedelta(minutes=10))
        return link, token

    link, token = mcp_connection("IAves QA")

    def rpc(method, params=None, auth=token):
        reply = buyer.post("/conta/mcp", {"jsonrpc":"2.0","id":1,"method":method,"params":params or {}},
                           content_type="application/json", secure=True, HTTP_AUTHORIZATION="Bearer " + auth.token)
        assert reply.status_code == 200, reply.status_code
        return reply.json()

    def call(name, *, auth=token, **arguments):
        return rpc("tools/call", {"name":name,"arguments":arguments}, auth=auth)

    def payload(response):
        assert "result" in response, response
        return json.loads(response["result"]["content"][0]["text"])

    with TemporaryDirectory() as directory, patch.dict(os.environ, {"HUBIA_KNOWLEDGE_DIR":directory}):
        library_sync.write_snapshot(archive, library.snapshot_path(snapshot), repository["name"], revision)
        tools = {tool["name"]:tool for tool in rpc("tools/list")["result"]["tools"]}
        assert tools["consultar_iaves"]["annotations"]["readOnlyHint"] and "memorizar_iaves" not in tools
        assert "cadeia" in tools["consultar_iaves"]["description"]
        instructions = rpc("initialize")["result"]["instructions"]
        assert "consultar_iaves" in instructions and NAME_MARKDOWN in instructions
        assert "error" in call("memorizar_iaves", chave="Preferência", valor="Relatórios em quilogramas")
        assert "error" in call("consultar_iaves", pergunta="Uniformidade do plantel", space_id=str(common))
        question = "Como avaliar a uniformidade do plantel?"
        context = payload(call("consultar_iaves", pergunta=question))
        assert context["estado"] == "contexto" and "memoria" not in context
        assert context["fonte"] == source_label and len(context["fontes"]) == 1
        assert NAME_MARKDOWN in context["instrucao"]
        note = context["fontes"][0]
        assert set(note) == {"texto"}
        assert public_reference in note["texto"] and private_source not in note["texto"] and private_raw not in note["texto"]
        default_output = json.dumps(context, ensure_ascii=False)
        assert filename not in default_output and "Business/IAves/" not in default_output and revision not in default_output
        assert "Outra nota relacionada" not in default_output and wiki_manual not in default_output
        source_question = "Quais foram as fontes?"
        source_ref = context["referencia_fontes"]
        detail = payload(call("consultar_iaves", pergunta=source_question, referencia_fontes=source_ref))
        assert detail["estado"] == "fontes" and detail["fontes"] == source_details
        assert payload(call("consultar_iaves", pergunta=f"Detalhar fontes: {source_ref}")) == detail
        assert note_path not in json.dumps(detail) and private_source not in json.dumps(detail)
        direct_question = question + " Informe também o nome e a revisão das fontes."
        direct_detail = payload(call("consultar_iaves", pergunta=direct_question))
        assert direct_detail["estado"] == "fontes" and direct_detail["fontes"] == source_details
        missing_ref = payload(call("consultar_iaves", pergunta=source_question))
        assert missing_ref["estado"] == "pergunta"
        assert filename not in json.dumps(missing_ref) and revision not in json.dumps(missing_ref)
        assert "error" in call("consultar_iaves", pergunta=source_question, referencia_fontes="invalida")
        _, separate_token = mcp_connection("IAves QA outro vínculo")
        assert "error" in call("consultar_iaves", auth=separate_token, pergunta=source_question, referencia_fontes=source_ref)
        request_id = uuid4()
        for _ in range(2):
            assert buyer.post(agent_url, {"pergunta":question,"request_id":request_id}, secure=True).status_code == 302
        content = buyer.get(agent_url, secure=True).content.decode()
        assert "Uniformidade do plantel depende" in content and content.count(question) == 1
        assert public_reference in content and private_source not in content and private_raw not in content
        assert source_label in BeautifulSoup(content, "html.parser").get_text()
        assert "<b>IA</b>ves" in content and filename not in content and note_path not in content and revision not in content
        assert wiki_reference not in content and "Outra nota relacionada" not in content and wiki_manual not in content
        detail_args = {"fontes": str(request_id)}
        source_page = buyer.get(agent_url, detail_args, secure=True)
        assert source_page.status_code == 200 and filename.encode() in source_page.content
        assert revision.encode() in source_page.content and note_path.encode() not in source_page.content
        assert staff.get(agent_url, detail_args, secure=True).status_code == 404
        followup = buyer.post(agent_url, {"pergunta":source_question,"request_id":uuid4()}, secure=True)
        assert followup.status_code == 302 and f"fontes={request_id}" in followup.url

        # Exercise a response stored before the source was stripped from the portal history.
        legacy = {"estado":"contexto", "fontes":[{"caminho":note_path, "fonte":private_source, "texto":excerpt}]}
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("UPDATE hub_tenancy.agent_runs SET answer=%s WHERE id=%s", [json.dumps(legacy),request_id])
            cursor.execute("SET LOCAL ROLE hub_portal")
        history = buyer.get(agent_url, secure=True).content.decode()
        assert source_label in BeautifulSoup(history, "html.parser").get_text() and public_reference in history
        assert filename not in history and note_path not in history and revision not in history
        legacy_detail = buyer.get(agent_url, detail_args, secure=True).content.decode()
        assert filename in legacy_detail and revision in legacy_detail and note_path not in legacy_detail
        assert private_source not in history + legacy_detail and private_raw not in history + legacy_detail

        feedback = {"action":"feedback","run_id":request_id,"helpful":"no"}
        other_space = reverse("use_agent", args=[tenant,common,agent])
        assert staff.post(other_space, feedback, secure=True).status_code == 404
        assert staff.post(agent_url, feedback, secure=True).status_code == 404
        assert buyer.post(agent_url, feedback, secure=True).status_code == 302
        admin_url = reverse("administration")
        assert buyer.get(admin_url, secure=True).status_code == 403
        negative_page = staff.get(admin_url, {"dias_iaves":"7"}, secure=True)
        assert negative_page.status_code == 200 and note_path.encode() in negative_page.content
        with connection.cursor() as cursor:
            bind_account(cursor, owner)
        negative = iaves_metrics.report(7)
        assert negative["days"] == 7 and negative["signups"] == 2 and negative["percentage"] == 0, negative
        assert negative["notes"] == [(note_path, 1)]
        assert buyer.post(agent_url, {**feedback,"helpful":"yes"}, secure=True).status_code == 302
        positive_page = staff.get(admin_url, secure=True)
        assert positive_page.status_code == 200 and b"50%" in positive_page.content
        with connection.cursor() as cursor:
            bind_account(cursor, owner)
        positive = iaves_metrics.report(30)
        assert positive["signups"] == 2 and positive["got_help"] == 1 and positive["percentage"] == 50, positive
        assert positive["median_minutes"] >= 0 and not positive["notes"]
        assert IAvesFeedback.objects.filter(run=request_id,user=customer,helpful=True).count() == 1

        token.scope += " hub.iaves.memory"
        token.save(update_fields=["scope"])
        assert payload(call("memorizar_iaves", chave="Unidade", valor="Usar quilogramas"))["estado"] == "memorizado"
        item = IAvesMemory.objects.get(agent=agent,user=customer,key="Unidade")
        assert "Usar quilogramas" in buyer.get(memory_url, secure=True).content.decode()
        assert "Usar quilogramas" not in staff.get(staff_memory, secure=True).content.decode()
        assert staff.post(staff_memory, {"action":"delete","item":item.pk}, secure=True).status_code == 404
        assert buyer.post(memory_url, {"action":"edit","item":item.pk,"key":"Unidade","value":"Usar gramas"},
                          secure=True).status_code == 302
        assert payload(call("consultar_iaves", pergunta=question))["memoria"] == [{"chave":"Unidade","valor":"Usar gramas"}]
        saved_link = payload(call("memorizar_iaves", chave="Referência", valor=private_model))
        assert saved_link == {"estado":"memorizado","chave":"Referência","valor":""}
        assert private_model not in json.dumps(payload(call("consultar_iaves", pergunta=question))["memoria"])
        assert IAvesMemory.objects.filter(agent=agent,user=customer,key="Referência",value=private_model).exists()

        credential = "somente-fixture-iaves"
        form = {"provider":"openai","model":iaves.DEFAULT_MODEL,"token":credential}
        assert staff.post(config_url, form, secure=True).status_code == 302
        assert credential.encode() not in staff.get(config_url, secure=True).content
        assert bytes(IAvesSettings.objects.get(agent=agent).encrypted_key) != credential.encode()
        tools = {tool["name"]:tool for tool in rpc("tools/list")["result"]["tools"]}
        assert not tools["consultar_iaves"]["annotations"]["readOnlyHint"] and "memorizar_iaves" in tools
        memorable = "Usamos pesagem semanal"
        result = {"resposta":(f"O **iAVES** recomenda conferir a nota local. [Projeto]({private_model}) {public_reference} "
                              f"Fonte: {note_path}, revisão {revision}."),
                  "pergunta":f"Qual é a idade do lote? {private_raw}",
                  "memorias":[{"chave":"Pesagem","valor":memorable}, {"chave":"Inventado","valor":"Invenção da fixture"}]}
        with patch("bridge.iaves.model_request", return_value=result) as model:
            answer = payload(call("consultar_iaves", pergunta=memorable + ". " + question))
            assert answer["estado"] == "pergunta" and answer["pergunta"].startswith("Qual é a idade do lote?")
            assert private_raw not in answer["pergunta"] and private_model not in answer["resposta"]
            assert f"O {NAME_MARKDOWN} recomenda" in answer["resposta"]
            assert public_reference in answer["resposta"] and answer["itens_memorizados"] == ["Pesagem"]
            assert answer["fonte"] == source_label and filename not in json.dumps(answer) and revision not in json.dumps(answer)
            model_note = model.call_args.args[2]["fontes"][0]
            assert set(model_note) == {"texto"}
            assert private_source not in model_note["texto"] and private_raw not in model_note["texto"]
            assert filename not in model_note["texto"] and note_path not in model_note["texto"] and revision not in model_note["texto"]
            assert wiki_reference not in model_note["texto"] and wiki_manual not in model_note["texto"]
            assert buyer.post(agent_url, {"pergunta":question, "request_id":uuid4()}, secure=True).status_code == 302
            content = buyer.get(agent_url, secure=True).content.decode()
            assert "O IAves recomenda" in BeautifulSoup(content, "html.parser").get_text()
            assert "<b>IA</b>ves" in content and "**iAVES**" not in content
        with patch("bridge.iaves.model_request", return_value={"resposta":private_model,"pergunta":"","memorias":[]}):
            assert payload(call("consultar_iaves", pergunta=question))["estado"] == "contexto"
        assert IAvesMemory.objects.get(agent=agent,user=customer,key="Pesagem").value == memorable
        assert not IAvesMemory.objects.filter(agent=agent,user=customer,key="Inventado").exists()
        assert staff.post(config_url, {"provider":"","model":"","token":""}, secure=True).status_code == 302
        assert not IAvesSettings.objects.filter(agent=agent).exists()

        with patch("bridge.iaves.avihub.query") as avihub_query:
            avihub = payload(call("consultar_iaves", pergunta="Consulte meus equipamentos"))
            assert avihub["avihub"]["estado"] == "indisponivel"
            avihub_query.assert_not_called()
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            connector_id = uuid4()
            cursor.execute("INSERT INTO hub_tenancy.agents(id,tenant_id,name,kind) VALUES(%s,%s,'AviHUB QA','avihub')",
                           [connector_id,tenant])
            cursor.execute("INSERT INTO hub_tenancy.agent_spaces(tenant_id,workspace_id,agent_id) VALUES(%s,%s,%s)",
                           [tenant,space,connector_id])
            cursor.execute("SET LOCAL ROLE hub_portal")
        with patch("bridge.iaves.avihub.query", return_value={"estado":"escolher_unidade","unidades":[{"id":"1","nome":"Unidade QA"}]}) as query:
            avihub = payload(call("consultar_iaves", pergunta="Consulte meus equipamentos", avihub="equipamentos", conta="conta-1"))
            assert avihub["estado"] == "pergunta" and avihub["avihub"]["unidades"][0]["nome"] == "Unidade QA"
            assert query.call_args.args[:4] == (tenant,customer,{"conta":"conta-1"},"listar_equipamentos")

        from .smoke_iaves_subscriptions import verify_subscriptions
        verify_subscriptions(buyer, signed_in(other_customer), stranger, customer, tenant, space, agent, connector_id)

        assert buyer.post(memory_url, {"action":"delete","item":item.pk}, secure=True).status_code == 302
        assert buyer.post(memory_url, {"action":"clear","confirm":"on"}, secure=True).status_code == 302
        assert not IAvesMemory.objects.filter(agent=agent,user=customer).exists()
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("DELETE FROM hub_tenancy.agent_spaces WHERE tenant_id=%s AND workspace_id=%s AND agent_id=%s", [tenant,space,agent])
            cursor.execute("SET LOCAL ROLE hub_portal")
        assert buyer.get(agent_url, secure=True).status_code == 404
        assert buyer.get(agent_url, detail_args, secure=True).status_code == 404
        assert buyer.get(memory_url, secure=True).status_code == 404
        assert "error" in call("consultar_iaves", pergunta=question)
        assert "error" in call("consultar_iaves", pergunta=source_question, referencia_fontes=source_ref)
        assert Link.objects.filter(pk=link.pk).exists()
    print("IAves: acesso padrão, base local, fontes sob demanda, histórico, memória privada, AviHUB e revogação verificados.")
