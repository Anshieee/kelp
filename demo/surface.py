"""A thin client over ``chat.api.ChatAPI`` used by every beat.

The demo drives the real HTTP contract through ``ChatAPI.handle`` rather than
poking the service directly, so a routing, auth, or serialization regression
fails the demo. Only the socket is skipped for beats 1-5; beat 6 runs the same
object over a real ``ThreadingHTTPServer`` in a child process.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Mapping, Sequence

from chat.api import ChatAPI, Request, Response
from demo.config import AUTH_TOKEN

__all__ = ["Surface", "parse_sse"]

_BOUNDARY = "----kel9demoboundary"


class Surface:
    def __init__(self, api: ChatAPI):
        self.api = api

    # -- requests ----------------------------------------------------------

    def json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Sequence[str]] | None = None,
        body: Mapping[str, Any] | None = None,
        expect: Sequence[int] = (200, 201, 202),
    ) -> dict[str, Any]:
        response = self.raw(
            method, path, query=query, body=body, expect=expect
        )
        if not response.body:
            return {}
        return json.loads(response.body.decode("utf-8"))

    def raw(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Sequence[str]] | None = None,
        body: Mapping[str, Any] | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        expect: Sequence[int] = (200, 201, 202),
    ) -> Response:
        payload = b""
        merged = {"authorization": f"Bearer {AUTH_TOKEN}"}
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            merged["content-type"] = "application/json"
        if headers:
            merged.update(headers)
        request = Request(
            method=method.upper(),
            path=path,
            query={key: list(value) for key, value in (query or {}).items()},
            headers=merged,
            body=payload,
        )
        response = self.api.handle(request)
        if response.status not in expect:
            detail = response.body.decode("utf-8", "replace")[:400] if response.body else ""
            raise RuntimeError(f"{method} {path} -> {response.status}: {detail}")
        return response

    # -- endpoints ---------------------------------------------------------

    def create_thread(self, title: str) -> dict[str, Any]:
        return self.json("POST", "/api/threads", body={"title": title})["thread"]

    def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        head = (
            f"--{_BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        body = head + data + f"\r\n--{_BOUNDARY}--\r\n".encode("utf-8")
        response = self.raw(
            "POST",
            "/api/attachments",
            body=body,
            headers={"content-type": f"multipart/form-data; boundary={_BOUNDARY}"},
            expect=(201,),
        )
        return json.loads(response.body.decode("utf-8"))["artifact"]

    def send(
        self,
        thread_id: str,
        text: str,
        *,
        client_turn_id: str,
        attachment_handles: Sequence[str] = (),
        expect: Sequence[int] = (202, 200),
    ) -> dict[str, Any]:
        return self.json(
            "POST",
            f"/api/threads/{thread_id}/turns",
            body={
                "text": text,
                "client_turn_id": client_turn_id,
                "attachment_handles": list(attachment_handles),
            },
            expect=expect,
        )

    def snapshot(self, thread_id: str) -> dict[str, Any]:
        return self.json("GET", f"/api/threads/{thread_id}")

    def decide(self, approval_id: str, decision: str) -> dict[str, Any]:
        return self.json(
            "POST",
            f"/api/approvals/{approval_id}/decision",
            body={"decision": decision},
        )

    def events(self, thread_id: str) -> list[dict[str, Any]]:
        return self.api.service.store.events_after(thread_id, 0, limit=10_000)

    def wait_for_turn(self, turn_id: str, timeout: float = 20.0) -> None:
        self.api.runner.run(self.api.service.wait_for_turn(turn_id, timeout=timeout))

    # -- sse ---------------------------------------------------------------

    def stream(
        self, thread_id: str, *, after: int = 0, max_events: int = 5000
    ) -> Iterator[tuple[int, str, dict[str, Any]]]:
        response = self.raw(
            "GET",
            f"/api/threads/{thread_id}/stream",
            query={"after": [str(after)]},
        )
        if response.stream is None:  # pragma: no cover - router always streams
            return iter(())
        return parse_sse(response.stream, max_events=max_events)


def parse_sse(
    frames: Iterator[str], *, max_events: int = 5000
) -> Iterator[tuple[int, str, dict[str, Any]]]:
    """Yield ``(seq, type, payload)`` from ``sse_frames`` output."""

    seen = 0
    seq: int | None = None
    event_type = ""
    for frame in frames:
        lines = frame.splitlines()
        for line in lines:
            if line.startswith("id: "):
                seq = int(line[4:])
            elif line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if seq is None or payload.get("seq") != seq:  # pragma: no cover
                    raise AssertionError("SSE frame id does not match its payload")
                yield seq, event_type, payload
                seen += 1
                if seen >= max_events:
                    return
                seq, event_type = None, ""
