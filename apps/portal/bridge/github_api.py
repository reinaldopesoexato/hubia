"""Acesso de leitura via instalação de GitHub App no GitHub.com."""
import json
import re
import time
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener

import jwt
from cryptography.fernet import InvalidToken

from agent_runtime import NoRedirect
from model_provider import cipher


class GitHubError(Exception):
    def __init__(self, message, *, revoked=False):
        super().__init__(message)
        self.revoked = revoked


HEADERS = {"Accept":"application/vnd.github+json", "User-Agent":"HUB-IA",
           "X-GitHub-Api-Version":"2022-11-28"}


def response_error(exc):
    reasons = {401:"A credencial da GitHub App foi recusada.",
               403:"Acesso negado, instalação suspensa ou limite da API atingido.",
               404:"Instalação, repositório ou branch indisponível. Confira a instalação e os repositórios selecionados.",
               409:"O repositório ainda não possui uma revisão disponível.",
               422:"O GitHub recusou os dados. Confira as permissões ou reinicie o cadastro."}
    revoked = exc.code in (401, 404, 410)
    if exc.code in (403, 422):
        # Primary/secondary rate limiting can share the same status as a denied grant.
        # Only discard an already authorized copy for an explicit access rejection.
        try:
            data = json.loads(exc.read(8192))
        except (OSError, HTTPException, AttributeError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        details = data.get("errors", [])
        errors = [item for item in details if isinstance(item, dict)] if isinstance(details, list) else []
        message = " ".join(str(item.get("message", "")) for item in (data, *errors)).casefold()
        repository_error = any(item.get("field") in ("repositories", "repository_ids") for item in errors)
        headers = exc.headers or {}
        rate_limited = bool(headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0"
                            or "rate limit" in message)
        denied = any(phrase in message for phrase in (
            "resource not accessible", "installation is suspended", "repository selection", "could not access",
            "repository is not accessible", "not accessible by the installation", "not part of"))
        revoked = not rate_limited and (denied or repository_error)
    return GitHubError(reasons.get(exc.code, f"O GitHub retornou HTTP {exc.code}."), revoked=revoked)


def api(path, *, token=None, body=None):
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = "Bearer " + token
    raw = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode()
    req = Request("https://api.github.com"+path, data=raw, headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=5) as response:
            return json.load(response)
    except HTTPError as exc:
        with exc:
            raise response_error(exc) from None
    except (OSError, HTTPException, ValueError):
        raise GitHubError("Não foi possível obter uma resposta válida do GitHub. Tente novamente.") from None


def credentials(code, organization):
    result = api("/app-manifests/"+quote(code,safe="")+"/conversions",body={})
    owner = result["owner"]
    if owner["type"]!="Organization" or owner["login"].casefold()!=organization.casefold():
        raise GitHubError("A GitHub App precisa pertencer à organização informada.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+",result["slug"]):
        raise GitHubError("Identificador da GitHub App inválido.")
    return {"app_id":int(result["id"]), "slug":result["slug"],
            "private_key":cipher().encrypt(result["pem"].encode())}


def installation_token(conn, repository_id=None):
    now = int(time.time())
    try:
        key = cipher().decrypt(bytes(conn.private_key))
        signed = jwt.encode({"iat":now-60,"exp":now+480,"iss":str(conn.app_id)},key,algorithm="RS256")
    except (InvalidToken, jwt.PyJWTError, ValueError, TypeError):
        raise GitHubError("A chave da GitHub App está indisponível.", revoked=True) from None
    install = api("/orgs/"+quote(conn.organization,safe="")+"/installation",token=signed)
    if (install["app_id"]!=conn.app_id or install["account"]["type"]!="Organization"
            or install["account"]["login"].casefold()!=conn.organization.casefold()):
        raise GitHubError("A instalação não corresponde a esta instituição.", revoked=True)
    if install.get("suspended_at"):
        raise GitHubError("A instalação foi suspensa no GitHub.", revoked=True)
    if install.get("repository_selection")!="selected":
        raise GitHubError("Selecione Only select repositories na instalação do GitHub.", revoked=True)
    if install.get("permissions",{}).get("contents")!="read":
        raise GitHubError("A instalação precisa da permissão Contents: Read-only.", revoked=True)
    installation_id = int(install["id"])
    scope = {"permissions":{"contents":"read","metadata":"read"}}
    if repository_id is not None:
        scope["repository_ids"] = [repository_id]
    result = api(f"/app/installations/{installation_id}/access_tokens", token=signed, body=scope)
    return installation_id,result["token"]


def inspect_repositories(conn, repository_id=""):
    installation, token = installation_token(conn)
    repos = []
    page = 1
    while True:
        batch = api(f"/installation/repositories?per_page=100&page={page}",token=token)
        for item in batch["repositories"]:
            repos.append({"id":item["id"],"name":item["full_name"],"branch":item["default_branch"]})
        if len(batch["repositories"])<100 or len(repos)>=batch["total_count"]:
            break
        page += 1
    repos.sort(key=lambda repo:repo["name"].casefold())
    if not repos:
        raise GitHubError("Nenhum repositório disponível nesta instalação.", revoked=True)
    if repository_id:
        target = next((r for r in repos if str(r["id"])==repository_id),None)
        if not target:
            raise GitHubError("O repositório selecionado deixou de estar disponível.", revoked=True)
    else:
        target = next((r for r in repos if r["name"].casefold()==conn.organization.casefold()+"/obsidian"),repos[0])
    owner, name = target["name"].split("/",1)
    path = "/repos/"+quote(owner,safe="")+"/"+quote(name,safe="")+"/branches/"+quote(target["branch"],safe="")
    ref = api(path,token=token)
    sha = ref["commit"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40,64}",sha):
        raise GitHubError("Revisão inválida na resposta do GitHub.")
    target["sha"] = sha
    return installation,repos
