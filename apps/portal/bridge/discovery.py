"""MCP catalog discovery, scoped to the bearer that is making the request."""
import asyncio
import hashlib
import json
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.db import close_old_connections, connection, transaction
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.utils import timezone

from .models import CatalogListing, CatalogObservation, Link
from .oauth import authenticate_mcp, unauthorized


CHANGE = {"jsonrpc":"2.0", "method":"notifications/tools/list_changed"}
POLL_SECONDS = 15


def digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def snapshot(auth):
    from .mcp import agent_entries, available_tools

    link, ctx, can_reply = auth
    tools = []
    for spec in available_tools(link, ctx, can_reply):
        if spec["name"] == "list_agents":
            agents = sorted(agent_entries(link, ctx), key=lambda row: (row["space_id"], row["id"]))
            spec = {**spec, "_meta":{"io.psxt.hubia/agents":agents},
                    "description":spec["description"] + f" Catálogo com {len(agents)} entradas. "
                    f"Consulte list_agents quando a revisão mudar ({digest(agents)[:16]})."}
        tools.append(spec)
    return tools, digest(tools)


def observation(auth):
    link, ctx, _ = auth
    row, _ = CatalogObservation.objects.select_for_update().get_or_create(
        token_id=ctx["token_id"], defaults={"link":link})
    return row


def list_tools(auth):
    link = auth[0]
    # Keep the write order aligned with revocation of links and their OAuth tokens.
    if not Link.objects.select_for_update().filter(pk=link.pk).exists():
        raise Http404
    row = observation(auth)
    tools, revision = snapshot(auth)
    row.listed_at = timezone.now()
    row.revision = revision
    row.announced = ""
    row.announced_at = None
    row.save(update_fields=["listed_at", "revision", "announced", "announced_at"])
    CatalogListing.objects.update_or_create(link=link, defaults={
        "listed_at":row.listed_at, "tools":[spec["name"] for spec in tools],
    })
    return {"tools":tools, "_meta":{"io.psxt.hubia/revision":revision}}


def claim_change(auth):
    row = observation(auth)
    _, revision = snapshot(auth)
    if row.revision == revision:
        return False
    now = timezone.now()
    if row.announced == revision and row.announced_at and now - row.announced_at < timedelta(minutes=2):
        return False
    row.announced, row.announced_at = revision, now
    row.save(update_fields=["announced", "announced_at"])
    return True


def accepts_stream(request):
    # A generic */* header is not an explicit request for SSE.
    return any((part.main_type, part.sub_type) == ("text", "event-stream") and part.quality > 0
               for part in request.accepted_types)


def event(message):
    return "event: message\ndata: " + json.dumps(message, separators=(",", ":")) + "\n\n"


def post_stream(ident, result):
    # The RPC result still reaches the caller when a refresh is signalled on its POST.
    return HttpResponse(event(CHANGE) + event({"jsonrpc":"2.0", "id":ident, "result":result}),
                        content_type="text/event-stream", headers={"X-Accel-Buffering":"no"})


def poll(request, email_only):
    try:
        with transaction.atomic():
            auth = authenticate_mcp(request, email_only)
            return claim_change(auth) if auth else None
    except Http404:
        return None
    finally:
        # This also releases PostgreSQL connections between ticks in ASGI worker threads.
        if not connection.in_atomic_block:
            close_old_connections()


async def messages(request, changed, email_only):
    yield "retry: 5000\n\n"
    while changed is not None:
        yield event(CHANGE) if changed else ": keep-alive\n\n"
        await asyncio.sleep(POLL_SECONDS)
        changed = await sync_to_async(poll, thread_sensitive=False)(request, email_only)


def listen(request, email_only=False):
    if not accepts_stream(request):
        return HttpResponse(status=406)
    changed = poll(request, email_only)
    if changed is None:
        return unauthorized(email_only)
    return StreamingHttpResponse(messages(request, changed, email_only), content_type="text/event-stream",
                                 headers={"X-Accel-Buffering":"no"})
