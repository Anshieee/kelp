"""A real chat server for beat 6 — the process the demo kills mid-answer.

Run by the demo (not by hand). Two modes, both on an ephemeral loopback port:

``--thread/--text/--client-turn-id``
    Open the run's database, start one slow turn, print a single JSON ready
    line, and serve until killed. This is the process the demo ``SIGKILL``s
    mid-stream.

``--recover``
    Boot as the *restarted* process: open the same database, run the service's
    restart step (``recover_interrupted``), print one JSON ready line naming the
    turns it closed, then serve ``ChatAPI`` until told to stop. The demo drives
    the post-kill snapshot, SSE replay, retry, and idempotent resend over this
    process's real socket, so the recovery path is exercised the way a deploy
    would: a new process, not a new object in the old one.

Only loopback, only local files. No keys and no outbound network.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from typing import Sequence

from chat.api import ChatAPI, make_server
from chat.artifacts import ArtifactRegistry
from chat.service import ChatService
from chat.store import ChatStore
from demo.config import AUTH_TOKEN, MODEL, DemoPaths
from demo.fixtures.provider import FixtureProvider, SlowTextProvider
from demo.fixtures.registry import build_registry
from demo.runner import RESUME_SCRIPT
from demo.surface import Surface

__all__ = ["ANSWER_CHARS", "WORDS", "main"]

WORDS: Sequence[str] = (
    "The demo server is streaming this answer word by word so the kill lands "
    "in the middle of a turn. Every token is written to the append-only event "
    "log before it is sent, which is why the conversation survives a SIGKILL. "
    "When the process comes back the turn is marked interrupted, the partial "
    "text is still readable, and a retry opens a fresh turn over the same "
    "question instead of duplicating it."
).split()

# What the killed turn would have streamed if nobody had pulled the plug.
ANSWER_CHARS = sum(len(word) + 1 for word in WORDS)


def _build_service(paths: DemoPaths, store: ChatStore, provider: object) -> ChatService:
    return ChatService(
        store=store,
        provider=provider,
        registry=build_registry(paths.skills),
        artifacts=ArtifactRegistry(store, paths.artifacts),
        model=MODEL,
        workspace_root=str(paths.workspace),
        tools=(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Demo chat server (beat 6).")
    parser.add_argument("--state", required=True)
    parser.add_argument("--thread", help="thread to open a turn on (turn mode)")
    parser.add_argument("--text", help="turn text (turn mode)")
    parser.add_argument("--client-turn-id", help="idempotency key (turn mode)")
    parser.add_argument(
        "--recover",
        action="store_true",
        help="boot as the restarted process: close interrupted turns, then serve",
    )
    args = parser.parse_args(argv)
    if not args.recover and not (args.thread and args.text and args.client_turn_id):
        parser.error("--thread, --text and --client-turn-id are required unless --recover is used")

    paths = DemoPaths.build(args.state)
    store = ChatStore(paths.db)
    # The restarted process only ever replays the scripted resume answer.
    provider = FixtureProvider([RESUME_SCRIPT]) if args.recover else SlowTextProvider(WORDS, delay=0.02)
    service = _build_service(paths, store, provider)
    api = ChatAPI(service, auth_token=AUTH_TOKEN)
    server = make_server(api, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="demo-http")
    thread.start()

    def stop(_signum: int, _frame: object) -> None:  # pragma: no cover - signal path
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        payload: dict[str, object] = {
            "ready": True,
            "port": int(server.server_address[1]),
            "mode": "recover" if args.recover else "turn",
        }
        if args.recover:
            # Restart step first: by the time the parent reads this line the
            # interrupted turn is already closed and announced.
            payload["recovered"] = service.recover_interrupted()
        else:
            sent = Surface(api).send(
                str(args.thread),
                str(args.text),
                client_turn_id=str(args.client_turn_id),
                expect=(202,),
            )
            payload.update(
                {
                    "thread_id": args.thread,
                    "turn_id": sent["turn_id"],
                    "answer_chars": ANSWER_CHARS,
                    "steps": len(WORDS),
                }
            )
        print(json.dumps(payload), flush=True)
        thread.join()
    finally:
        server.server_close()
        api.close()
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
