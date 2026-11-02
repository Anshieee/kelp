#!/usr/bin/env python
"""Enumerate the ChatAPI route table and print one JSON record per request.

This is a behaviour-neutral parity check for the ``chat/api.py`` router split.
Run it inside two trees and diff the output: every method x head x rest shape
must resolve to the same handler, the same rest arguments, or the same typed
error.

    git worktree add /tmp/kelp-old origin/main
    (cd /tmp/kelp-old && python /path/to/kelp-api-routers/scripts/route_parity.py > /tmp/routes-old.json)
    python scripts/route_parity.py > /tmp/routes-new.json
    diff -u /tmp/routes-old.json /tmp/routes-new.json

Handlers are replaced with a recorder so the comparison is about route
resolution, not service state. The health and metrics branches run against
deterministic fakes, so their bodies compare too.

It is deliberately not a pytest test: the router split must not change the
suite's test count or add a new behavioural surface.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.getcwd())

from chat.api import ChatAPI, Request, Response  # noqa: E402
from chat.errors import ChatError  # noqa: E402

METHODS = ["GET", "POST", "PATCH", "DELETE", "PUT", "HEAD", "OPTIONS"]
HEADS = [
    "auth",
    "threads",
    "turns",
    "approvals",
    "attachments",
    "artifacts",
    "health",
    "healthz",
    "readyz",
    "metrics",
    "shared",
    "unknown",
]
RESTS = [
    [],
    ["a"],
    ["a", "b"],
    ["a", "b", "c"],
    ["session"],
    ["sessions"],
    ["devices"],
    ["devices", "abcd1234"],
    ["share"],
    ["shares"],
    ["shares", "abcd1234"],
    ["turns"],
    ["stream"],
    ["stop"],
    ["retry"],
    ["decision"],
    ["preview"],
    ["content"],
    ["a", "stream"],
    ["a", "turns"],
    ["a", "share"],
    ["a", "shares"],
    ["a", "shares", "abcd1234"],
    ["a", "decision"],
    ["a", "preview"],
    ["a", "content"],
]


class Slots:
    limit = 1

    def stats(self) -> tuple[int, int]:
        return 0, 1


class Health:
    def liveness(self) -> dict[str, object]:
        return {"ok": True, "probe": "live"}

    def readiness(self, *, runner: object = None) -> tuple[dict[str, object], bool]:
        return {"ok": True, "probe": "ready"}, True


class Metrics:
    def render(self) -> str:
        return ""


class Service:
    def publish_spend_metrics(self) -> None:
        return None


def make_api() -> ChatAPI:
    api = ChatAPI.__new__(ChatAPI)
    api.health = Health()
    api.metrics = Metrics()
    api.service = Service()
    api.runner = object()
    api._turn_slots = Slots()
    api._stream_slots = Slots()
    api._share_stream_slots = Slots()
    return api


def marker(name: str, rest: list[str]) -> Response:
    body = json.dumps({"handler": name, "rest": rest}, separators=(",", ":")).encode()
    return Response(status=200, body=body)


def install_old(api: ChatAPI) -> None:
    api._auth = lambda request, method, rest: marker("auth", rest)
    api._threads = lambda request, method, rest: marker("threads", rest)
    api._turns = lambda request, method, rest: marker("turns", rest)
    api._approvals = lambda request, method, rest: marker("approvals", rest)
    api._upload = lambda request: marker("upload", [])
    api._artifacts = lambda request, method, rest: marker("artifacts", rest)


def install_new(api: ChatAPI) -> None:
    from chat.routers import approvals, artifacts, auth, threads, turns

    auth._auth = lambda api, request, method, rest: marker("auth", rest)
    threads._threads = (
        lambda api, request, method, rest, frames_fn=None: marker("threads", rest)
    )
    turns._turns = lambda api, request, method, rest: marker("turns", rest)
    approvals._approvals = lambda api, request, method, rest: marker("approvals", rest)
    artifacts._upload = lambda api, request: marker("upload", [])
    artifacts._artifacts = lambda api, request, method, rest: marker("artifacts", rest)


def describe(response: Response) -> dict[str, object]:
    return {
        "status": response.status,
        "headers": dict(sorted(response.headers.items())),
        "cookies": list(response.cookies),
        "body": response.body.decode("utf-8", "replace"),
    }


def main() -> None:
    api = make_api()
    if hasattr(ChatAPI, "_auth"):
        install_old(api)
    else:
        install_new(api)

    records = []
    for head in HEADS:
        for rest in RESTS:
            for method in METHODS:
                segments = [head, *rest]
                request = Request(method=method, path="/api/" + "/".join(segments))
                try:
                    outcome = describe(api._route(request, segments))
                except ChatError as exc:
                    outcome = {"error": exc.code, "status": exc.status}
                except Exception as exc:  # noqa: BLE001 - record, then compare
                    outcome = {"error": type(exc).__name__}
                records.append(
                    {
                        "method": method,
                        "segments": segments,
                        "outcome": outcome,
                    }
                )
    records.sort(key=lambda row: (row["segments"], row["method"]))
    print(json.dumps({"routes": records}, sort_keys=True, indent=0))


if __name__ == "__main__":
    main()
