"""KEL-13 M2-C: the content-addressed index under ``var/index``.

Covers the SHA-256 key, byte-identical rebuilds, cache hits, invalidation when
the artifact changes, stale-index pruning when it disappears, restrictive
permissions, and the "no secrets, no questions" rule.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from pathlib import Path

import pytest

from skills.builtin.docs_answer import embeddings
from skills.builtin.docs_answer.chunking import chunk_from_dict, chunk_to_dict
from skills.builtin.docs_answer.handler import run
from skills.builtin.docs_answer.index import (
    INDEX_VERSION,
    artifact_sha256,
    ensure_index,
    index_root,
    prune_indexes,
    remove_index,
)
from skills.builtin.docs_answer.paths import artifact_path
from skills.context import SkillContext

from .test_docs_answer import HANDLE, make_pdf, workspace


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trapping outward sockets keeps this module offline too."""

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("docs.answer attempted network access")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    embeddings.reset_embedder()


def _index_files(root: Path) -> list[Path]:
    return sorted((index_root(root)).rglob("*.json"))


def _table_csv() -> bytes:
    return b"region,revenue_usd,orders\nemea,412300,1840\napac,135300,612\n"


def test_index_is_keyed_by_the_artifact_sha256(tmp_path: Path) -> None:
    context, root = workspace(tmp_path, {"abcdefgh1234.csv": _table_csv()})
    path = artifact_path(context, HANDLE, "csv")

    result = ensure_index(root, handle=HANDLE, kind="csv", path=path)

    assert result.rebuilt is True
    digest = artifact_sha256(path)
    assert result.path == index_root(root) / "abcdefgh1234" / "csv" / f"{digest}.json"
    assert result.path.is_file()
    payload = json.loads(result.path.read_text())
    assert payload["version"] == INDEX_VERSION
    assert payload["artifact"]["sha256"] == digest
    assert payload["artifact"]["rows"] == 2
    assert payload["header"] == ["region", "revenue_usd", "orders"]
    assert all(chunk["rows"] for chunk in payload["chunks"])


def test_index_is_private_on_disk(tmp_path: Path) -> None:
    context, root = workspace(tmp_path, {"abcdefgh1234.csv": _table_csv()})
    path = artifact_path(context, HANDLE, "csv")
    result = ensure_index(root, handle=HANDLE, kind="csv", path=path)

    directory_mode = stat.S_IMODE(os.stat(result.path.parent).st_mode)
    file_mode = stat.S_IMODE(os.stat(result.path).st_mode)
    assert directory_mode == 0o700
    assert file_mode == 0o600


def test_cache_hit_does_not_rewrite_the_file(tmp_path: Path) -> None:
    context, root = workspace(tmp_path, {"abcdefgh1234.csv": _table_csv()})
    path = artifact_path(context, HANDLE, "csv")

    first = ensure_index(root, handle=HANDLE, kind="csv", path=path)
    before = os.stat(first.path)

    second = ensure_index(root, handle=HANDLE, kind="csv", path=path)
    after = os.stat(second.path)

    assert second.rebuilt is False
    assert second.payload == first.payload
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_rebuild_is_byte_identical_and_path_independent(tmp_path: Path) -> None:
    data = make_pdf([["Support", "Support response target is 30 minutes."]])
    left_context, left_root = workspace(tmp_path / "left", {"abcdefgh1234.pdf": data})
    right_context, right_root = workspace(tmp_path / "right", {"abcdefgh1234.pdf": data})

    left = ensure_index(
        left_root, handle=HANDLE, kind="pdf", path=artifact_path(left_context, HANDLE, "pdf")
    )
    right = ensure_index(
        right_root, handle=HANDLE, kind="pdf", path=artifact_path(right_context, HANDLE, "pdf")
    )
    assert left.path.read_bytes() == right.path.read_bytes()

    # Delete and rebuild: the same bytes come back, and the whole workspace
    # path never leaks into the payload.
    expected = left.path.read_bytes()
    left.path.unlink()
    rebuilt = ensure_index(
        left_root, handle=HANDLE, kind="pdf", path=artifact_path(left_context, HANDLE, "pdf")
    )
    assert rebuilt.rebuilt is True
    assert rebuilt.path.read_bytes() == expected
    assert str(left_root).encode() not in expected


def test_editing_the_artifact_replaces_its_index(tmp_path: Path) -> None:
    context, root = workspace(tmp_path, {"abcdefgh1234.csv": _table_csv()})
    path = artifact_path(context, HANDLE, "csv")
    first = ensure_index(root, handle=HANDLE, kind="csv", path=path)

    path.write_bytes(b"region,revenue_usd,orders\nemea,999,1\n")
    second = ensure_index(root, handle=HANDLE, kind="csv", path=path)

    assert second.rebuilt is True
    assert second.path != first.path
    assert not first.path.exists()
    assert _index_files(root) == [second.path]
    assert second.payload["artifact"]["rows"] == 1


def test_corrupt_or_foreign_index_is_rebuilt(tmp_path: Path) -> None:
    context, root = workspace(tmp_path, {"abcdefgh1234.csv": _table_csv()})
    path = artifact_path(context, HANDLE, "csv")
    first = ensure_index(root, handle=HANDLE, kind="csv", path=path)

    for tamper in (
        lambda payload: b"{not json",
        lambda payload: json.dumps({**payload, "version": 999}).encode(),
        lambda payload: json.dumps({**payload, "chunks": "nope"}).encode(),
        lambda payload: json.dumps(
            {**payload, "artifact": {**payload["artifact"], "kind": "pdf"}}
        ).encode(),
    ):
        first.path.write_bytes(tamper(json.loads(first.path.read_text())))
        result = ensure_index(root, handle=HANDLE, kind="csv", path=path)
        assert result.rebuilt is True
        assert result.payload["version"] == INDEX_VERSION
        assert result.payload["chunks"]


def test_deleting_the_artifact_prunes_its_index(tmp_path: Path) -> None:
    context, root = workspace(tmp_path, {"abcdefgh1234.csv": _table_csv()})
    path = artifact_path(context, HANDLE, "csv")
    ensure_index(root, handle=HANDLE, kind="csv", path=path)
    assert _index_files(root)

    path.unlink()
    removed = prune_indexes(root)

    assert removed == ["abcdefgh1234"]
    assert not (index_root(root) / "abcdefgh1234").exists()
    assert _index_files(root) == []


def test_the_next_question_prunes_the_deleted_artifact(tmp_path: Path) -> None:
    context, root = workspace(
        tmp_path,
        {
            "abcdefgh1234.csv": _table_csv(),
            "abcdefgh5678.csv": _table_csv(),
        },
    )
    for handle in (HANDLE, "artifact:abcdefgh5678"):
        asyncio.run(
            run({"artifact": handle, "kind": "csv", "question": "total revenue"}, context)
        )
    assert len(_index_files(root)) == 2

    (root / "var" / "artifacts" / "abcdefgh5678.csv").unlink()
    asyncio.run(run({"artifact": HANDLE, "kind": "csv", "question": "total revenue"}, context))

    remaining = _index_files(root)
    assert len(remaining) == 1
    assert "abcdefgh1234" in str(remaining[0])
    assert not (index_root(root) / "abcdefgh5678").exists()


def test_remove_index_is_explicit_and_idempotent(tmp_path: Path) -> None:
    context, root = workspace(tmp_path, {"abcdefgh1234.csv": _table_csv()})
    ensure_index(
        root, handle=HANDLE, kind="csv", path=artifact_path(context, HANDLE, "csv")
    )

    assert remove_index(root, HANDLE) is True
    assert remove_index(root, HANDLE) is False
    with pytest.raises(ValueError):
        remove_index(root, "../../etc")


def test_prune_ignores_unrelated_entries(tmp_path: Path) -> None:
    _, root = workspace(tmp_path, {})
    root_index = index_root(root)
    stray = root_index / "other"
    note = root_index / "README.txt"
    stray.mkdir(parents=True)
    note.write_text("not an index")

    prune_indexes(root)

    assert stray.is_dir()
    assert note.is_file()


def test_index_never_stores_the_question_or_context_secrets(tmp_path: Path) -> None:
    secret = "sk-index-super-secret"
    root = tmp_path / "ws"
    (root / "var" / "artifacts").mkdir(parents=True)
    (root / "var" / "artifacts" / "abcdefgh1234.pdf").write_bytes(
        make_pdf([["Support", "Support response target is 30 minutes."]])
    )
    context = SkillContext("docs.answer", root, {"DEMO_TOKEN": secret})

    asyncio.run(
        run(
            {
                "artifact": HANDLE,
                "kind": "pdf",
                "question": "What is the ninja-support target?",
            },
            context,
        )
    )

    index_text = "\n".join(path.read_text() for path in _index_files(root))
    assert secret not in index_text
    assert "ninja-support" not in index_text
    assert "question" not in json.loads(_index_files(root)[0].read_text())


def test_swapping_the_embedding_model_invalidates_the_cache(tmp_path: Path) -> None:
    context, root = workspace(
        tmp_path,
        {"abcdefgh1234.pdf": make_pdf([["Support", "Support response target is 30 minutes."]])},
    )
    path = artifact_path(context, HANDLE, "pdf")

    lexical = ensure_index(root, handle=HANDLE, kind="pdf", path=path)
    assert lexical.payload["embedding_requested"] is None

    embeddings.set_embedder("toy", lambda texts: [[1.0] for _ in texts])
    hooked = ensure_index(
        root,
        handle=HANDLE,
        kind="pdf",
        path=path,
        embedder=embeddings.get_embedder(),
    )
    assert hooked.rebuilt is True
    assert hooked.payload["embedding_model"] == "toy"

    # Same model: the vectors are served from cache, not recomputed.
    calls: list[int] = []

    def counting(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        return [[1.0] for _ in texts]

    embeddings.set_embedder("toy", counting)
    cached = ensure_index(
        root, handle=HANDLE, kind="pdf", path=path, embedder=embeddings.get_embedder()
    )
    assert cached.rebuilt is False
    assert calls == []
    embeddings.reset_embedder()


def test_chunk_payload_round_trips(tmp_path: Path) -> None:
    context, root = workspace(tmp_path, {"abcdefgh1234.csv": _table_csv()})
    payload = ensure_index(
        root, handle=HANDLE, kind="csv", path=artifact_path(context, HANDLE, "csv")
    ).payload

    first = payload["chunks"][0]
    assert chunk_to_dict(chunk_from_dict(first)) == first
