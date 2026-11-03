"""Tests for the phone app's static mount (chat/phone.py).

The mount serves the built PWA shell at ``/phone/*``: public, read-only,
traversal-safe, with an explicit cache policy. The shell carries no owner
data — every ``/api`` call the shell makes enforces the owner session — so
these tests pin the serving rules, not any auth behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chat.api import ChatAPI
from chat.store import ChatStore
from tests.chatkit import MODEL, StubProvider, text_script
from chat.service import ChatService
from openshell.audit import AuditLog


class _FakeRegistry:
    def names(self) -> list[str]:
        return ["docs"]


@pytest.fixture()
def phone_dist(tmp_path: Path) -> Path:
    """A minimal fake build output."""

    dist = tmp_path / "phone-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "icons").mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><html><body>phone shell</body></html>", encoding="utf-8"
    )
    (dist / "sw.js").write_text("self.addEventListener('fetch', () => {});", encoding="utf-8")
    (dist / "manifest.webmanifest").write_text(
        json.dumps({"name": "Kelp", "display": "standalone"}), encoding="utf-8"
    )
    (dist / "assets" / "app-abc123.css").write_text("body{}", encoding="utf-8")
    (dist / "icons" / "icon-192.png").write_bytes(b"\x89PNG fake")
    (dist / "secret.txt").write_text("not for the web", encoding="utf-8")
    return dist


class PhoneStack:
    """ChatAPI wired to a fake phone build, mirroring tests/test_health.py."""

    def __init__(self, tmp_path: Path, phone_root: Path | None) -> None:
        self.store = ChatStore(tmp_path / "db" / "chat.db")
        audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
        self.service = ChatService(
            store=self.store,
            provider=StubProvider([text_script("Hello")]),
            registry=_FakeRegistry(),
            model=MODEL,
            audit=audit,
        )
        self.api = ChatAPI(
            self.service,
            auth_token="root-token",
            csrf_token="csrf",
            phone_root=phone_root,
        )

    def close(self) -> None:
        self.api.close()


def _get(api: ChatAPI, path: str, method: str = "GET"):
    from chat.api import Request

    request = Request(
        method=method,
        path=path,
        headers={},
        body=b"",
        client="127.0.0.1",
    )
    return api.handle(request)


def test_index_is_served_at_the_mount_root(phone_dist: Path, tmp_path: Path) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        response = _get(stack.api, "/phone/")
        assert response.status == 200
        assert response.headers["Content-Type"] == "text/html"
        assert b"phone shell" in response.body
        assert response.headers["Cache-Control"] == "no-cache"
    finally:
        stack.close()


def test_bare_prefix_serves_the_shell(phone_dist: Path, tmp_path: Path) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        assert _get(stack.api, "/phone").status == 200
    finally:
        stack.close()


def test_service_worker_is_no_cache_and_js(phone_dist: Path, tmp_path: Path) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        response = _get(stack.api, "/phone/sw.js")
        assert response.status == 200
        assert response.headers["Content-Type"] == "text/javascript"
        assert response.headers["Cache-Control"] == "no-cache"
    finally:
        stack.close()


def test_manifest_has_the_pwa_mime(phone_dist: Path, tmp_path: Path) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        response = _get(stack.api, "/phone/manifest.webmanifest")
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/manifest+json"
    finally:
        stack.close()


def test_hashed_assets_are_immutable(phone_dist: Path, tmp_path: Path) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        response = _get(stack.api, "/phone/assets/app-abc123.css")
        assert response.status == 200
        assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    finally:
        stack.close()


def test_unknown_extensionless_path_falls_back_to_the_shell(
    phone_dist: Path, tmp_path: Path
) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        response = _get(stack.api, "/phone/settings")
        assert response.status == 200
        assert b"phone shell" in response.body
    finally:
        stack.close()


def test_unknown_file_path_is_a_plain_404(phone_dist: Path, tmp_path: Path) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        response = _get(stack.api, "/phone/assets/missing-xyz.css")
        assert response.status == 404
    finally:
        stack.close()


def test_traversal_is_an_indistinguishable_404(
    phone_dist: Path, tmp_path: Path
) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        for path in (
            "/phone/../secret.txt",
            "/phone/..%2Fsecret.txt",
            "/phone/....//secret.txt",
        ):
            response = _get(stack.api, path)
            assert response.status == 404, path
            assert b"not for the web" not in response.body
    finally:
        stack.close()


def test_mutations_are_refused(phone_dist: Path, tmp_path: Path) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        response = _get(stack.api, "/phone/", method="POST")
        assert response.status == 405
    finally:
        stack.close()


def test_unbuilt_root_answers_phone_not_built(tmp_path: Path) -> None:
    stack = PhoneStack(tmp_path, tmp_path / "absent-dist")
    try:
        response = _get(stack.api, "/phone/")
        assert response.status == 404
        body = json.loads(response.body)
        assert body["error"]["code"] == "phone_not_built"
    finally:
        stack.close()


def test_api_routes_are_unaffected_by_the_mount(
    phone_dist: Path, tmp_path: Path
) -> None:
    stack = PhoneStack(tmp_path, phone_dist)
    try:
        response = _get(stack.api, "/api/healthz")
        assert response.status == 200
        # An /api path that merely contains the word phone is still API.
        response = _get(stack.api, "/api/phone")
        assert response.status in (401, 404)
    finally:
        stack.close()
