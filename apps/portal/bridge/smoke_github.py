import io
import json
import os
import tarfile
from html.parser import HTMLParser
from tempfile import TemporaryDirectory
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.db import connection
from django.test import Client
from django.urls import reverse

from .models import GitHubConnection


class RegistrationForm(HTMLParser):
    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "input" and fields.get("name") == "manifest":
            self.manifest = json.loads(fields["value"])


def verify_github(manager, outsider, tenant, space):
    from .github_api import GitHubError
    url = reverse("github_connector", args=[tenant])
    callback = reverse("github_registered",args=[tenant])
    assert Client().get(url,secure=True,HTTP_HOST="hubia.psxt.io").status_code==302
    assert manager.get(url,secure=True).status_code==200
    assert outsider.get(url,secure=True).status_code==404
    assert manager.post(url,{"action":"register","github_org":"bad/path"},secure=True).status_code==400
    start = manager.post(url,{"action":"register","github_org":"test-org"},secure=True)
    assert start.status_code==200 and b"https://github.com/organizations/test-org/settings/apps/new" in start.content
    form = RegistrationForm()
    form.feed(start.content.decode())
    manifest = form.manifest
    origin = "https://hubia.psxt.io"
    assert manifest["url"] == origin
    assert manifest["hook_attributes"] == {"url":origin+"/webhooks/github/","active":False}
    assert manifest["redirect_url"] == origin+callback and manifest["setup_url"] == origin+url
    assert manifest["default_events"] == [] and manifest["default_permissions"] == {"contents":"read"}
    flow = manager.session["github_registration"]
    assert manager.get(callback,{"code":"qa","state":"wrong"},secure=True).status_code==400
    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    def api(path, *, token=None, body=None):
        if path=="/app-manifests/qa/conversions":
            return {"owner":{"login":"test-org","type":"Organization"},"id":844,"pem":pem,"slug":"hub-qa"}
        if path=="/orgs/test-org/installation":
            assert jwt.decode(token,key.public_key(),algorithms=["RS256"])["iss"]=="844"
            return {"app_id":844,"account":{"type":"Organization","login":"test-org"},"repository_selection":"selected",
                    "permissions":{"contents":"read"},"id":753}
        if path=="/app/installations/753/access_tokens":
            assert body["permissions"]=={"contents":"read","metadata":"read"}
            return {"token":"installation-test"}
        assert token=="installation-test"
        if path=="/installation/repositories?per_page=100&page=1":
            return {"total_count":1,"repositories":[{"id":901,"full_name":"test-org/Obsidian","default_branch":"main"}]}
        assert path=="/repos/test-org/Obsidian/branches/main"
        return {"commit":{"sha":"a"*40}}
    with patch("bridge.github_api.api",side_effect=api):
        assert manager.get(callback,{"code":"qa","state":flow["state"]},secure=True).status_code==302
        entry = GitHubConnection.objects.get(tenant=tenant)
        assert bytes(entry.private_key)!=pem.encode()
        assert outsider.post(url,{"action":"test"},secure=True).status_code==404
        assert manager.post(url,{"action":"test"},secure=True).status_code==302
        result = manager.get(url,secure=True).content.decode()
        assert all(t in result for t in ("Conectado","test-org/Obsidian","main","a"*12))
        assert pem not in result and "installation-test" not in result
    verify_library(manager, outsider, tenant, space, url)
    with patch("bridge.github_api.api",side_effect=GitHubError("Instalação indisponível.")):
        assert manager.post(url,{"action":"test"},secure=True).status_code==302
        result = manager.get(url,secure=True).content.decode()
        assert "Instalação indisponível." in result and "a"*12 in result
    with patch("bridge.github_api.api",side_effect=GitHubError("Instalação indisponível.", revoked=True)):
        assert manager.post(url,{"action":"test"},secure=True).status_code==302
        result = manager.get(url,secure=True).content.decode()
        assert "Instalação indisponível." in result and "a"*12 not in result
    print("GitHub: registro privado, JWT, instalação, leitura da revisão, falha e isolamento institucional verificados com API simulada.")


def verify_library(manager, viewer, tenant, space, config_url):
    from types import SimpleNamespace
    from uuid import uuid4
    from . import github_api, library, library_sync, mcp
    from .models import NoteLibrary

    config = {"action":"create_library","repo":"901","name":"Biblioteca QA"}
    assert viewer.post(config_url, config, secure=True).status_code == 404
    assert manager.post(config_url, config, secure=True).status_code == 302
    agent = NoteLibrary.objects.get(connection_id=tenant)
    url = reverse("use_agent", args=[tenant, space, agent.id])
    link = SimpleNamespace(tenant=tenant, space=space)
    from smoke_agents import select_institution_agent
    assert viewer.get(url, secure=True).status_code == 404
    assert select_institution_agent(manager, tenant, space, agent.pk, True, {"secure":True}).status_code == 302
    assert "Nenhuma pasta" in viewer.get(url, secure=True).content.decode()
    def tools():
        return [t["name"] for t in mcp.available_tools(link, {}, False)]
    assert "ler_nota" not in tools()
    form = {"action":"grant_folders","space":space,"folders":"Equipe"}
    assert manager.post(config_url, {**form, "space":str(uuid4())}, secure=True).status_code == 404
    assert manager.post(config_url, {**form, "folders":"Equipe/../Privado"}, secure=True).status_code == 400
    assert manager.post(config_url, form, secure=True).status_code == 302
    assert "buscar_notas" in tools()
    body = "Procedimento de atualização"
    files = {name:body.encode() for name in ("Equipe/Manual.md", "Equipe2/Segredo.md", "Equipe/.pessoal/Teste.md", "Inicio.md")}
    files.update({f"Equipe/Nota {n}.md": b"page query" for n in range(7)})
    files["Equipe/Longa.md"] = ("á" * (library.CHUNK + 1)).encode()

    def download(name, revision, token, destination):
        assert name == "test-org/Obsidian" and token == "library-test"
        if revision == "c" * 40:
            destination.write(b"arquivo parcial")
            return
        with tarfile.open(fileobj=destination, mode="w:gz") as archive:
            root = tarfile.TarInfo("obsidian/")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            link_entry = tarfile.TarInfo("obsidian/Equipe/Atalho.md")
            link_entry.type = tarfile.SYMTYPE
            link_entry.linkname = "../Equipe2/Segredo.md"
            archive.addfile(link_entry)
            for name, data in files.items():
                if revision == "b" * 40 and name == "Equipe/Manual.md":
                    continue
                entry = tarfile.TarInfo("obsidian/" + name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))

    def sync(revision):
        def api(path, *, token):
            assert token == "library-test"
            if path == "/repos/test-org/Obsidian":
                return {"id":901, "default_branch":"main"}
            assert path == "/repos/test-org/Obsidian/branches/main"
            return {"commit":{"sha":revision}}
        with patch("bridge.github_api.installation_token", return_value=(1,"library-test")) as mint, patch(
                "bridge.github_api.api",side_effect=api), patch("bridge.library_sync.download_archive",side_effect=download) as fetch:
            result = library_sync.sync_library(agent)
            mint.assert_called_once_with(agent.connection, 901)
            assert fetch.call_count == (result == "updated")
            return result

    def unavailable(action, args, phrase):
        try:
            call(action, args)
        except ValueError as exc:
            assert phrase in str(exc)
        else:
            raise AssertionError("Consulta deveria estar indisponível.")

    with TemporaryDirectory() as directory, patch.dict(os.environ, {"HUBIA_KNOWLEDGE_DIR":directory}):
        def call(action, args):
            return mcp.call_tool(link, {}, False, {"name":action,"arguments":args})
        unavailable("buscar_notas", {"consulta":"atualizacao"}, "cópia local")
        assert "Aguardando uma cópia local" in manager.get(config_url,secure=True).content.decode()
        assert sync("a" * 40) == "updated" and sync("a" * 40) == "unchanged"
        assert "Revisão da cópia local" in manager.get(config_url,secure=True).content.decode()
        with patch("bridge.github_api.api",side_effect=AssertionError("Consulta ao GitHub fora da sincronização")), patch(
                "bridge.github_api.installation_token",side_effect=AssertionError("Emissão de token fora da sincronização")):
            results = call("buscar_notas", {"consulta":"atualizacao"})
            assert [n["caminho"] for n in results["notas"]] == ["Equipe/Manual.md"]
            note = call("ler_nota", {"caminho":"Equipe/Manual.md"})
            assert note["texto"] == body and note["revisao"] in note["fonte"]
            long = call("ler_nota", {"caminho":"Equipe/Longa.md"})
            last = call("ler_nota", {"caminho":long["caminho"], "inicio":str(long["proximo_inicio"]), "revisao":long["revisao"]})
            assert len(long["texto"]) == library.CHUNK and last["texto"] == "á" and last["proximo_inicio"] is None
            page = call("buscar_notas", {"consulta":"PAGE query"})
            second = call("buscar_notas", {"consulta":"PAGE query", "pagina":str(page["proxima_pagina"]), "revisao":page["revisao"]})
            assert len(page["notas"]) == 6 and len(second["notas"]) == 1 and second["proxima_pagina"] is None
            for t,s,p in [(tenant,space,"Equipe2/Segredo.md"),(tenant,space,"Equipe/Atalho.md"),
                          (tenant,space,"Equipe/.pessoal/Teste.md"),(tenant,space,"Equipe/../Privado/A.md"),
                          (uuid4(),space,"Equipe/Manual.md"),(tenant,uuid4(),"Equipe/Manual.md")]:
                try:
                    library.query(t,s,"ler_nota",{"caminho":p})
                except ValueError:
                    pass
                else:
                    raise AssertionError("Leitura fora da concessão")
            assert body in viewer.post(url,{"action":"ler_nota","caminho":"Equipe/Manual.md"},secure=True).content.decode()
            assert manager.post(config_url,{**form,"folders":"/"},secure=True).status_code == 302
            all_notes = call("buscar_notas", {"consulta":"atualização"})
            assert {n["caminho"] for n in all_notes["notas"]} == {"Equipe/Manual.md","Equipe2/Segredo.md","Inicio.md"}
            assert call("ler_nota", {"caminho":"Inicio.md"})["texto"] == body
        assert sync("b" * 40) == "updated"
        unavailable("buscar_notas", {"consulta":"atualizacao", "revisao":"a" * 40}, "notas foram atualizadas")
        unavailable("ler_nota", {"caminho":"Equipe/Manual.md"}, "Nota indisponível")
        try:
            sync("c" * 40)
        except library_sync.SnapshotError:
            pass
        else:
            raise AssertionError("Uma cópia parcial foi aceita.")
        assert call("ler_nota", {"caminho":"Inicio.md"})["revisao"] == "b" * 40
        with patch("bridge.github_api.installation_token",side_effect=github_api.GitHubError("Revogada", revoked=True)):
            try:
                library_sync.sync_library(agent)
            except github_api.GitHubError as exc:
                assert exc.revoked
            else:
                raise AssertionError("A revogação do GitHub não foi sinalizada.")
        unavailable("ler_nota", {"caminho":"Inicio.md"}, "cópia local")
        assert manager.post(config_url,{**form,"folders":""},secure=True).status_code == 302
        assert "ler_nota" not in tools()
    print("Biblioteca local: sincronização, leitura sem GitHub, paginação, atualização, falha parcial e revogação verificadas.")
