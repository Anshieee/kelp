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
import time
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
from demo.surface import Surface
from openshell.audit import AuditLog

__all__ = ["Beat", "Demo", "TURN_SCRIPTS", "build_scripts"]

TOTAL_BUDGET_SECONDS = 240.0
GITHUB_REPO = "Anshieee/nebius-nvidia"

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
    # 6 — the resumed turn, after the kill. Plain text; no tools.
    [
        text_script(
            "Resumed after the restart: the thread kept every event, the interrupted "
            "answer is still marked interrupted, and this turn is new."
        )
    ],
]


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

    def _audit_turn(self, thread_id: str, turn_id: str, since_seq: int) -> int:
        """Mirror approval decisions and write executions into the audit log."""

        written = 0
        for event in self.surface.events(thread_id):
            if event["seq"] <= since_seq:
                continue
            if event["type"] == "approval_resolved":
                card = self.store.get_approval(str(event["approval_id"])) or {}
                status = str(event.get("status"))
                self.audit.record(
                    "approval_decision" if status in {"approved", "denied"} else "approval_settled",
                    turn_id=turn_id,
                    request_id=str(event["approval_id"]),
                    tool=card.get("tool"),
                    actor="owner",
                    decision=status,
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
        turn_id, blocks = self._send_and_gate(
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

        written = self._audit_turn(thread_id, turn_id, since)
        self.check(beat, written >= 3, "decision, settlement, and execution reached the audit log")
        entries = self.audit.entries(turn_id=turn_id)
        decisions = {entry.get("decision") for entry in entries if entry["event"] == "approval_decision"}
        self.check(beat, "approved" in decisions, "audit log shows the approval")
        settlements = {entry.get("decision") for entry in entries if entry["event"] == "approval_settled"}
        self.check(beat, "consumed" in settlements, "audit log shows the single use")
        self.check(
            beat,
            any(entry["event"] == "tool_executed" and entry.get("outcome") == "ok" for entry in entries),
            "audit log shows the execution",
        )
        self.check(
            beat,
            all("prompt" not in json.dumps(entry) for entry in entries),
            "audit entries carry no task payloads or secrets",
        )

        beat.data.update(
            {
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
        task = self._send_and_gate(
            beat,
            thread_id,
            "Every weekday at 8:30, summarize my open issues.",
            client_turn_id="demo-5",
            tool="schedule.create",
            decision="approve",
        )[1]
        task_id = self._block(task, "schedule.create")["detail"]["result"]["output"]["task_id"]

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

        beat.data.update(
            {
                "thread_id": thread_id,
                "task_id": task_id,
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

        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "demo.server",
                "--state",
                str(self.paths.state),
                "--thread",
                thread_id,
                "--text",
                "Write a long status report about the Nebius demo while I kill this server.",
                "--client-turn-id",
                "demo-6",
            ],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            ready_line = child.stdout.readline()
            if not ready_line:
                stderr = child.stderr.read() if child.stderr else ""
                raise DemoError(beat.number, f"child server never became ready: {stderr[-400:]}")
            ready = json.loads(ready_line)
            turn_id = str(ready["turn_id"])
            self.check(beat, ready.get("port", 0) > 0, "child server bound a real socket")

            try:
                _wait_for(lambda: _count_events(self.paths.db, thread_id, "token_delta") >= 3, 15.0)
            except TimeoutError:
                raise DemoError(beat.number, "the child never streamed enough tokens to kill") from None
            if child.poll() is not None:  # pragma: no cover - timing guard
                raise DemoError(beat.number, "child finished before it could be killed")
            os.kill(child.pid, signal.SIGKILL)
        finally:
            child.wait(timeout=10)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

        self.check(beat, child.returncode == -signal.SIGKILL, f"server was killed (exit {child.returncode})")

        before = self.surface.events(thread_id)
        prefix = [(event["seq"], event["type"]) for event in before]
        partial = self._assistant_text(self.surface.snapshot(thread_id), turn_id)
        self.check(beat, 0 < len(partial) < 400, "the answer was mid-stream when the server died")
        self.check(
            beat,
            any(event["type"] == "token_delta" for event in before),
            "streamed tokens were on disk before the kill",
        )

        # Restart: a new store handle and a new service over the same files.
        closed = self.service.recover_interrupted()
        self.check(beat, closed == [turn_id], "the interrupted turn was closed on restart")
        turn = self.store.get_turn(turn_id) or {}
        self.check(beat, turn.get("reason") == "interrupted", "turn marked interrupted")

        after = self.surface.events(thread_id)
        self.check(
            beat,
            [(event["seq"], event["type"]) for event in after[: len(prefix)]] == prefix,
            "every event written before the kill survived",
        )
        self.check(beat, len(after) == len(prefix) + 1, "restart appended exactly one event")
        self.check(beat, after[-1]["type"] == "turn_finished", "the closing event is turn_finished")
        self.check(
            beat,
            self._assistant_text(self.surface.snapshot(thread_id), turn_id) == partial,
            "partial answer is preserved verbatim",
        )

        # Resume the thread: one new turn, no duplicate user message.
        resumed = self.surface.json("POST", f"/api/turns/{turn_id}/retry", expect=(202,))
        new_turn = resumed["turn_id"]
        self.surface.wait_for_turn(new_turn, timeout=20.0)
        snapshot = self.surface.snapshot(thread_id)
        user_messages = [m for m in snapshot["messages"] if m["role"] == "user"]
        self.check(beat, len(user_messages) == 1, "no duplicate user message after resume")
        turns = {m["turn_id"] for m in snapshot["messages"]}
        self.check(beat, len(turns) == 2, "exactly two turns: interrupted and resumed")
        resumed_turn = self.store.get_turn(new_turn) or {}
        self.check(beat, resumed_turn.get("retry_of") == turn_id, "resumed turn points at the killed one")
        self.check(beat, resumed_turn.get("reason") == "stop", "resumed turn finished normally")
        final = self._assistant_text(snapshot, new_turn)
        self.check(beat, final != partial and len(final) > 0, "resumed answer is new content")

        # An idempotent resend must not open a third turn.
        again = self.surface.send(
            thread_id,
            "Write a long status report about the Nebius demo while I kill this server.",
            client_turn_id="demo-6",
            expect=(200,),
        )
        self.check(beat, again.get("idempotent") is True, "resend was recognised as a replay")
        self.check(beat, again["turn_id"] == turn_id, "resend returned the original turn")
        self.check(
            beat,
            len({m["turn_id"] for m in self.surface.snapshot(thread_id)["messages"]}) == 2,
            "replay created no third turn",
        )

        # Wire replay: reconnect with Last-Event-ID and receive a gapless log.
        last = self.store.last_seq(thread_id)
        frames = list(self.surface.stream(thread_id, after=0, max_events=last + 1))
        seqs = [frame[0] for frame in frames]
        self.check(beat, seqs == list(range(1, last + 1)), "SSE replay is gapless")
        self.check(beat, frames[-1][1] == "turn_finished", "stream ends on turn_finished")

        beat.data.update(
            {
                "killed_turn": turn_id,
                "resumed_turn": new_turn,
                "events_before_kill": len(prefix),
                "partial_chars": len(partial),
                "final_chars": len(final),
                "last_seq": last,
            }
        )

    def _assistant_text(self, snapshot: Mapping[str, Any], turn_id: str) -> str:
        for message in snapshot["messages"]:
            if message["turn_id"] == turn_id and message["role"] == "assistant":
                return "".join(
                    str(block.get("text") or "")
                    for block in message["blocks"]
                    if block.get("type") == "text"
                )
        return ""


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


def _wait_for(predicate: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("condition was not met before the timeout")
