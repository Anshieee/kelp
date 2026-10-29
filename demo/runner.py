"""The six beats, driven through the real chat API against recorded fixtures.

Each beat is a method returning a :class:`Beat` receipt. A failed check raises
:class:`demo.errors.DemoError`, which the CLI turns into exit status 1 — the
demo doubles as the regression harness for the prototype story.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from chat.api import ChatAPI
from chat.artifacts import ArtifactRegistry
from chat.store import ChatStore
from demo.config import AUTH_TOKEN, MODEL, REPO_ROOT, DemoPaths
from demo.errors import DemoError
from demo.fixtures import assets
from demo.fixtures.provider import FixtureProvider, text_script, tool_script
from demo.fixtures.registry import build_registry
from demo.handlers.schedule_create import list_tasks
from demo.surface import RemoteSurface, Surface
from openshell.audit import AuditLog

__all__ = ["Beat", "Demo", "PLANTED_SECRET", "RESUME_SCRIPT", "TURN_SCRIPTS", "build_scripts"]

TOTAL_BUDGET_SECONDS = 240.0
GITHUB_REPO = "Anshieee/nebius-nvidia"

# A fake provider key planted in an audited field. The audit log must persist
# ``[redacted]`` instead of this value; beat 4 asserts that against the file.
PLANTED_SECRET = "sk_live_kel9planted0000"


TURN_SCRIPTS: list[list[list[Any]]] = [
    # 1 — web search, then an answer that cites the chips.
    [
        tool_script(
            "web.search",
            {"query": "Nebius GPU pricing H200 L40S", "max_results": 3},
            call_id="call_search",
        ),
        text_script(
            "Nebius lists the H200 at $2.49 and the L40S at $1.19 per GPU-hour, "
            "with reserved capacity billed monthly [1][3]."
        ),
    ],
    # 2a — summarise the attached PDF.
    [
        tool_script(
            "docs.answer",
            {"artifact": "{{pdf}}", "kind": "pdf", "question": "Summarize this PDF"},
            call_id="call_pdf",
        ),
        text_script("Here is the PDF summary, with the page it came from."),
    ],
    # 2b — answer a numeric question from the attached CSV.
    [
        tool_script(
            "docs.answer",
            {"artifact": "{{csv}}", "kind": "csv", "question": "What was the total revenue?"},
            call_id="call_csv",
        ),
        text_script("The spreadsheet total is above, with the rows it came from."),
    ],
    # 3 — read-only GitHub lookup; no approval prompt.
    [
        tool_script(
            "github.read",
            {"kind": "issue", "repo": GITHUB_REPO, "number": 12},
            call_id="call_issue",
        ),
        text_script("Issue 12 is open: a killed turn must resume without duplicates."),
    ],
    # 4a — a write the owner declines. The gate must fail closed.
    [
        tool_script(
            "schedule.create",
            {
                "title": "Declined capacity digest",
                "prompt": "Summarize H200 capacity every weekday morning.",
                "schedule": {"kind": "cron", "expr": "0 7 * * 1-5"},
                "timezone": "UTC",
            },
            call_id="call_denied",
        ),
        text_script("Nothing was scheduled: the approval was denied."),
    ],
    # 4b — the same write, approved, followed by the audit entry.
    [
        tool_script(
            "schedule.create",
            {
                "title": "Weekly capacity digest",
                "prompt": "Summarize H200 capacity for the week.",
                "schedule": {"kind": "cron", "expr": "0 9 * * 1"},
                "timezone": "UTC",
            },
            call_id="call_approved",
        ),
        text_script("Scheduled. The audit log records both the decision and the run."),
    ],
    # 5 — a recurring task the scheduler will list.
    [
        tool_script(
            "schedule.create",
            {
                "title": "Weekday open-issue summary",
                "prompt": "Summarize my open issues.",
                "schedule": {"kind": "cron", "expr": "30 8 * * 1-5"},
                "timezone": "UTC",
            },
            call_id="call_weekday",
        ),
        text_script("The weekday task is on the scheduler list with its next run time."),
    ],
]

# 6 — the resumed turn after the kill. Served by the restarted process
#     (``demo.server --recover``), not by this process's provider; it stays in
#     the table so both halves of beat 6 share one scripted answer.
RESUME_SCRIPT: list[Any] = [
    text_script(
        "Resumed after the restart: the thread kept every event, the interrupted "
        "answer is still marked interrupted, and this turn is new."
    )
]

TURN_SCRIPTS.append(RESUME_SCRIPT)


def build_scripts() -> list[list[list[Any]]]:
    """The script table the fixture provider replays, in turn order."""

    return TURN_SCRIPTS


@dataclass
class Beat:
    number: int
    name: str
    duration_ms: int = 0
    checks: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return f"{len(self.checks)} checks"


class _Drain:
    """Keep a child's stderr from filling its pipe; keep the tail for errors."""

    def __init__(self, pipe: Any, *, keep: int = 200):
        self._lines: deque[str] = deque(maxlen=keep)
        self._thread = threading.Thread(target=self._run, args=(pipe,), daemon=True)
        self._thread.start()

    def _run(self, pipe: Any) -> None:
        try:
            for line in pipe:
                self._lines.append(line)
        except (ValueError, OSError):  # pragma: no cover - pipe closed under us
            pass
        finally:
            try:
                pipe.close()
            except OSError:  # pragma: no cover - already closed
                pass

    def tail(self, lines: int = 12) -> str:
        return "".join(self._lines[-lines:])

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout=timeout)


class Demo:
    def __init__(
        self,
        paths: DemoPaths,
        *,
        console: Callable[[str], None] = print,
    ):
        self.paths = paths
        self.console = console
        self.store = ChatStore(paths.db)
        self.registry = build_registry(paths.skills)
        self.artifacts = ArtifactRegistry(self.store, paths.artifacts)
        self.substitutions: dict[str, str] = {}
        self.provider = FixtureProvider(build_scripts(), substitutions=self.substitutions)
        self.service = self._service(self.provider)
        self.api = ChatAPI(self.service, auth_token=AUTH_TOKEN, stream_idle=0.2)
        self.surface = Surface(self.api)
        self.audit = AuditLog(paths.audit)
        self.beats: list[Beat] = []
        self._drains: dict[int, _Drain] = {}

    def _service(self, provider: Any) -> Any:
        from chat.service import ChatService

        return ChatService(
            store=self.store,
            provider=provider,
            registry=self.registry,
            artifacts=self.artifacts,
            model=MODEL,
            workspace_root=str(self.paths.workspace),
            tools=(),
        )

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> list[Beat]:
        steps: Sequence[tuple[str, Callable[[Beat], None]]] = (
            ("ask + sources", self._beat_search),
            ("PDF + CSV artifacts", self._beat_artifacts),
            ("GitHub read", self._beat_github),
            ("approve a write", self._beat_approval),
            ("scheduled task", self._beat_schedule),
            ("kill, restart, resume", self._beat_resume),
        )
        started = time.monotonic()
        failure: DemoError | None = None
        try:
            for number, (name, step) in enumerate(steps, start=1):
                beat = Beat(number=number, name=name)
                mark = time.monotonic()
                self.console(f"beat {number}  {name:<24}")
                step(beat)
                beat.duration_ms = int((time.monotonic() - mark) * 1000)
                self._write_receipt(beat)
                self.beats.append(beat)
                self.console(
                    f"         ok, {beat.duration_ms} ms, {beat.summary()}"
                )
            elapsed = time.monotonic() - started
            if elapsed > TOTAL_BUDGET_SECONDS:
                raise DemoError(0, f"run took {elapsed:.1f}s; budget is {TOTAL_BUDGET_SECONDS:.0f}s")
            self._write_report(elapsed)
            return self.beats
        except DemoError as exc:
            failure = exc
            self._write_report(time.monotonic() - started, failure=exc)
            raise
        finally:
            self.close()

    def close(self) -> None:
        try:
            self.api.close()
        finally:
            self.store.close()

    # -- helpers -----------------------------------------------------------

    def check(self, beat: Beat, condition: Any, message: str) -> None:
        if not condition:
            raise DemoError(beat.number, message)
        beat.checks.append(message)

    def _write_receipt(self, beat: Beat) -> None:
        payload = {
            "beat": beat.number,
            "name": beat.name,
            "ok": True,
            "duration_ms": beat.duration_ms,
            "checks": beat.checks,
            "data": beat.data,
        }
        path = self.paths.receipts / f"beat-{beat.number}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _write_report(self, elapsed: float, *, failure: DemoError | None = None) -> None:
        lines = [
            "# KEL-9 demo report",
            "",
            f"- state: `{self.paths.state}`",
            f"- model: `{MODEL}` (recorded fixture provider — no network, no keys)",
            f"- total: {elapsed:.2f}s of a {TOTAL_BUDGET_SECONDS:.0f}s budget",
        ]
        if failure is not None:
            lines.append(f"- **failed:** beat {failure.beat}: {failure.message}")
        lines += [
            "",
            "| beat | name | duration | checks |",
            "|---|---|---|---|",
        ]
        for beat in self.beats:
            lines.append(
                f"| {beat.number} | {beat.name} | {beat.duration_ms} ms | {len(beat.checks)} |"
            )
        lines += ["", "## Checks", ""]
        for beat in self.beats:
            lines.append(f"### {beat.number}. {beat.name}")
            lines.extend(f"- {check}" for check in beat.checks)
            lines.append("")
        self.paths.report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _turn_blocks(self, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        snapshot = self.surface.snapshot(thread_id)
        for message in snapshot["messages"]:
            if message["turn_id"] == turn_id and message["role"] == "assistant":
                return list(message["blocks"])
        raise AssertionError(f"no assistant message for turn {turn_id}")

    def _block(self, blocks: Sequence[Mapping[str, Any]], tool: str) -> Mapping[str, Any]:
        for block in reversed(blocks):
            if block.get("type") == "tool_call" and block.get("tool") == tool:
                return block
        raise AssertionError(f"no {tool} block")

    def _event_types(self, thread_id: str) -> list[str]:
        return [event["type"] for event in self.surface.events(thread_id)]

    # -- child servers (beat 6) --------------------------------------------

    def _spawn_server(
        self,
        beat: Beat,
        *,
        thread_id: str | None = None,
        text: str | None = None,
        client_turn_id: str | None = None,
        recover: bool = False,
    ) -> tuple[subprocess.Popen[str], dict[str, Any]]:
        """Start a real server child and wait for its one-line JSON ready payload."""

        command = [
            sys.executable,
            "-m",
            "demo.server",
            "--state",
            str(self.paths.state),
        ]
        if recover:
            command.append("--recover")
        else:
            if not (thread_id and text and client_turn_id):  # pragma: no cover - caller supplies
                raise DemoError(beat.number, "turn-mode server needs a thread, text, and client turn id")
            command += [
                "--thread",
                thread_id,
                "--text",
                text,
                "--client-turn-id",
                client_turn_id,
            ]
        child = subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        drain = _Drain(child.stderr)
        self._drains[id(child)] = drain
        line = child.stdout.readline() if child.stdout is not None else ""
        if not line:
            self._stop_child(child)
            raise DemoError(
                beat.number,
                f"child server never became ready: {drain.tail()[-400:].strip()}",
            )
        try:
            ready = json.loads(line)
        except json.JSONDecodeError as exc:
            self._stop_child(child)
            raise DemoError(
                beat.number, f"child ready line was not JSON: {line[:200]!r}"
            ) from exc
        if not isinstance(ready, dict) or ready.get("ready") is not True:
            self._stop_child(child)
            raise DemoError(beat.number, f"child ready line was not a ready payload: {line[:200]!r}")
        if not isinstance(ready.get("port"), int) or int(ready["port"]) <= 0:
            self._stop_child(child)
            raise DemoError(beat.number, f"child server reported no port: {ready!r}")
        return child, ready

    def _stop_child(self, child: subprocess.Popen[str], *, timeout: float = 10.0) -> None:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - unresponsive child
            child.kill()
            child.wait(timeout=timeout)
        if child.stdout is not None:
            child.stdout.close()
        drain = self._drains.pop(id(child), None)
        if drain is not None:
            drain.join()

    def _wait_for_approval(
        self, beat: Beat, thread_id: str, turn_id: str, tool: str, timeout: float = 10.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.surface.json(
                "GET",
                "/api/approvals",
                query={"thread_id": [thread_id], "status": ["pending"]},
            )
            for card in response["approvals"]:
                if card.get("turn_id") == turn_id and card.get("tool") == tool:
                    return card
            time.sleep(0.01)
        raise DemoError(beat.number, f"no approval appeared for {tool} within {timeout:.0f}s")

    def _run_turn(
        self,
        beat: Beat,
        thread_id: str,
        text: str,
        *,
        client_turn_id: str,
        attachment_handles: Sequence[str] = (),
    ) -> tuple[str, list[dict[str, Any]]]:
        payload = self.surface.send(
            thread_id,
            text,
            client_turn_id=client_turn_id,
            attachment_handles=attachment_handles,
        )
        turn_id = payload["turn_id"]
        self.surface.wait_for_turn(turn_id, timeout=20.0)
        turn = self.store.get_turn(turn_id)
        if turn is None:  # pragma: no cover - send just created it
            raise DemoError(beat.number, f"turn {turn_id} disappeared")
        return turn_id, self._turn_blocks(thread_id, turn_id)

    def _record_decision(
        self,
        beat: Beat,
        *,
        turn_id: str,
        card: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Audit the owner's decision at the moment it is made.

        The entry is written before the turn is waited on, so a decision can
        never be a post-hoc reconstruction. The ``operator_note`` deliberately
        carries a fake provider key: the log's scrubber must redact it.
        """

        status = str((result.get("approval") or {}).get("status") or "")
        outcome = "not_executed" if status == "denied" else "awaiting_execution"
        entry = self.audit.record(
            "approval_decision",
            turn_id=turn_id,
            request_id=str(card["id"]),
            tool=str(card.get("tool") or ""),
            actor="owner",
            decision=status,
            outcome=outcome,
            extra={
                "single_use": bool(card.get("single_use")),
                "operator_note": f"owner decision recorded; provider key {PLANTED_SECRET}",
            },
        )
        self.check(beat, entry.get("decision") == status, f"{card['id']}: decision value recorded")
        self.check(
            beat,
            PLANTED_SECRET not in json.dumps(entry) and "[redacted]" in json.dumps(entry),
            f"{card['id']}: audited note is secret-scrubbed",
        )
        self.check(
            beat,
            bool(self.audit.entries(turn_id=turn_id, event="approval_decision")),
            f"{card['id']}: decision was in the log before the turn finished",
        )
        return entry

    def _audit_outcomes(self, thread_id: str, turn_id: str, since_seq: int) -> int:
        """Audit what the decision led to: single use and run, never the decision."""

        written = 0
        for event in self.surface.events(thread_id):
            if event["seq"] <= since_seq:
                continue
            if event["type"] == "approval_resolved" and event.get("status") == "consumed":
                card = self.store.get_approval(str(event["approval_id"])) or {}
                self.audit.record(
                    "approval_settled",
                    turn_id=turn_id,
                    request_id=str(event["approval_id"]),
                    tool=card.get("tool"),
                    actor="owner",
                    decision="consumed",
                    outcome="executed" if event.get("executed") else "not_executed",
                )
                written += 1
            elif event["type"] == "tool_call_finished" and event.get("tool") == "schedule.create":
                self.audit.record(
                    "tool_executed",
                    turn_id=turn_id,
                    tool="schedule.create",
                    actor="owner",
                    outcome="ok" if event.get("ok") else "failed",
                    duration_ms=event.get("duration_ms"),
                )
                written += 1
        return written

    # -- beat 1 ------------------------------------------------------------

    def _beat_search(self, beat: Beat) -> None:
        thread = self.surface.create_thread("KEL-9 demo")
        beat.data["thread_id"] = thread["id"]
        turn_id, blocks = self._run_turn(
            beat,
            thread["id"],
            "What is the latest Nebius GPU pricing? Cite your sources.",
            client_turn_id="demo-1",
        )
        turn = self.store.get_turn(turn_id) or {}
        self.check(beat, turn.get("state") == "finished", "turn finished")
        self.check(beat, turn.get("reason") == "stop", "turn stopped normally")

        snapshot = self.surface.snapshot(thread["id"])
        assistant = next(m for m in snapshot["messages"] if m["turn_id"] == turn_id and m["role"] == "assistant")
        self.check(beat, assistant["status"] == "complete", "answer message is complete")
        self.check(beat, assistant["sources_total"] >= 2, "at least two source chips")

        search = self._block(blocks, "web.search")
        self.check(beat, search["status"] == "done", "web.search succeeded")
        self.check(
            beat,
            search["detail"]["result_summary"] == "3 results",
            "web.search returned three results",
        )
        urls = {source.get("url") for source in assistant["sources"]}
        self.check(
            beat,
            "https://nebius.com/prices" in urls,
            "source chips carry the recorded URLs",
        )
        self.check(
            beat,
            "sources_ready" in self._event_types(thread["id"]),
            "sources_ready reached the stream",
        )
        text = "".join(
            str(block.get("text") or "") for block in blocks if block.get("type") == "text"
        )
        self.check(beat, "$2.49" in text, "answer cites the price")

        beat.data.update(
            {
                "turn_id": turn_id,
                "sources_total": assistant["sources_total"],
                "urls": sorted(url for url in urls if url),
            }
        )

    # -- beat 2 ------------------------------------------------------------

    def _beat_artifacts(self, beat: Beat) -> None:
        thread = self.surface.create_thread("Documents")
        thread_id = thread["id"]
        beat.data["thread_id"] = thread_id

        pdf = self.surface.upload("nebius-overview.pdf", assets.pdf_bytes())
        csv = self.surface.upload("q3-revenue.csv", assets.csv_bytes())
        self.substitutions["pdf"] = pdf["handle"]
        self.substitutions["csv"] = csv["handle"]
        self.check(beat, pdf["kind"] == "pdf", "PDF upload registered as a PDF")
        self.check(beat, pdf["pages"] == 2, "PDF page count read from the file")
        self.check(beat, csv["kind"] == "csv", "CSV upload registered as a CSV")
        self.check(beat, csv["rows"] == 3, "CSV row count read from the file")

        # 2a — summarise the PDF.
        pdf_turn, pdf_blocks = self._run_turn(
            beat,
            thread_id,
            "Summarize this PDF for me.",
            client_turn_id="demo-2a",
            attachment_handles=[pdf["handle"]],
        )
        self.check(beat, pdf_turn is not None, "PDF turn finished")
        self.check(
            beat,
            (self.store.get_turn(pdf_turn) or {}).get("reason") == "stop",
            "PDF turn stopped normally",
        )
        pdf_answer = self._block(pdf_blocks, "docs.answer")["detail"]["result"]["output"]
        self.check(beat, pdf_answer["artifact"]["pages"] == 2, "answer reports both pages")
        self.check(beat, pdf_answer["citations"][0]["locator"].startswith("page "), "PDF citation names a page")
        artifact_blocks = [b for b in pdf_blocks if b.get("type") == "artifact"]
        self.check(beat, len(artifact_blocks) == 1, "PDF artifact card rendered")
        card = artifact_blocks[0]["artifact"]
        self.check(beat, card["kind"] == "pdf", "card is a PDF")
        self.check(
            beat,
            card["open_url"].endswith(f"thread_id={thread_id}"),
            f"card URL is thread-scoped ({card['open_url']})",
        )

        content = self.surface.raw(
            "GET", f"/api/artifacts/{pdf['handle']}/content", query={"thread_id": [thread_id]}
        )
        self.check(beat, content.body.startswith(b"%PDF-"), "PDF bytes served intact")
        self.check(
            beat,
            content.headers.get("Content-Type") == "application/pdf",
            "PDF content type is correct",
        )

        # 2b — numeric answer from the CSV, plus the table preview.
        csv_turn, csv_blocks = self._run_turn(
            beat,
            thread_id,
            "What was the total revenue in the CSV?",
            client_turn_id="demo-2b",
            attachment_handles=[csv["handle"]],
        )
        self.check(beat, csv_turn is not None, "CSV turn finished")
        self.check(
            beat,
            (self.store.get_turn(csv_turn) or {}).get("reason") == "stop",
            "CSV turn stopped normally",
        )
        csv_answer = self._block(csv_blocks, "docs.answer")["detail"]["result"]["output"]
        self.check(
            beat,
            "1,284,500" in csv_answer["answer"],
            "CSV answer is the computed total",
        )
        self.check(beat, csv_answer["artifact"]["rows"] == 3, "CSV answer reports three rows")

        preview = self.surface.json(
            "GET",
            f"/api/artifacts/{csv['handle']}/preview",
            query={"thread_id": [thread_id], "rows": ["3"]},
        )
        self.check(beat, preview["columns"][0] == "region", "preview columns come from the header")
        self.check(beat, preview["total_rows"] == 3, "preview counts every row")
        self.check(
            beat,
            len(preview["rows"]) == 3 and preview["rows"][0][0] == "emea",
            "preview shows the data rows",
        )
        csv_content = self.surface.raw(
            "GET", f"/api/artifacts/{csv['handle']}/content", query={"thread_id": [thread_id]}
        )
        self.check(
            beat,
            csv_content.headers.get("Content-Type", "").startswith("text/csv"),
            "CSV content type is correct",
        )

        beat.data.update(
            {
                "pdf": pdf["handle"],
                "csv": csv["handle"],
                "pdf_answer": pdf_answer["answer"],
                "csv_answer": csv_answer["answer"],
                "pdf_pages": 2,
                "csv_rows": 3,
            }
        )

    # -- beat 3 ------------------------------------------------------------

    def _beat_github(self, beat: Beat) -> None:
        thread = self.surface.create_thread("GitHub lookup")
        thread_id = thread["id"]
        beat.data["thread_id"] = thread_id
        turn_id, blocks = self._run_turn(
            beat,
            thread_id,
            "What is the status of issue 12 in Anshieee/nebius-nvidia?",
            client_turn_id="demo-3",
        )
        read = self._block(blocks, "github.read")
        self.check(beat, read["status"] == "done", "github.read succeeded")
        issue = read["detail"]["result"]["output"]["issue"]
        self.check(beat, issue["number"] == 12, "fixture issue 12 returned")
        self.check(beat, issue["state"] == "open", "issue state read")
        sweep = read["detail"].get("sweep") or {}
        self.check(beat, sweep.get("repo") == GITHUB_REPO, "sweep attached to the anchor")
        self.check(beat, sweep.get("count") == 1, "sweep lists one item")

        events = self.surface.events(thread_id)
        approvals = [event for event in events if event["type"] == "approval_requested"]
        self.check(beat, not approvals, "read-only tool asked for no approval")
        snapshot = self.surface.snapshot(thread_id)
        assistant = next(
            message
            for message in snapshot["messages"]
            if message["turn_id"] == turn_id and message["role"] == "assistant"
        )
        self.check(
            beat,
            "https://github.com/Anshieee/nebius-nvidia/issues/12"
            in {source.get("url") for source in assistant["sources"]},
            "issue URL became a source chip",
        )
        effects = [e for e in events if e["type"] == "tool_call_started"][0]["effects"]
        self.check(beat, list(effects) == ["read", "network"], "declared effects are read-only")

        beat.data.update(
            {"turn_id": turn_id, "issue": 12, "state": issue["state"], "repo": GITHUB_REPO}
        )

    # -- beat 4 ------------------------------------------------------------

    def _beat_approval(self, beat: Beat) -> None:
        thread = self.surface.create_thread("Approvals")
        thread_id = thread["id"]
        beat.data["thread_id"] = thread_id
        tasks_before = list_tasks(self.paths.workspace)

        # 4a — deny. The gate must fail closed and nothing may be written.
        denied_turn, blocks = self._send_and_gate(
            beat,
            thread_id,
            "Every weekday at 7, summarize H200 capacity for me.",
            client_turn_id="demo-4a",
            tool="schedule.create",
            decision="deny",
        )
        denied = self._block(blocks, "schedule.create")
        self.check(beat, denied["status"] == "blocked", "denied write is blocked")
        error = denied["detail"].get("error") or {}
        self.check(beat, error.get("code") == "approval_denied", "deny failed closed")
        self.check(
            beat,
            list_tasks(self.paths.workspace) == tasks_before,
            "nothing was written after the denial",
        )
        denied_entries = self.audit.entries(turn_id=denied_turn, event="approval_decision")
        self.check(beat, len(denied_entries) == 1, "the denial is in the audit log too")
        self.check(
            beat,
            denied_entries[0].get("decision") == "denied"
            and denied_entries[0].get("outcome") == "not_executed",
            "the denial is recorded as not executed",
        )

        # 4b — approve. The write runs exactly once and is audited.
        since = self.store.last_seq(thread_id)
        turn_id, blocks = self._send_and_gate(
            beat,
            thread_id,
            "Create a weekly capacity digest for Monday at 9.",
            client_turn_id="demo-4b",
            tool="schedule.create",
            decision="approve",
        )
        approved = self._block(blocks, "schedule.create")
        self.check(beat, approved["status"] == "done", "approved write ran")
        task = approved["detail"]["result"]["output"]
        self.check(beat, task["status"] == "scheduled", "handler reported a scheduled task")
        self.check(
            beat,
            any(item["task_id"] == task["task_id"] for item in list_tasks(self.paths.workspace)),
            "task is in the scheduler store",
        )

        written = self._audit_outcomes(thread_id, turn_id, since)
        self.check(beat, written >= 2, "the single use and the execution reached the audit log")
        entries = self.audit.entries(turn_id=turn_id)
        decisions = {entry.get("decision") for entry in entries if entry["event"] == "approval_decision"}
        self.check(beat, "approved" in decisions, "audit log shows the approval at decision time")
        settlements = {entry.get("decision") for entry in entries if entry["event"] == "approval_settled"}
        self.check(beat, "consumed" in settlements, "audit log shows the single use")
        self.check(
            beat,
            any(entry["event"] == "tool_executed" and entry.get("outcome") == "ok" for entry in entries),
            "audit log shows the execution",
        )

        # The planted key went into an audited field; only the redaction may land.
        raw = self.paths.audit.read_text(encoding="utf-8")
        self.check(beat, PLANTED_SECRET not in raw, "the planted secret never reached the audit file")
        self.check(beat, "[redacted]" in raw, "the audited note was redacted in place")
        self.check(
            beat,
            all(
                "prompt" not in entry and "Summarize H200 capacity" not in json.dumps(entry)
                for entry in entries
            ),
            "audit entries carry no task payloads",
        )

        beat.data.update(
            {
                "denied_turn": denied_turn,
                "approved_turn": turn_id,
                "approved_task": task["task_id"],
                "audit_entries": len(entries),
                "audit_log": str(self.paths.audit),
            }
        )

    def _send_and_gate(
        self,
        beat: Beat,
        thread_id: str,
        text: str,
        *,
        client_turn_id: str,
        tool: str,
        decision: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        payload = self.surface.send(thread_id, text, client_turn_id=client_turn_id)
        turn_id = payload["turn_id"]
        card = self._wait_for_approval(beat, thread_id, turn_id, tool)

        self.check(beat, card["status"] == "pending", f"{client_turn_id}: card starts pending")
        self.check(beat, card["single_use"] is True, f"{client_turn_id}: card is single-use")
        self.check(beat, card["source"] == "skill", f"{client_turn_id}: card came from the skill gate")
        self.check(beat, "write" in card["effects"], f"{client_turn_id}: card names the write effect")
        self.check(
            beat,
            card["scopes"]["paths"] == ["${WORKSPACE}/var/scheduler/**"],
            f"{client_turn_id}: card shows the scoped path",
        )
        self.check(beat, card["ttl_seconds"] == 600, f"{client_turn_id}: card expires in 10 minutes")

        result = self.surface.decide(card["id"], decision)
        self.check(beat, result["ok"] is True, f"{client_turn_id}: decision accepted")
        self.check(beat, result["executed"] is False, f"{client_turn_id}: decision ran nothing by itself")
        self._record_decision(beat, turn_id=turn_id, card=card, result=result)

        self.surface.wait_for_turn(turn_id, timeout=20.0)
        blocks = self._turn_blocks(thread_id, turn_id)
        approval_blocks = [b for b in blocks if b.get("type") == "approval"]
        self.check(beat, len(approval_blocks) == 1, f"{client_turn_id}: inline approval block present")
        self.check(
            beat,
            approval_blocks[0]["approval"]["status"] in {"approved", "denied", "consumed"},
            f"{client_turn_id}: block shows the decision",
        )
        return turn_id, blocks

    # -- beat 5 ------------------------------------------------------------

    def _beat_schedule(self, beat: Beat) -> None:
        thread = self.surface.create_thread("Scheduler")
        thread_id = thread["id"]
        since = self.store.last_seq(thread_id)
        turn_id, blocks = self._send_and_gate(
            beat,
            thread_id,
            "Every weekday at 8:30, summarize my open issues.",
            client_turn_id="demo-5",
            tool="schedule.create",
            decision="approve",
        )
        task_id = self._block(blocks, "schedule.create")["detail"]["result"]["output"]["task_id"]

        tasks = list_tasks(self.paths.workspace)
        entry = next((item for item in tasks if item["task_id"] == task_id), None)
        self.check(beat, entry is not None, "task appears in the scheduler list")
        assert entry is not None  # for type checkers
        self.check(beat, entry["schedule"]["expr"] == "30 8 * * 1-5", "cron expression stored")
        self.check(beat, entry["timezone"] == "UTC", "timezone stored")
        next_run = datetime.fromisoformat(entry["next_run_at"])
        self.check(beat, next_run > datetime.now(timezone.utc), "next run is in the future")
        self.check(beat, next_run.minute == 30 and next_run.hour == 8, "next run matches the cron")
        self.check(beat, next_run.weekday() < 5, "next run is a weekday")
        self.check(beat, len(tasks) >= 2, "the scheduler lists every created task")

        # The decision was written at decision time as awaiting_execution; the
        # single use and the run have to settle it, or the log keeps a dangling
        # outcome for this turn.
        written = self._audit_outcomes(thread_id, turn_id, since)
        self.check(beat, written >= 2, "the single use and the execution reached the audit log")
        entries = self.audit.entries(turn_id=turn_id)
        self.check(
            beat,
            any(
                entry["event"] == "approval_settled" and entry.get("decision") == "consumed"
                for entry in entries
            ),
            "the scheduled write was settled as consumed",
        )
        self.check(
            beat,
            any(
                entry["event"] == "tool_executed" and entry.get("outcome") == "ok"
                for entry in entries
            ),
            "the audit log shows the scheduled write executing",
        )
        decisions = {
            entry.get("decision") for entry in entries if entry["event"] == "approval_decision"
        }
        self.check(beat, "approved" in decisions, "audit log shows the approval at decision time")
        settled_ids = {
            entry.get("request_id") for entry in entries if entry["event"] == "approval_settled"
        }
        dangling = {
            entry.get("request_id")
            for entry in entries
            if entry["event"] == "approval_decision"
            and entry.get("outcome") == "awaiting_execution"
        } - settled_ids
        self.check(beat, not dangling, "no approval is left dangling as awaiting_execution")

        beat.data.update(
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "task_id": task_id,
                "audit_entries": len(entries),
                "task_count": len(tasks),
                "tasks": [
                    {
                        "task_id": item["task_id"],
                        "title": item["title"],
                        "expr": item["schedule"]["expr"],
                        "next_run_at": item["next_run_at"],
                    }
                    for item in tasks
                ],
            }
        )

    # -- beat 6 ------------------------------------------------------------

    def _beat_resume(self, beat: Beat) -> None:
        thread = self.surface.create_thread("Kill and resume")
        thread_id = thread["id"]
        beat.data["thread_id"] = thread_id
        question = "Write a long status report about the Nebius demo while I kill this server."

        # A real child process serves the turn that gets killed.
        child, ready = self._spawn_server(
            beat, thread_id=thread_id, text=question, client_turn_id="demo-6"
        )
        turn_id = str(ready["turn_id"])
        answer_chars = int(ready["answer_chars"])
        steps = int(ready["steps"])
        self.check(beat, ready.get("port", 0) > 0, "child server bound a real socket")
        try:
            try:
                _wait_for(
                    lambda: _count_events(self.paths.db, thread_id, "token_delta") >= 3,
                    15.0,
                )
            except TimeoutError:
                raise DemoError(beat.number, "the child never streamed enough tokens to kill") from None
            self.check(beat, child.poll() is None, "child was still running when the kill landed")
            self.check(
                beat,
                _turn_state(self.paths.db, turn_id) == "running",
                "the turn was still running when the kill landed",
            )
            os.kill(child.pid, signal.SIGKILL)
        finally:
            self._stop_child(child)

        self.check(
            beat,
            child.returncode == -signal.SIGKILL,
            f"server was killed (exit {child.returncode})",
        )

        prefix = _read_events(self.paths.db, thread_id)
        types_before = [event["type"] for event in prefix]
        partial = self._assistant_text(self.surface.snapshot(thread_id), turn_id)
        deltas = types_before.count("token_delta")
        last_before = int(prefix[-1]["seq"]) if prefix else 0
        self.check(beat, "turn_started" in types_before, "turn_started reached the log before the kill")
        self.check(beat, "token_delta" in types_before, "streamed tokens were on disk before the kill")
        self.check(beat, "turn_finished" not in types_before, "the turn had not finished before the kill")
        self.check(
            beat,
            0 < deltas < steps,
            f"the kill landed before the final step ({deltas} of {steps} tokens streamed)",
        )
        self.check(
            beat,
            0 < len(partial) < answer_chars,
            f"the answer was mid-stream ({len(partial)} of {answer_chars} chars)",
        )
        self.check(
            beat,
            _turn_state(self.paths.db, turn_id) == "running",
            "SIGKILL left the turn running on disk",
        )

        # Restart: a brand-new process runs the recovery boot step, then serves.
        restarted, recovered = self._spawn_server(beat, recover=True)
        try:
            self.check(
                beat,
                recovered.get("recovered") == [turn_id],
                "the new process closed the interrupted turn",
            )
            record = _turn_record(self.paths.db, turn_id)
            self.check(
                beat,
                record.get("state") == "finished",
                "the turn is finished after the restart",
            )
            self.check(
                beat,
                record.get("reason") == "interrupted",
                "the restarted process marked the turn interrupted",
            )

            # Everything below crosses the process boundary over a real socket.
            remote = RemoteSurface(int(recovered["port"]))
            snapshot = remote.snapshot(thread_id)
            interrupted = self._assistant_message(snapshot, turn_id)
            self.check(
                beat,
                interrupted is not None and interrupted["status"] == "interrupted",
                "the restarted server reports the message interrupted",
            )
            self.check(
                beat,
                interrupted is not None and self._message_text(interrupted) == partial,
                "the partial answer survived verbatim into the new process",
            )

            last = int(snapshot["last_seq"])
            frames = remote.stream(thread_id, after=0, max_events=last)
            self.check(
                beat,
                [seq for seq, _, _ in frames] == list(range(1, last + 1)),
                "SSE replay from the new process is gapless",
            )
            self.check(
                beat,
                [kind for _, kind, _ in frames] == types_before + ["turn_finished"],
                "replay preserves the pre-kill order and appends turn_finished",
            )

            # Resume over the new process's socket: one new turn, no duplicate user message.
            resumed = remote.json("POST", f"/api/turns/{turn_id}/retry", expect=(202,))
            new_turn = str(resumed["turn_id"])
            self.check(beat, resumed.get("retry_of") == turn_id, "resumed turn points at the killed one")
            try:
                _wait_for(
                    lambda: _turn_state(self.paths.db, new_turn) == "finished",
                    20.0,
                    interval=0.02,
                )
            except TimeoutError:
                raise DemoError(beat.number, "the resumed turn never finished") from None
            snapshot = remote.snapshot(thread_id)
            resumed_turn = _turn_record(self.paths.db, new_turn)
            self.check(beat, resumed_turn.get("reason") == "stop", "resumed turn finished normally")
            user_messages = [m for m in snapshot["messages"] if m["role"] == "user"]
            self.check(beat, len(user_messages) == 1, "no duplicate user message after resume")
            self.check(
                beat,
                len({m["turn_id"] for m in snapshot["messages"]}) == 2,
                "exactly two turns: interrupted and resumed",
            )
            final = self._assistant_text(snapshot, new_turn)
            self.check(beat, final != partial and len(final) > 0, "resumed answer is new content")

            # An idempotent resend must not open a third turn.
            again = remote.json(
                "POST",
                f"/api/threads/{thread_id}/turns",
                body={
                    "text": question,
                    "client_turn_id": "demo-6",
                    "attachment_handles": [],
                },
                expect=(200,),
            )
            self.check(beat, again.get("idempotent") is True, "resend was recognised as a replay")
            self.check(beat, again.get("turn_id") == turn_id, "resend returned the original turn")
            self.check(
                beat,
                len({m["turn_id"] for m in remote.snapshot(thread_id)["messages"]}) == 2,
                "replay created no third turn",
            )

            beat.data.update(
                {
                    "killed_turn": turn_id,
                    "resumed_turn": new_turn,
                    "events_before_kill": len(prefix),
                    "last_seq_before_kill": last_before,
                    "answer_chars": answer_chars,
                    "partial_chars": len(partial),
                    "final_chars": len(final),
                    "last_seq": last,
                    "recovered_by": "demo.server --recover",
                }
            )
        finally:
            self._stop_child(restarted)

    @staticmethod
    def _assistant_message(
        snapshot: Mapping[str, Any], turn_id: str
    ) -> Mapping[str, Any] | None:
        for message in snapshot["messages"]:
            if message["turn_id"] == turn_id and message["role"] == "assistant":
                return message
        return None

    @staticmethod
    def _message_text(message: Mapping[str, Any]) -> str:
        return "".join(
            str(block.get("text") or "")
            for block in message["blocks"]
            if block.get("type") == "text"
        )

    def _assistant_text(self, snapshot: Mapping[str, Any], turn_id: str) -> str:
        message = self._assistant_message(snapshot, turn_id)
        return self._message_text(message) if message is not None else ""


def _count_events(db: Path, thread_id: str, event_type: str) -> int:
    uri = f"file:{db}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM events WHERE thread_id = ? AND type = ?",
            (thread_id, event_type),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def _read_events(db: Path, thread_id: str) -> list[dict[str, Any]]:
    """Read the persisted event log from a fresh connection, not a cached handle."""

    uri = f"file:{db}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT seq, type FROM events WHERE thread_id = ? ORDER BY seq",
            (thread_id,),
        ).fetchall()
    finally:
        connection.close()
    return [{"seq": int(row["seq"]), "type": str(row["type"])} for row in rows]


def _turn_record(db: Path, turn_id: str) -> dict[str, Any]:
    uri = f"file:{db}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else {}


def _turn_state(db: Path, turn_id: str) -> str:
    return str(_turn_record(db, turn_id).get("state") or "")


def _wait_for(predicate: Callable[[], bool], timeout: float, *, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError("condition was not met before the timeout")
