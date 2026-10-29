"""A real chat server for beat 6 — the process the demo kills mid-answer.

Run by the demo (not by hand); it opens the run's database and artifact root,
serves ``ChatAPI`` on an ephemeral loopback port, starts one slow turn, prints a
single JSON line so the parent knows it is ready, and then waits to be killed.

Only loopback, only local files. No keys and no outbound network.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from typing import Sequence

from chat.api import ChatAPI, make_server
from chat.artifacts import ArtifactRegistry
from chat.service import ChatService
from chat.store import ChatStore
from demo.config import AUTH_TOKEN, MODEL, DemoPaths
from demo.fixtures.provider import SlowTextProvider
from demo.fixtures.registry import build_registry
from demo.surface import Surface

__all__ = ["main"]

WORDS: Sequence[str] = (
    "The demo server is streaming this answer word by word so the kill lands "
    "in the middle of a turn. Every token is written to the append-only event "
    "log before it is sent, which is why the conversation survives a SIGKILL. "
    "When the process comes back the turn is marked interrupted, the partial "
    "text is still readable, and a retry opens a fresh turn over the same "
    "question instead of duplicating it."
).split()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Demo chat server (beat 6).")
    parser.add_argument("--state", required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--client-turn-id", required=True)
    args = parser.parse_args(argv)

    paths = DemoPaths.build(args.state)
    store = ChatStore(paths.db)
    service = ChatService(
        store=store,
        provider=SlowTextProvider(WORDS, delay=0.02),
        registry=build_registry(paths.skills),
        artifacts=ArtifactRegistry(store, paths.artifacts),
        model=MODEL,
        workspace_root=str(paths.workspace),
        tools=(),
    )
    api = ChatAPI(service, auth_token=AUTH_TOKEN)
    server = make_server(api, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="demo-http")
    thread.start()

    payload = Surface(api).send(
        args.thread, args.text, client_turn_id=args.client_turn_id, expect=(202,)
    )
    print(
        json.dumps(
            {
                "ready": True,
                "port": int(server.server_address[1]),
                "thread_id": args.thread,
                "turn_id": payload["turn_id"],
            }
        ),
        flush=True,
    )
    try:
        thread.join()
    except KeyboardInterrupt:  # pragma: no cover - the demo sends SIGKILL
        pass
    finally:
        server.server_close()
        api.close()
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
