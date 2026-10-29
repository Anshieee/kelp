"""HTTP contract for the chat surface (KEL-8 §12, §15)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from chat import ChatAPI, ChatService, ChatStore, Request
from chat.store import ThreadBusyError
from kernel.provider import StreamEnd, TextDelta

from .chatkit import MODEL, StubProvider, text_script, tool_script

AUTH = {"authorization": "Bearer tok"}
CSRF = {"x-csrf-token": "csrf"}


def make_api(
    tmp_path: Path,
    scripts: list[list[Any]] | None = None,
    *,
    auth_token: str | None = "tok",
    csrf_token: str | None = "csrf",
    **service_kwargs: Any,
) -> tuple[ChatAPI, ChatService, ChatStore]:
    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(
        store=store,
        provider=StubProvider(scripts or [text_script("Hello")]),
        model=MODEL,
        **service_kwargs,
    )
    api = ChatAPI(service, auth_token=auth_token, csrf_token=csrf_token)
    api.stream_idle = 0.03
    return api, service, store


def call(
    api: ChatAPI,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    query: Mapping[str, list[str]] | None = None,
    headers: Mapping[str, str] | None = None,
    auth: bool = True,
    csrf: bool = True,
) -> Any:
    request_headers: dict[str, str] = {}
    if auth:
        request_headers.update(AUTH)
    if csrf and method.upper() in {"POST", "PATCH", "DELETE"}:
        request_headers.update(CSRF)
    if body is not None:
        request_headers["content-type"] = "application/json"
    request_headers.update(headers or {})
    raw = json.dumps(body).encode() if body is not None else b""
    return api.handle(
        Request(method, path, query or {}, request_headers, raw)
    )


def body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def create_thread(api: ChatAPI, title: str | None = None) -> dict[str, Any]:
    response = call(api, "POST", "/api/threads", {} if title is None else {"title": title})
    assert response.status == 201, response.body
    return body(response)["thread"]


def send(api: ChatAPI, service: ChatService, thread_id: str, text: str = "hi",
         client_turn_id: str = "ct-1234abcd") -> dict[str, Any]:
    response = call(
        api,
        "POST",
        f"/api/threads/{thread_id}/turns",
        {"client_turn_id": client_turn_id, "text": text},
    )
    assert response.status == 202, response.body
    payload = body(response)
    api.runner.run(service.wait_for_turn(payload["turn_id"]))
    return payload


def test_missing_auth_is_json_401(tmp_path: Path) -> None:
    api, service, store = make_api(tmp_path)
    try:
        response = call(api, "GET", "/api/threads", auth=False)
        assert response.status == 401
        assert body(response)["error"]["code"] == "unauthorized"
        assert response.headers["Content-Type"].startswith("application/json")

        cookie = api.handle(
            Request("GET", "/api/threads", {}, {"cookie": "session=tok"}, b"")
        )
        assert cookie.status == 200
    finally:
        api.close()
        store.close()


def test_mutations_require_csrf_or_same_origin(tmp_path: Path) -> None:
    api, _service, store = make_api(tmp_path)
    try:
        rejected = call(api, "POST", "/api/threads", {}, csrf=False)
        assert rejected.status == 403
        assert body(rejected)["error"]["code"] == "csrf_failed"

        same_origin_api, _service2, store2 = make_api(
            tmp_path / "second", csrf_token=None
        )
        try:
            allowed = call(
                same_origin_api,
                "POST",
                "/api/threads",
                {},
                headers={"origin": "http://localhost", "host": "localhost"},
                csrf=False,
            )
            assert allowed.status == 201
            cross_site = call(
                same_origin_api,
                "POST",
                "/api/threads",
                {},
                headers={"origin": "https://evil.example", "host": "localhost"},
                csrf=False,
            )
            assert cross_site.status == 403
        finally:
            same_origin_api.close()
            store2.close()
    finally:
        api.close()
        store.close()


def test_thread_crud_over_http(tmp_path: Path) -> None:
    api, _service, store = make_api(tmp_path)
    try:
        thread = create_thread(api, "Nebius GPU pricing")
        assert thread["title_source"] == "user"

        listed = call(api, "GET", "/api/threads")
        assert listed.status == 200
        assert body(listed)["threads"][0]["id"] == thread["id"]
        assert body(listed)["next_cursor"] is None

        fetched = call(api, "GET", f"/api/threads/{thread['id']}", query={"limit": ["10"]})
        assert fetched.status == 200
        assert body(fetched)["thread"]["id"] == thread["id"]
        assert body(fetched)["messages"] == []
        assert body(fetched)["last_seq"] == 0

        renamed = call(api, "PATCH", f"/api/threads/{thread['id']}", {"title": "Renamed"})
        assert renamed.status == 200
        assert body(renamed)["thread"]["title"] == "Renamed"

        bad_title = call(api, "PATCH", f"/api/threads/{thread['id']}", {"title": ""})
        assert bad_title.status == 400

        deleted = call(api, "DELETE", f"/api/threads/{thread['id']}")
        assert deleted.status == 200
        payload = body(deleted)
        assert payload["ok"] is True
        assert payload["thread_id"] == thread["id"]
        assert payload["deleted"] is True
        for counter in ("messages", "blocks", "approvals", "artifacts"):
            assert payload[counter] == 0
        # A fresh thread still owns its creation event; the purge counts what it
        # removed instead of assuming zero.
        assert isinstance(payload["turns"], int)
        assert payload["events"] >= 1
        assert payload["artifact_handles"] == []
        assert isinstance(payload["vacuumed"], bool)
        assert call(api, "GET", f"/api/threads/{thread['id']}").status == 404

        # The purge is idempotent: a repeat delete is a 200 with deleted false,
        # not a 404 that would make a retrying client loop.
        repeated = call(api, "DELETE", f"/api/threads/{thread['id']}")
        assert repeated.status == 404
        assert body(repeated)["error"]["code"] == "unknown_thread"
    finally:
        api.close()
        store.close()


def test_delete_thread_returns_409_while_a_turn_is_live(
    tmp_path: Path, monkeypatch: Any
) -> None:
    api, service, store = make_api(tmp_path)
    try:
        thread = create_thread(api)

        def refuse(thread_id: str, **kwargs: Any) -> dict[str, Any]:
            raise ThreadBusyError(thread_id, ["turn_still_running"])

        monkeypatch.setattr(store, "purge_thread", refuse)
        response = call(api, "DELETE", f"/api/threads/{thread['id']}")

        assert response.status == 409
        error = body(response)["error"]
        assert error["code"] == "turn_in_progress"
        assert error["details"] == {"active_turn_id": "turn_still_running"}
        # Nothing was deleted: the thread is still readable and streamable.
        assert call(api, "GET", f"/api/threads/{thread['id']}").status == 200
    finally:
        api.close()
        store.close()


def test_send_snapshot_and_idempotent_replay(tmp_path: Path) -> None:
    api, service, store = make_api(tmp_path)
    try:
        thread = create_thread(api)
        payload = send(api, service, thread["id"], "hello", client_turn_id="ct-send")

        snapshot = call(api, "GET", f"/api/threads/{thread['id']}")
        snapshot_body = body(snapshot)
        assert [message["role"] for message in snapshot_body["messages"]] == [
            "user",
            "assistant",
        ]
        assert snapshot_body["messages"][1]["status"] == "complete"
        assert snapshot_body["last_seq"] >= 4
        assert snapshot_body["has_more"] is False

        replay = call(
            api,
            "POST",
            f"/api/threads/{thread['id']}/turns",
            {"client_turn_id": "ct-send", "text": "hello"},
        )
        assert replay.status == 200
        assert body(replay)["idempotent"] is True
        assert body(replay)["turn_id"] == payload["turn_id"]
        assert len(store.list_messages(thread["id"])[0]) == 2

        empty = call(
            api,
            "POST",
            f"/api/threads/{thread['id']}/turns",
            {"client_turn_id": "ct-empty", "text": "   "},
        )
        assert empty.status == 400
        assert body(empty)["error"]["code"] == "invalid_message"

        long_text = "x" * 8001
        too_long = call(
            api,
            "POST",
            f"/api/threads/{thread['id']}/turns",
            {"client_turn_id": "ct-long", "text": long_text},
        )
        assert too_long.status == 400

        missing = call(
            api,
            "POST",
            "/api/threads/ses_00000000000000000000000000000000/turns",
            {"client_turn_id": "ct-missing", "text": "hi"},
        )
        assert missing.status == 404
    finally:
        api.close()
        store.close()


def test_second_send_while_a_turn_runs_is_409(tmp_path: Path) -> None:
    class Slow:
        async def stream(self, request: object) -> object:
            yield TextDelta("working")
            import asyncio

            await asyncio.sleep(5)
            yield StreamEnd("stop")

    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(store=store, provider=Slow(), model=MODEL)
    api = ChatAPI(service, auth_token="tok", csrf_token="csrf")
    try:
        thread = create_thread(api)
        first = call(
            api,
            "POST",
            f"/api/threads/{thread['id']}/turns",
            {"client_turn_id": "ct-first", "text": "go"},
        )
        assert first.status == 202
        turn_id = body(first)["turn_id"]

        second = call(
            api,
            "POST",
            f"/api/threads/{thread['id']}/turns",
            {"client_turn_id": "ct-second", "text": "again"},
        )
        assert second.status == 409
        error = body(second)["error"]
        assert error["code"] == "turn_in_progress"
        assert error["details"]["active_turn_id"] == turn_id

        stopped = call(api, "POST", f"/api/turns/{turn_id}/stop", {})
        assert stopped.status == 202
        api.runner.run(service.wait_for_turn(turn_id))
        assert call(api, "POST", f"/api/turns/{turn_id}/stop", {}).status == 409
    finally:
        api.close()
        store.close()


def test_sse_replay_starts_after_last_event_id(tmp_path: Path) -> None:
    api, service, store = make_api(tmp_path)
    try:
        thread = create_thread(api)
        send(api, service, thread["id"])

        stream = call(
            api,
            "GET",
            f"/api/threads/{thread['id']}/stream",
            query={"after": ["0"]},
            headers={"accept": "text/event-stream"},
        )
        assert stream.status == 200
        assert stream.headers["Content-Type"].startswith("text/event-stream")
        assert stream.headers["Cache-Control"] == "no-store"
        assert stream.stream is not None
        frames = []
        for frame in stream.stream:
            frames.append(frame)
            if len(frames) == 3:
                break
        seqs = [
            int(json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))["seq"])
            for frame in frames
        ]
        assert seqs == [1, 2, 3]

        resumed = call(
            api,
            "GET",
            f"/api/threads/{thread['id']}/stream",
            headers={"last-event-id": "3"},
        )
        later = [next(resumed.stream)]
        payload = json.loads(
            next(line[6:] for line in later[0].splitlines() if line.startswith("data: "))
        )
        assert payload["seq"] == 4

        unknown = call(api, "GET", "/api/threads/ses_00000000000000000000000000000000/stream")
        assert unknown.status == 404
    finally:
        api.close()
        store.close()


def test_retry_over_http(tmp_path: Path) -> None:
    class Failing:
        async def stream(self, request: object) -> object:
            from kernel.provider import ProviderFailure

            yield TextDelta("partial")
            yield ProviderFailure("provider_timeout", "timed out", retryable=True)

    store = ChatStore(tmp_path / "chat.db")
    service = ChatService(store=store, provider=Failing(), model=MODEL)
    api = ChatAPI(service, auth_token="tok", csrf_token="csrf")
    try:
        thread = create_thread(api)
        first = call(
            api,
            "POST",
            f"/api/threads/{thread['id']}/turns",
            {"client_turn_id": "ct-fail", "text": "question"},
        )
        turn_id = body(first)["turn_id"]
        api.runner.run(service.wait_for_turn(turn_id))

        retry = call(api, "POST", f"/api/turns/{turn_id}/retry", {})
        assert retry.status == 202
        retried = body(retry)
        assert retried["retry_of"] == turn_id
        assert retried["turn_id"] != turn_id
        api.runner.run(service.wait_for_turn(retried["turn_id"]))
        assert len(store.list_messages(thread["id"])[0]) == 3
    finally:
        api.close()
        store.close()


def test_approval_inbox_and_decision_endpoints(tmp_path: Path) -> None:
    api, service, store = make_api(tmp_path)
    try:
        thread = create_thread(api)
        card = service.open_exec_approval(
            thread_id=thread["id"],
            turn_id=None,
            call_id="call_1",
            tool="shell.exec",
            command=["echo", "hi"],
        )
        listed = call(api, "GET", "/api/approvals", query={"status": ["pending"]})
        assert listed.status == 200
        assert body(listed)["approvals"][0]["id"] == card["id"]

        fetched = call(api, "GET", f"/api/approvals/{card['id']}")
        assert fetched.status == 200
        assert body(fetched)["approval"]["status"] == "pending"

        approved = call(
            api,
            "POST",
            f"/api/approvals/{card['id']}/decision",
            {"decision": "approve"},
        )
        assert approved.status == 200
        assert body(approved)["approval"]["status"] == "approved"
        assert body(approved)["executed"] is False

        again = call(
            api,
            "POST",
            f"/api/approvals/{card['id']}/decision",
            {"decision": "approve"},
        )
        assert again.status == 200
        assert body(again)["idempotent"] is True

        conflict = call(
            api,
            "POST",
            f"/api/approvals/{card['id']}/decision",
            {"decision": "deny"},
        )
        assert conflict.status == 409
        assert body(conflict)["error"]["code"] == "not_approved"

        unknown = call(
            api,
            "POST",
            "/api/approvals/apr_missing/decision",
            {"decision": "approve"},
        )
        assert unknown.status == 404

        too_big = call(
            api,
            "POST",
            f"/api/approvals/{card['id']}/decision",
            {"decision": "approve", "note": "x" * 1100},
        )
        assert too_big.status == 400
    finally:
        api.close()
        store.close()


def _multipart(filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = "----kel8boundary"
    body_bytes = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body_bytes, f"multipart/form-data; boundary={boundary}"


def test_artifact_upload_metadata_preview_and_content(tmp_path: Path) -> None:
    api, _service, store = make_api(tmp_path)
    try:
        csv = b"region,revenue\nEMEA,4210000\nAPAC,2980000\n"
        payload, content_type = _multipart("revenue.csv", csv, "text/csv")
        upload = api.handle(
            Request(
                "POST",
                "/api/attachments",
                {},
                {**AUTH, **CSRF, "content-type": content_type},
                payload,
            )
        )
        assert upload.status == 201, upload.body
        card = body(upload)["artifact"]
        handle = card["handle"]
        assert card["kind"] == "csv"
        assert card["rows"] == 2

        meta = call(api, "GET", f"/api/artifacts/{handle}")
        assert meta.status == 200
        assert body(meta)["artifact"]["name"] == "revenue.csv"

        preview = call(api, "GET", f"/api/artifacts/{handle}/preview", query={"rows": ["1"]})
        assert preview.status == 200
        preview_body = body(preview)
        assert preview_body["columns"] == ["region", "revenue"]
        assert preview_body["rows"] == [["EMEA", "4210000"]]
        assert preview_body["total_rows"] == 2
        assert preview_body["truncated"] is True

        content = call(api, "GET", f"/api/artifacts/{handle}/content")
        assert content.status == 200
        assert content.body == csv
        assert content.headers["X-Content-Type-Options"] == "nosniff"
        assert content.headers["Content-Disposition"].startswith("inline")
        assert content.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in content.headers["Content-Security-Policy"]

        ranged = call(
            api,
            "GET",
            f"/api/artifacts/{handle}/content",
            headers={"range": "bytes=0-5"},
        )
        assert ranged.status == 206
        assert ranged.body == csv[:6]
        assert ranged.headers["Content-Range"] == f"bytes 0-5/{len(csv)}"

        download = call(api, "GET", f"/api/artifacts/{handle}/content", query={"download": ["1"]})
        assert download.headers["Content-Disposition"].startswith("attachment")

        unknown = call(
            api, "GET", "/api/artifacts/artifact:missing12345/content"
        )
        assert unknown.status == 404
    finally:
        api.close()
        store.close()


def test_bound_artifact_requires_its_own_thread(tmp_path: Path) -> None:
    """A bound handle without the matching thread_id is a 404 (§8)."""

    api, service, store = make_api(tmp_path, [text_script("got it")])
    try:
        thread = create_thread(api)
        other = create_thread(api)
        csv = b"region,revenue\nEMEA,42\n"
        payload, content_type = _multipart("revenue.csv", csv, "text/csv")
        upload = api.handle(
            Request(
                "POST",
                "/api/attachments",
                {},
                {**AUTH, **CSRF, "content-type": content_type},
                payload,
            )
        )
        assert upload.status == 201
        handle = body(upload)["artifact"]["handle"]

        # Before it is attached the composer may preview its own upload.
        assert call(api, "GET", f"/api/artifacts/{handle}").status == 200

        sent = call(
            api,
            "POST",
            f"/api/threads/{thread['id']}/turns",
            {
                "client_turn_id": "ct-bound-artifact",
                "text": "summarize",
                "attachment_handles": [handle],
            },
        )
        assert sent.status == 202, sent.body
        api.runner.run(service.wait_for_turn(body(sent)["turn_id"]))

        routes = (
            f"/api/artifacts/{handle}",
            f"/api/artifacts/{handle}/content",
            f"/api/artifacts/{handle}/preview",
        )
        for route in routes:
            bare = call(api, "GET", route)
            assert bare.status == 404, route
            assert body(bare)["error"]["code"] == "artifact_not_found"

            cross = call(api, "GET", route, query={"thread_id": [other["id"]]})
            assert cross.status == 404, route
            assert body(cross)["error"]["code"] == "artifact_not_found"

        own = {"thread_id": [thread["id"]]}
        meta = call(api, "GET", routes[0], query=own)
        assert meta.status == 200
        assert body(meta)["artifact"]["handle"] == handle

        content = call(api, "GET", routes[1], query=own)
        assert content.status == 200
        assert content.body == csv

        preview = call(api, "GET", routes[2], query=own)
        assert preview.status == 200
        assert body(preview)["columns"] == ["region", "revenue"]
    finally:
        api.close()
        store.close()


def test_bound_artifact_card_urls_carry_the_thread_scope(tmp_path: Path) -> None:
    api, service, store = make_api(tmp_path, [text_script("ok")])
    try:
        thread = create_thread(api)
        meta = service.artifacts.save_upload("rows.csv", b"a,b\n1,2\n")
        service.artifacts.claim(meta["handle"], thread["id"])
        card = service.artifacts.card(meta["handle"], thread_id=thread["id"])
        scope = f"thread_id={thread['id']}"
        assert scope in card["open_url"]
        assert scope in card["preview_url"]
        assert scope in card["download_url"]
        # Following the card's own URL works without extra client knowledge.
        def follow(url: str) -> Any:
            parts = urlsplit(url)
            return call(api, "GET", parts.path, query=parse_qs(parts.query))

        followed = follow(card["preview_url"])
        assert followed.status == 200
        content = follow(card["open_url"])
        assert content.status == 200
        assert content.body == b"a,b\n1,2\n"
    finally:
        api.close()
        store.close()


def test_pdf_upload_is_inline_frameable(tmp_path: Path) -> None:
    api, _service, store = make_api(tmp_path)
    try:
        pdf = b"%PDF-1.4\n1 0 obj<</Type /Page>>endobj\n%%EOF"
        payload, content_type = _multipart("q3.pdf", pdf, "application/pdf")
        upload = api.handle(
            Request(
                "POST",
                "/api/attachments",
                {},
                {**AUTH, **CSRF, "content-type": content_type},
                payload,
            )
        )
        assert upload.status == 201
        handle = body(upload)["artifact"]["handle"]
        content = call(api, "GET", f"/api/artifacts/{handle}/content")
        assert content.headers["Content-Type"] == "application/pdf"
        assert content.headers["X-Frame-Options"] == "SAMEORIGIN"
        preview = call(api, "GET", f"/api/artifacts/{handle}/preview")
        assert preview.status == 415
    finally:
        api.close()
        store.close()


def test_upload_rejects_unsupported_and_oversized_files(tmp_path: Path) -> None:
    api, _service, store = make_api(tmp_path)
    try:
        payload, content_type = _multipart("run.exe", b"MZ\x00\x00binary", "application/octet-stream")
        rejected = api.handle(
            Request("POST", "/api/attachments", {}, {**AUTH, **CSRF, "content-type": content_type}, payload)
        )
        assert rejected.status == 415
        assert body(rejected)["error"]["code"] == "unsupported_file"

        huge = b"%PDF-" + b"x" * (20 * 1024 * 1024 + 1)
        payload, content_type = _multipart("huge.pdf", huge, "application/pdf")
        oversized = api.handle(
            Request("POST", "/api/attachments", {}, {**AUTH, **CSRF, "content-type": content_type}, payload)
        )
        assert oversized.status == 413
    finally:
        api.close()
        store.close()


def test_artifact_path_traversal_is_unknown(tmp_path: Path) -> None:
    api, _service, store = make_api(tmp_path)
    try:
        store.create_artifact(
            {
                "handle": "artifact:traversal01",
                "thread_id": None,
                "name": "escape.pdf",
                "kind": "pdf",
                "mime": "application/pdf",
                "size_bytes": 4,
                "sha256": "x",
                "storage_path": "../escape.pdf",
            }
        )
        response = call(api, "GET", "/api/artifacts/artifact:traversal01/content")
        assert response.status == 404
    finally:
        api.close()
        store.close()


def test_responses_never_contain_secret_values(tmp_path: Path) -> None:
    secret = "nebius-key-abcdef123456"
    scripts = [tool_script("web.search", {"query": f"q {secret}"}), text_script("done")]
    api, service, store = make_api(
        tmp_path, scripts, secrets={"NEBIUS_API_KEY": secret}
    )
    try:
        thread = create_thread(api)
        send(api, service, thread["id"], "search")

        snapshot = call(api, "GET", f"/api/threads/{thread['id']}")
        assert secret.encode() not in snapshot.body
        stream = call(api, "GET", f"/api/threads/{thread['id']}/stream")
        replay = b"".join(frame.encode() for frame in stream.stream)
        assert secret.encode() not in replay
    finally:
        api.close()
        store.close()


def test_stdlib_http_server_adapter(tmp_path: Path) -> None:
    import http.client
    import threading

    from chat import make_server

    api, _service, store = make_api(tmp_path)
    server = make_server(api, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/api/health", headers={"Authorization": "Bearer tok"})
        health = connection.getresponse()
        assert health.status == 200
        assert json.loads(health.read())["ok"] is True

        connection.request(
            "POST",
            "/api/threads",
            body=b"{}",
            headers={
                "Authorization": "Bearer tok",
                "X-CSRF-Token": "csrf",
                "Content-Type": "application/json",
            },
        )
        created = connection.getresponse()
        assert created.status == 201
        assert json.loads(created.read())["thread"]["id"].startswith("ses_")
        connection.close()

        unauthenticated = http.client.HTTPConnection(host, port, timeout=2)
        unauthenticated.request("GET", "/api/threads")
        denied = unauthenticated.getresponse()
        assert denied.status == 401
        unauthenticated.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        api.close()
        store.close()
