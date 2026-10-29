"""Card derivation and redaction (KEL-8 §6-§9)."""

from __future__ import annotations

from typing import Any

from chat.cards import (
    APPROVAL_RATIONALE_SUFFIX,
    apply_sweeps,
    approval_card,
    approval_rationale,
    artifact_card,
    error_copy,
    merge_sources,
    redact_arguments,
    sources_from_envelope,
    summarize_arguments,
    sweep_items,
)


def test_display_rules_are_deterministic() -> None:
    assert summarize_arguments("web.search", {"query": "Nebius GPU pricing"}) == (
        "Nebius GPU pricing"
    )
    assert summarize_arguments("docs.answer", {"question": "Total revenue?"}) == (
        "Total revenue?"
    )
    assert summarize_arguments(
        "github.read", {"kind": "issue", "repo": "Anshieee/nebius-nvidia"}
    ) == "Anshieee/nebius-nvidia, issue"
    assert summarize_arguments(
        "schedule.create",
        {"title": "Weekday summary", "schedule": {"expr": "0 8 * * 1-5"}},
    ) == "Weekday summary, 0 8 * * 1-5"
    assert summarize_arguments("shell.exec", {"command": ["echo", "hi"]}) == "echo hi"


def test_fallback_summary_uses_scalars_and_skips_secret_keys() -> None:
    summary = summarize_arguments(
        "custom.tool",
        {"alpha": "one", "api_key": "do-not-show", "beta": 2, "gamma": True, "delta": "no"},
    )
    assert summary == "alpha=one, beta=2, gamma=true"
    assert "do-not-show" not in summary


def test_summary_strips_newlines_and_truncates_at_120() -> None:
    summary = summarize_arguments("web.search", {"query": "a\nb   c"})
    assert summary == "a b c"
    long = summarize_arguments("web.search", {"query": "x" * 300})
    assert len(long) == 120
    assert long.endswith("…")


def test_redact_arguments_removes_values_and_secret_keys() -> None:
    cleaned = redact_arguments(
        {"note": "token is s3cret-value", "api_key": "abc", "query": "keep"},
        secrets=("s3cret-value",),
    )
    assert cleaned == {
        "note": "token is [redacted]",
        "api_key": "[redacted]",
        "query": "keep",
    }


def test_redact_arguments_caps_at_4kb_without_partial_payload() -> None:
    cleaned = redact_arguments({"blob": "x" * 5000})
    assert cleaned == {"truncated": True}


def test_web_sources_skip_non_http_urls_and_keep_order() -> None:
    envelope = {
        "ok": True,
        "output": {
            "results": [
                {"title": "One", "url": "https://one.example/a", "snippet": "first"},
                {"title": "Bad", "url": "javascript:alert(1)"},
                {"title": "Two", "url": "http://two.example/b"},
            ]
        },
    }
    sources = sources_from_envelope("web.search", envelope)
    assert [source["url"] for source in sources] == [
        "https://one.example/a",
        "http://two.example/b",
    ]
    assert all(source["kind"] == "web" for source in sources)


def test_failed_search_yields_one_failed_chip() -> None:
    sources = sources_from_envelope(
        "web.search", {"ok": False, "error": {"code": "timeout"}}
    )
    assert sources == [
        {
            "id": "src_0",
            "kind": "web",
            "title": "Web search failed",
            "status": "failed",
            "tool": "web.search",
        }
    ]


def test_docs_sources_use_artifact_name_and_locator() -> None:
    envelope = {
        "ok": True,
        "output": {
            "answer": "…",
            "artifact": {"handle": "artifact:q3report01", "kind": "pdf", "mime": "application/pdf"},
            "citations": [{"locator": "p.4", "text": "Revenue was $4.2M"}],
        },
    }
    sources = sources_from_envelope(
        "docs.answer", envelope, artifact={"name": "q3-report.pdf", "handle": "artifact:q3report01"}
    )
    assert sources[0]["title"] == "q3-report.pdf"
    assert sources[0]["handle"] == "artifact:q3report01"
    assert sources[0]["locator"] == "p.4"
    assert sources[0]["snippet"] == "Revenue was $4.2M"


def test_github_source_uses_html_url() -> None:
    envelope = {
        "ok": True,
        "output": {
            "kind": "issue",
            "issue": {
                "number": 12,
                "title": "Wire the approval card",
                "state": "open",
                "html_url": "https://github.com/a/b/issues/12",
                "body": "details",
            },
        },
    }
    sources = sources_from_envelope("github.read", envelope)
    assert sources[0]["url"] == "https://github.com/a/b/issues/12"
    assert sources[0]["title"] == "Wire the approval card"


def test_merge_sources_dedupes_caps_and_counts_overflow() -> None:
    new = [
        {"kind": "web", "title": f"T{i}", "url": f"https://e.example/{i}", "status": "ok"}
        for i in range(10)
    ]
    visible, total = merge_sources([], new)
    assert len(visible) == 8
    assert total == 10
    assert [source["id"] for source in visible] == [f"src_{i}" for i in range(8)]

    visible, total = merge_sources([{"kind": "web", "url": "https://e.example/1"}], new)
    assert total == 10
    assert visible[0]["url"] == "https://e.example/1"


def test_artifact_card_urls_follow_kind() -> None:
    pdf = artifact_card(
        {
            "handle": "artifact:q3report01",
            "name": "q3-report.pdf",
            "kind": "pdf",
            "mime": "application/pdf",
            "size_bytes": 1843200,
            "pages": 12,
            "created_at": "2026-10-29T09:10:00Z",
        }
    )
    assert pdf["preview_url"] == "/api/artifacts/artifact:q3report01/content#page=1&toolbar=0"
    assert pdf["download_url"].endswith("?download=1")

    csv = artifact_card(
        {"handle": "artifact:csvreport1", "name": "rows.csv", "kind": "csv", "mime": "text/csv"}
    )
    assert csv["preview_url"] == "/api/artifacts/artifact:csvreport1/preview"


def test_approval_rationale_templates_and_suffix() -> None:
    write = approval_rationale(("write",), scopes={"paths": [], "hosts": [], "repos": []})
    assert "change something in your workspace" in write
    assert write.endswith(APPROVAL_RATIONALE_SUFFIX)

    execute = approval_rationale(("execute",), command=["echo", "hi"])
    assert "run a command on this machine" in execute
    assert execute.endswith(APPROVAL_RATIONALE_SUFFIX)

    network = approval_rationale(
        ("network",), scopes={"hosts": ["api.tavily.com"], "paths": [], "repos": []}
    )
    assert "send data to api.tavily.com" in network


def test_approval_card_shape_uses_argv_and_no_cost() -> None:
    card = approval_card(
        approval_id="apr_1234",
        source="openshell",
        thread_id="ses_1",
        turn_id="turn_1",
        call_id="call_1",
        tool="shell.exec",
        effects=("execute",),
        command=["echo", "hi"],
        arguments={"command": ["echo", "hi"]},
        requested_at="2026-10-29T09:00:00+00:00",
        expires_at="2026-10-29T09:10:00+00:00",
    )
    assert card["command"] == ["echo", "hi"]
    assert isinstance(card["command"], list)
    assert card["cost"] == {"amount_usd": None, "known": False}
    assert card["ttl_seconds"] == 600
    assert card["single_use"] is True
    assert card["source"] == "openshell"
    assert card["status"] == "pending"


def test_sweep_items_for_issue_and_repo() -> None:
    issue = {
        "ok": True,
        "output": {
            "kind": "issue",
            "issue": {
                "number": 4,
                "title": "Fix",
                "state": "open",
                "html_url": "https://github.com/a/b/issues/4",
            },
        },
    }
    assert sweep_items("issue", issue) == [
        {"number": 4, "title": "Fix", "state": "open", "url": "https://github.com/a/b/issues/4"}
    ]
    repo = {
        "ok": True,
        "output": {
            "kind": "repo",
            "repo": {
                "full_name": "a/b",
                "html_url": "https://github.com/a/b",
                "open_issues_count": 9,
            },
        },
    }
    assert sweep_items("repo", repo)[0]["title"] == "a/b"


def _issue_result(number: int) -> dict[str, Any]:
    return {
        "ok": True,
        "output": {
            "kind": "issue",
            "issue": {
                "number": number,
                "title": f"Issue {number}",
                "state": "open",
                "html_url": f"https://github.com/a/b/issues/{number}",
            },
        },
    }


def _github_block(call_id: str, number: int, repo: str = "a/b", kind: str = "issue") -> dict[str, Any]:
    return {
        "type": "tool_call",
        "call_id": call_id,
        "tool": "github.read",
        "status": "done",
        "detail": {
            "arguments": {"repo": repo, "kind": kind},
            "result": _issue_result(number),
        },
    }


def test_apply_sweeps_merges_a_contiguous_run() -> None:
    blocks = [_github_block("call_a", 1), _github_block("call_b", 2)]
    apply_sweeps(blocks)
    sweep = blocks[0]["detail"]["sweep"]
    assert sweep["count"] == 2
    assert [item["number"] for item in sweep["items"]] == [1, 2]
    assert sweep["truncated"] is False
    assert blocks[1]["detail"]["sweep_anchor"] == "call_a"
    assert "sweep" not in blocks[1]["detail"]


def test_apply_sweeps_breaks_on_a_different_repo_and_truncates() -> None:
    blocks = [_github_block(f"call_{i}", i) for i in range(6)]
    blocks.append(_github_block("call_other", 99, repo="other/repo"))
    apply_sweeps(blocks)
    sweep = blocks[0]["detail"]["sweep"]
    assert sweep["count"] == 6
    assert len(sweep["items"]) == 5
    assert sweep["truncated"] is True
    assert blocks[-1]["detail"]["sweep"]["repo"] == "other/repo"


def test_error_copy_has_a_catch_all() -> None:
    assert error_copy("provider_timeout") == "The model took too long to answer."
    assert error_copy("brand_new_code") == "Something went wrong."
    assert error_copy("invalid_tool_output") == error_copy("invalid_output")
