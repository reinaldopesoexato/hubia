"""Bibliotecas institucionais de notas Markdown em repositórios GitHub."""
import base64
import binascii
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from . import github_api
from .agent_catalog import assigned
from .models import LibraryAccess

PURPOSE = "Busca e lê notas Markdown nas pastas liberadas para este espaço."
MAX_BYTES = 512 * 1024
PAGE = 6
CHUNK = 16000


def allowed_path(path):
    return (isinstance(path, str) and 0 < len(path) <= 1024
            and "\\" not in path and all(ord(c) >= 32 for c in path)
            and all(part and not part.startswith(".") for part in path.split("/")))


def parse_folders(value):
    folders = list(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))
    if folders == ["/"]:
        return folders
    if len(folders) > 60 or any(not allowed_path(folder) for folder in folders):
        raise ValueError("Informe até 60 pastas relativas, uma por linha, sem barras nas extremidades ou componentes ocultos.")
    return folders


def folded(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


def number(value, minimum):
    if not re.fullmatch(r"[0-9]{1,8}", str(value)) or int(value) < minimum:
        raise ValueError("Página ou posição inválida.")
    return int(value)


def note_text(item, base, token):
    if item["size"] > MAX_BYTES:
        raise ValueError("A nota excede o limite de 512 KiB.")
    blob = github_api.api(base + "/git/blobs/" + item["sha"], token=token)
    if blob.get("encoding") != "base64" or blob.get("size") != item["size"]:
        raise ValueError("O GitHub retornou um formato de nota inesperado.")
    try:
        data = base64.b64decode("".join(blob["content"].split()), validate=True)
        if len(data) != item["size"]:
            raise ValueError("Tamanho de nota inválido.")
        return data.decode("utf-8-sig")
    except (UnicodeError, binascii.Error):
        raise ValueError("A nota precisa estar em UTF-8.") from None


def query(tenant, space, action, args):
    # O chamador já verificou a associação do usuário ao espaço. A concessão vincula
    # o repositório, a instituição e o espaço novamente em cada consulta.
    if not assigned(tenant, space, "library"):
        raise ValueError("Biblioteca indisponível neste espaço.")
    access = LibraryAccess.objects.select_related("library__connection").filter(
        library__connection_id=tenant, space=space).first()
    if not access or not access.folders:
        raise ValueError("Nenhuma pasta da biblioteca foi liberada para este espaço.")
    def allowed(path):
        return (allowed_path(path) and path.casefold().endswith(".md")
                and (access.folders == ["/"] or any(path.startswith(folder + "/") for folder in access.folders)))
    path = args.get("caminho", "")
    terms = folded(args.get("consulta", "").strip()).split()
    if action == "ler_nota" and not allowed(path):
        raise ValueError("Nota indisponível neste espaço.")
    if action == "buscar_notas" and (not terms or len(args.get("consulta", "")) > 200):
        raise ValueError("Informe uma busca de 1 a 200 caracteres.")
    conn = access.library.connection
    repo = next((r for r in conn.repositories if r["id"] == access.library.repository_id), None)
    if not repo:
        raise ValueError("O repositório da biblioteca precisa ser verificado no conector.")
    try:
        _, token = github_api.installation_token(conn, repo["id"])
        base = "/repos/" + quote(repo["name"], safe="/")
        current = github_api.api(base, token=token)
        if current.get("id") != repo["id"]:
            raise ValueError("O repositório mudou. Verifique novamente o conector GitHub.")
        branch = github_api.api(base + "/branches/" + quote(current["default_branch"], safe=""), token=token)["commit"]
        revision = branch["sha"]
        if args.get("revisao") and args["revisao"] != revision:
            raise ValueError("As notas foram atualizadas. Repita a busca na primeira página.")
        tree = github_api.api(base + "/git/trees/" + branch["commit"]["tree"]["sha"] + "?recursive=1", token=token)
        if tree.get("truncated"):
            raise ValueError("A árvore do repositório excede o limite do GitHub para consulta completa.")
        notes = {item["path"]:item for item in tree["tree"] if item["type"] == "blob"
                 and item["mode"] in ("100644", "100755") and allowed(item["path"])}
        source = "https://github.com/" + quote(repo["name"], safe="/") + "/blob/" + revision + "/"
        if action == "ler_nota":
            if path not in notes:
                raise ValueError("Nota indisponível neste espaço.")
            content = note_text(notes[path], base, token)
            offset = number(args.get("inicio", "0"), 0)
            if offset > len(content):
                raise ValueError("Posição fora da nota.")
            end = offset + CHUNK
            return {"caminho":path, "fonte":source + quote(path), "revisao":revision,
                    "texto":content[offset:end], "proximo_inicio":end if end < len(content) else None}
        page = number(args.get("pagina", "1"), 1)
        def matches(value):
            value = folded(value)
            return all(term in value for term in terms)
        paths = sorted(notes, key=lambda p:(not matches(p), p.casefold(), p))
        start = (page - 1) * PAGE
        def match(path):
            if matches(path):
                return {"caminho":path, "fonte":source + quote(path)}
            if notes[path]["size"] <= MAX_BYTES:
                try:
                    body = note_text(notes[path], base, token)
                except ValueError:
                    return None
                if matches(body):
                    return {"caminho":path, "fonte":source + quote(path)}
            return None
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = [item for item in pool.map(match, paths[start:start+PAGE]) if item]
        return {"notas":results, "revisao":revision, "proxima_pagina":page+1 if start+PAGE < len(paths) else None}
    except github_api.GitHubError as exc:
        raise ValueError(str(exc)) from None
