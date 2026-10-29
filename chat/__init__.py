"""Dashboard chat surface (KEL-8 / P0-2).

Server-side threads, an append-only SSE wire log, cards derived from typed
envelopes, inline approvals over KEL-7 objects, and artifact serving. The UI
is a later slice; nothing here renders HTML.

Typical wiring::

    store = ChatStore("var/chat/chat.db")
    service = ChatService(store=store, provider=provider, registry=registry)
    api = ChatAPI(service)
"""

from .api import ChatAPI, LoopRunner, Request, Response, make_server, serve
from .artifacts import ArtifactError, ArtifactRegistry
from .errors import ChatError
from .service import MESSAGE_STATUS_BY_REASON, ChatService
from .store import ChatStore
from .stream import EventBus, format_event, sse_frames

__all__ = [
    "ArtifactError",
    "ArtifactRegistry",
    "ChatAPI",
    "ChatError",
    "ChatService",
    "ChatStore",
    "EventBus",
    "LoopRunner",
    "MESSAGE_STATUS_BY_REASON",
    "Request",
    "Response",
    "format_event",
    "make_server",
    "serve",
    "sse_frames",
]
