"""Build the private local Markdown snapshot independently of portal requests."""
import argparse
import fcntl
import gzip
import logging
import os
import re
import sqlite3
import tarfile
import tempfile
import time
from contextlib import closing
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from agent_runtime import NoRedirect


MAX_DOWNLOAD = 512 * 1024**2
MAX_EXPANDED = 1536 * 1024**2
MAX_INDEX = 512 * 1024**2
MAX_MEMBERS = 50_000
CHUNK = 64 * 1024
TIMEOUT = 10
SYNC_LIMIT = 180
DEFAULT_INTERVAL = 300
logger = logging.getLogger(__name__)


class SnapshotError(Exception):
    pass


def check_deadline(deadline):
    if time.monotonic() > deadline:
        raise SnapshotError("A sincronização ultrapassou o tempo disponível.")


def archive_location(opener, name, revision, token):
    from . import github_api

    headers = {**github_api.HEADERS, "Authorization": "Bearer " + token}
    path = "/repos/" + quote(name, safe="/") + "/tarball/" + revision
    try:
        with opener.open(Request("https://api.github.com" + path, headers=headers), timeout=TIMEOUT):
            raise SnapshotError("O GitHub não forneceu o endereço do arquivo da revisão.")
    except HTTPError as exc:
        with exc:
            if exc.code != 302:
                raise github_api.response_error(exc) from None
            location = exc.headers.get("Location", "")

    try:
        url = urlsplit(location)
        paths = {f"/{name}/{style}/{revision}".casefold() for style in ("legacy.tar.gz", "tar.gz")}
        if (url.scheme == "https" and url.netloc.casefold() in ("codeload.github.com", "codeload.github.com:443")
                and url.path.casefold() in paths and not url.fragment):
            return location
    except ValueError:
        pass
    raise SnapshotError("O endereço retornado para o arquivo da revisão é inválido.")


def download_archive(name, revision, token, archive):
    opener = build_opener(NoRedirect())
    deadline = time.monotonic() + SYNC_LIMIT
    try:
        location = archive_location(opener, name, revision, token)
        # The codeload URL is already signed, so forward only its URL and a user agent.
        request = Request(location, headers={"User-Agent": "HUB-IA"})
        with opener.open(request, timeout=TIMEOUT) as response:
            total = 0
            while chunk := response.read1(CHUNK):
                check_deadline(deadline)
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise SnapshotError("O arquivo do repositório excede o limite de download.")
                archive.write(chunk)
    except (OSError, HTTPException, ValueError):
        # Response details can contain the signed codeload URL.
        raise SnapshotError("Não foi possível baixar o arquivo do repositório.") from None


class LimitedArchive:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_EXPANDED
        self.deadline = time.monotonic() + SYNC_LIMIT

    def read(self, size):
        check_deadline(self.deadline)
        chunk = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise SnapshotError("O conteúdo descompactado excede o limite da biblioteca.")
        return chunk


def archive_notes(archive):
    from .library import MAX_BYTES, allowed_path, folded

    with gzip.GzipFile(fileobj=archive) as stream:
        source = LimitedArchive(stream)
        with tarfile.open(fileobj=source, mode="r|") as tar:
            root = tar.next()
            if root is None or not root.isdir() or "/" in root.name or not allowed_path(root.name):
                raise SnapshotError("O arquivo do repositório tem uma estrutura inválida.")
            prefix = root.name + "/"
            indexed = 0
            for count, member in enumerate(tar, start=1):
                if count > MAX_MEMBERS:
                    raise SnapshotError("O repositório excede o limite de arquivos da biblioteca.")
                path = member.name.removeprefix(prefix)
                if (path == member.name or not allowed_path(path) or not path.casefold().endswith(".md")
                        or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.issparse()
                        or member.size < 0):
                    continue
                try:
                    path.encode("utf-8")
                except UnicodeError:
                    continue
                body = normalized = None
                if member.size <= MAX_BYTES:
                    with tar.extractfile(member) as note:
                        try:
                            body = note.read().decode("utf-8-sig")
                        except UnicodeError:
                            pass
                    if body is not None:
                        normalized = folded(body)
                        indexed += member.size + len(normalized.encode("utf-8"))
                        if indexed > MAX_INDEX:
                            raise SnapshotError("O volume de notas excede o limite da biblioteca.")
                yield (path, member.size, body, folded(path), normalized)
        # Read the gzip trailer to detect a truncated download before publishing the database.
        while source.read(CHUNK):
            pass


def write_snapshot(archive, destination, name, revision):
    try:
        with closing(sqlite3.connect(destination)) as db:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("""CREATE TABLE metadata (name TEXT NOT NULL, revision TEXT NOT NULL,
                       synchronized_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE notes (path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                       body TEXT, folded_path TEXT NOT NULL, folded_body TEXT)""")
            db.executemany("INSERT INTO notes VALUES (?, ?, ?, ?, ?)", archive_notes(archive))
            db.execute("INSERT INTO metadata VALUES (?, ?, ?)", (name, revision, time.time()))
            db.commit()
    except (tarfile.TarError, EOFError, OSError, sqlite3.Error):
        raise SnapshotError("Não foi possível criar uma cópia local íntegra da biblioteca.") from None


def fetch_revision(library, repo):
    from . import github_api

    name = repo["name"]
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", name):
        raise github_api.GitHubError("Nome de repositório inválido no conector.", revoked=True)
    _, token = github_api.installation_token(library.connection, library.repository_id)
    base = "/repos/" + quote(name, safe="/")
    current = github_api.api(base, token=token)
    if current.get("id") != library.repository_id or current.get("full_name", name).casefold() != name.casefold():
        raise github_api.GitHubError("O repositório mudou. Verifique novamente o conector GitHub.", revoked=True)
    branch = github_api.api(base + "/branches/" + quote(current["default_branch"], safe=""), token=token)
    revision = branch["commit"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise SnapshotError("O GitHub retornou uma revisão inválida.")
    return name, revision, token


def sync_library(library):
    from . import github_api
    from .library import configured_repository, snapshot_path, snapshot_status

    destination = snapshot_path(library)
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with destination.with_suffix(".lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        try:
            repo = configured_repository(library)
            if not repo:
                raise github_api.GitHubError("Confira o repositório selecionado no conector GitHub.", revoked=True)
            name, revision, token = fetch_revision(library, repo)
            previous = snapshot_status(library)
            if previous and (previous["name"], previous["revision"]) == (name, revision):
                return "unchanged"
            with tempfile.TemporaryFile(dir=destination.parent) as archive:
                download_archive(name, revision, token, archive)
                archive.seek(0)
                descriptor, filename = tempfile.mkstemp(prefix=".knowledge-", dir=destination.parent)
                os.close(descriptor)
                temporary = Path(filename)
                try:
                    write_snapshot(archive, temporary, name, revision)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            return "updated"
        except github_api.GitHubError as exc:
            if exc.revoked:
                destination.unlink(missing_ok=True)
            raise


def synchronize():
    from django.db import DatabaseError, close_old_connections
    from .github_api import GitHubError
    from .models import NoteLibrary

    close_old_connections()
    success = True
    try:
        for library in NoteLibrary.objects.select_related("connection").iterator():
            try:
                if sync_library(library) == "updated":
                    logger.info("Biblioteca %s atualizada.", library.pk)
            except (GitHubError, SnapshotError) as exc:
                success = False
                logger.warning("Biblioteca %s: %s", library.pk, exc)
            except (OSError, KeyError, TypeError, ValueError):
                success = False
                logger.warning("Não foi possível sincronizar a biblioteca %s.", library.pk)
    except DatabaseError:
        success = False
        logger.warning("Não foi possível carregar as bibliotecas para sincronização.")
    finally:
        close_old_connections()
    return success


def main():
    parser = argparse.ArgumentParser(description="Sincroniza as cópias locais das bibliotecas de notas.")
    parser.add_argument("--once", action="store_true", help="Roda uma vez e sai.")
    args = parser.parse_args()
    try:
        interval = int(os.environ.get("HUBIA_KNOWLEDGE_SYNC_INTERVAL", DEFAULT_INTERVAL))
        if interval < 1:
            raise ValueError
    except ValueError:
        parser.error("HUBIA_KNOWLEDGE_SYNC_INTERVAL deve ser um número inteiro positivo de segundos.")

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    django.setup()
    logging.basicConfig(level=logging.INFO)
    while True:
        success = synchronize()
        if args.once:
            return 0 if success else 1
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
