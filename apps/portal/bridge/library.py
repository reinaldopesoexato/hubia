"""Consulta da cópia local das notas institucionais."""
import os
import re
import sqlite3
import unicodedata
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .agent_catalog import assigned
from .models import LibraryAccess, NoteLibrary, PESO_EXATO_TENANT_ID

PURPOSE = "Busca e lê notas Markdown nas pastas liberadas para este espaço."
MAX_BYTES = 512 * 1024
PAGE = 6
CHUNK = 16000
SYNC_PENDING = "A cópia local da biblioteca ainda está indisponível. Aguarde a próxima sincronização."
IAVES_FOLDER = "Business/IAves/"
IAVES_REPOSITORY_ID = 1306809360
IAVES_STOPWORDS = frozenset("""
    agora ainda ajuda algo algum aqui aves avicultura bem boa bom com como consigo dessa desse devo dia dos das
    ela ele essa esse esta estao estou fazer fale geral gostaria hoje isso local mas melhor meu minha minhas meus
    muito nao nos nossa nossas nosso nossos onde para pelo pela pode podem poderia por qual quais quando quanto
    oi ola quero saber seja ser posso sobre sua suas seu seus tambem tarde tenho ter tipo todas todos uma umas voce voces
    you your the and how what can from this with about are por que para como los las del una mis sus
""".split())


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


def snapshot_path(library):
    directory = Path(os.environ.get("HUBIA_KNOWLEDGE_DIR", "/var/lib/hubia/knowledge"))
    return directory / f"{library.connection_id}-{library.pk}-{library.repository_id}.sqlite3"


def configured_repository(library):
    return next((repo for repo in library.connection.repositories if repo["id"] == library.repository_id), None)


@contextmanager
def open_snapshot(library):
    uri = snapshot_path(library).absolute().as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as db:
            yield db
    except sqlite3.Error:
        raise ValueError(SYNC_PENDING) from None


def snapshot_metadata(db):
    row = db.execute("SELECT name, revision, synchronized_at FROM metadata").fetchone()
    if not row:
        raise ValueError(SYNC_PENDING)
    return row


def snapshot_status(library):
    if not configured_repository(library):
        return None
    try:
        with open_snapshot(library) as db:
            name, revision, stamp = snapshot_metadata(db)
        synchronized_at = datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return {"name":name, "revision":revision, "synchronized_at":synchronized_at}


def iaves_terms(question):
    words = re.findall(r"[a-z0-9]{2,}", folded(question))
    return list(dict.fromkeys(word for word in words if word not in IAVES_STOPWORDS))[:16]


def iaves_notes(tenant, question):
    """Acesso exclusivo do IAves, após validar a atribuição do agente no espaço."""
    if tenant != PESO_EXATO_TENANT_ID:
        raise ValueError("Base do IAves indisponível.")
    library = NoteLibrary.objects.select_related("connection").filter(
        connection_id=tenant, repository_id=IAVES_REPOSITORY_ID,
    ).first()
    if not library or not configured_repository(library):
        raise ValueError(SYNC_PENDING)
    terms = iaves_terms(question)
    if not terms:
        return []

    matches = " + ".join("3 * (instr(folded_path, ?) > 0) + (instr(folded_body, ?) > 0)" for _ in terms)
    params = [value for term in terms for value in (term, term)]
    with open_snapshot(library) as db:
        _, revision, _ = snapshot_metadata(db)
        rows = db.execute(f"""
            WITH relevant AS (
                SELECT path, body, ({matches}) AS score FROM notes
                WHERE substr(path, 1, ?) = ? AND body IS NOT NULL AND length(body) > 0
            )
            SELECT path, body FROM relevant WHERE score > 0 ORDER BY score DESC, path LIMIT 5
            """, [*params, len(IAVES_FOLDER), IAVES_FOLDER]).fetchall()
    results = []
    for path, body in rows:
        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", body) if paragraph.strip()]
        if not paragraphs:
            continue
        def relevance(paragraph):
            normalized = folded(paragraph)
            return sum(term in normalized for term in terms)
        selected = sorted(paragraphs, key=relevance, reverse=True)[:3]
        excerpt = "\n\n".join(paragraph[:850] for paragraph in selected)[:2200]
        results.append({"caminho":path, "revisao":revision, "texto":excerpt})
    return results


def read_note(db, args, source, revision):
    path = args["caminho"]
    row = db.execute("SELECT size, body FROM notes WHERE path = ?", [path]).fetchone()
    if not row:
        raise ValueError("Nota indisponível neste espaço.")
    size, text = row
    if size > MAX_BYTES:
        raise ValueError("A nota excede o limite de 512 KiB.")
    if text is None:
        raise ValueError("A nota precisa estar em UTF-8.")
    offset = number(args.get("inicio", "0"), 0)
    if offset > len(text):
        raise ValueError("Posição fora da nota.")
    end = offset + CHUNK
    return {"caminho":path, "fonte":source + quote(path), "revisao":revision,
            "texto":text[offset:end], "proximo_inicio":end if end < len(text) else None}


def search_notes(db, args, terms, folders, source, revision):
    page = number(args.get("pagina", "1"), 1)
    prefixes = [] if folders == ["/"] else [folder + "/" for folder in folders]
    scope = " OR ".join("substr(path, 1, length(?)) = ?" for _ in prefixes) or "1"
    params = [part for prefix in prefixes for part in (prefix, prefix)]
    title_match = " AND ".join("instr(folded_path, ?) > 0" for _ in terms)
    body_match = " AND ".join("instr(folded_body, ?) > 0" for _ in terms)
    db.create_function("casefold", 1, str.casefold, deterministic=True)
    rows = db.execute(f"""
        WITH scoped AS (
            SELECT path, ({title_match}) AS title_match, ({body_match}) AS body_match
            FROM notes WHERE ({scope})
        )
        SELECT path FROM scoped WHERE title_match OR body_match
        ORDER BY title_match DESC, casefold(path), path LIMIT ? OFFSET ?
        """, [*terms, *terms, *params, PAGE + 1, (page - 1) * PAGE]).fetchall()
    results = [{"caminho":path, "fonte":source + quote(path)} for (path,) in rows[:PAGE]]
    return {"notas":results, "revisao":revision, "proxima_pagina":page + 1 if len(rows) > PAGE else None}


def query(tenant, space, action, args):
    # O chamador já verificou a associação do usuário ao espaço. A concessão vincula
    # o repositório, a instituição e o espaço novamente em cada consulta.
    if not assigned(tenant, space, "library"):
        raise ValueError("Biblioteca indisponível neste espaço.")
    access = LibraryAccess.objects.select_related("library__connection").filter(
        library__connection_id=tenant, space=space).first()
    if not access or not access.folders:
        raise ValueError("Nenhuma pasta da biblioteca foi liberada para este espaço.")
    folders = access.folders
    if action == "ler_nota":
        path = args.get("caminho", "")
        if (not allowed_path(path) or not path.casefold().endswith(".md")
                or (folders != ["/"] and not any(path.startswith(folder + "/") for folder in folders))):
            raise ValueError("Nota indisponível neste espaço.")
    elif action == "buscar_notas":
        query_text = args.get("consulta", "")
        terms = folded(query_text.strip()).split()
        if not terms or len(query_text) > 200:
            raise ValueError("Informe uma busca de 1 a 200 caracteres.")
    else:
        raise ValueError("Ação indisponível.")
    if not configured_repository(access.library):
        raise ValueError("O repositório da biblioteca precisa ser verificado no conector.")
    with open_snapshot(access.library) as db:
        repo_name, revision, _ = snapshot_metadata(db)
        if args.get("revisao") and args["revisao"] != revision:
            raise ValueError("As notas foram atualizadas. Repita a busca na primeira página.")
        source = "https://github.com/" + quote(repo_name, safe="/") + "/blob/" + revision + "/"
        if action == "ler_nota":
            return read_note(db, args, source, revision)
        return search_notes(db, args, terms, folders, source, revision)
