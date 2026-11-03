#!/usr/bin/env python3
"""GitHub Actions worker for the isolated Kelp demo prepare/publish pipeline."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.error
import urllib.request
import uuid


API = os.environ["DEMO_API_BASE"].rstrip("/")
WORKER_TOKEN = os.environ["DEMO_WORKER_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
ACTIVE_TASK: tuple[str, str] | None = None
TEST_SECRET_ENV = re.compile(
    r"^(?:DEMO_|GITHUB_|GH_|GIT_CONFIG_|GIT_ASKPASS$|GIT_SSH_COMMAND$|SSH_AUTH_SOCK$|OPENROUTER_|ANTHROPIC_|OPENAI_|NETLIFY_|"
    r"AWS_(?:ACCESS_KEY|SECRET|SESSION|WEB_IDENTITY)|.*(?:TOKEN|API_KEY|SECRET|CREDENTIAL|PASSWORD|PRIVATE_KEY).*)",
    re.IGNORECASE,
)


class LeaseHeartbeat:
    """Keep a claimed task lease alive, retry transient failures, and expose loss."""

    def __init__(self, task_id: str, worker_id: str, lease_seconds: int) -> None:
        self.task_id = task_id
        self.worker_id = worker_id
        self.interval = max(1, lease_seconds // 3)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="demo-lease-heartbeat", daemon=True)
        self._error: RuntimeError | None = None
        self._failed = threading.Event()

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()
        if self._error and exc is None:
            raise RuntimeError("Worker lease heartbeat failed; task cannot continue") from self._error

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=31)

    def check(self) -> None:
        if self._error:
            raise RuntimeError("Worker lease heartbeat failed; task cannot continue") from self._error

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            delay = 1
            while not self._stop.is_set():
                try:
                    api("heartbeat", {"task_id": self.task_id, "worker_id": self.worker_id})
                    break
                except Exception as error:
                    if "lease_lost" in str(error):
                        self._error = RuntimeError("Worker lease was lost")
                        self._failed.set()
                        print("Demo lease heartbeat rejected; stopping worker", file=sys.stderr)
                        return
                    print(
                        f"Demo lease heartbeat failed ({type(error).__name__}); retrying in {delay}s",
                        file=sys.stderr,
                        flush=True,
                    )
                    if self._stop.wait(delay):
                        return
                    delay = min(delay * 2, 30)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def api(action: str, payload: dict, *, method: str = "POST") -> dict:
    request = urllib.request.Request(
        f"{API}/.netlify/functions/demo-worker?action={urllib.parse.quote(action)}",
        data=json.dumps(payload, separators=(",", ":")).encode() if method != "GET" else None,
        method=method,
        headers={"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json", "Cache-Control": "no-store"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.headers.get("Cache-Control", "").lower().find("no-store") < 0:
                raise RuntimeError("Worker API response was not marked no-store")
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise RuntimeError("Worker API request failed") from error
    if not body.get("ok"):
        raise RuntimeError(f"Worker API rejected {action}: {body.get('error', {}).get('code', 'unknown')}")
    return body


def child_test_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Keep test subprocesses from inheriting worker, provider, or GitHub credentials."""
    environment = os.environ if source is None else source
    return {key: value for key, value in environment.items() if not TEST_SECRET_ENV.match(key)}


def run(args: list[str], cwd: pathlib.Path, *, check: bool = True,
        isolated_test_env: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if isolated_test_env:
        env = child_test_environment(env)
    token = env.get("GITHUB_TOKEN", "") or env.get("DEMO_READ_TOKEN", "")
    if token:
        import base64
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False, env=env)
    if check and result.returncode:
        # Tool output is intentionally not copied to the Actions log; it may contain user content.
        raise RuntimeError(f"Command failed ({pathlib.Path(args[0]).name}, exit {result.returncode})")
    return result


def validate_patch(patch: str) -> None:
    if not patch or len(patch) > 400_000 or "GIT binary patch" in patch:
        raise RuntimeError("Model returned an empty or oversized patch")
    paths = [p for p in re.findall(r"^(?:\+\+\+|---) (?:a/|b/)?(.+)$", patch, re.MULTILINE) if p != "/dev/null"]
    if not paths or any(p.startswith(("/", "../")) or ".." in pathlib.PurePosixPath(p).parts for p in paths):
        raise RuntimeError("Model patch contains an unsafe path")
    if any(re.search(r"(?:\.env|credentials|secret|\.github/workflows|(?:^|/)tests?/|^scripts/kelp_demo_worker\.py$)", p, re.I) for p in paths):
        raise RuntimeError("Model patch targets a protected path")


def repository_context(tree: pathlib.Path) -> str:
    tracked = run(["git", "ls-files"], tree).stdout.splitlines()
    listing = "\n".join(tracked[:1200])
    if len(tracked) > 1200:
        listing += f"\n... ({len(tracked) - 1200} additional tracked paths omitted)"
    readme = tree / "README.md"
    overview = readme.read_text(encoding="utf-8")[:12000] if readme.is_file() else "(No root README.md)"
    return f"Repository file index (bounded to 1200 paths):\n{listing}\n\nREADME.md (bounded to 12000 characters):\n{overview}"


def read_repository_file(tree: pathlib.Path, name: str) -> str:
    relative = pathlib.PurePosixPath(name)
    if (not name or relative.is_absolute() or ".." in relative.parts
            or re.search(r"(?:^|/)(?:\.env[^/]*|credentials?|secrets?)(?:$|/)", name, re.I)
            or re.search(r"(?:^|/)\.git(?:/|$)", name, re.I)):
        return "Error: path is not available to the repository reader."
    tracked = set(run(["git", "ls-files", "--", name], tree).stdout.splitlines())
    target = tree.joinpath(*relative.parts)
    if name not in tracked or target.is_symlink() or not target.resolve().is_relative_to(tree.resolve()):
        return "Error: only tracked, in-repository files can be read."
    if target.stat().st_size > 24_000:
        return "Error: file exceeds the 24000-byte read limit."
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "Error: binary files are not available to the repository reader."


def model_patch(tree: pathlib.Path, model: str, task_prompt: str, provider_key: str) -> str:
    messages = [{"role": "user", "content": (
        "Make the smallest safe code change requested below in this repository. Return ONLY a unified git diff patch, no markdown. "
        "Do not edit credentials, .env files, GitHub workflows, dependencies, or tests unless explicitly requested.\n\n"
        f"Repository context:\n{repository_context(tree)}\n\nRequest:\n{task_prompt}"
    )}]
    tools = [{"type": "function", "function": {
        "name": "read_repository_file",
        "description": "Read one tracked UTF-8 repository file (maximum 24000 bytes). At most 8 reads are allowed.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"], "additionalProperties": False},
    }}]
    reads = 0
    for _ in range(9):
        request_data = json.dumps({"model": model, "messages": messages, "tools": tools,
                                   "tool_choice": "auto", "max_tokens": 6000, "temperature": 0},
                                  separators=(",", ":")).encode()
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions", data=request_data, method="POST",
            headers={"Authorization": f"Bearer {provider_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.loads(response.read())
        except Exception as error:
            raise RuntimeError("OpenRouter model request failed") from error
        finally:
            del request_data
            del request
        message = result.get("choices", [{}])[0].get("message", {})
        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content", "")
        messages.append(message)
        for call in calls:
            reads += 1
            if reads > 8 or call.get("function", {}).get("name") != "read_repository_file":
                output = "Error: tool is unavailable or the read limit was reached."
            else:
                try:
                    arguments = json.loads(call["function"].get("arguments", "{}"))
                    output = read_repository_file(tree, arguments.get("path", ""))
                except (KeyError, TypeError, ValueError):
                    output = "Error: provide a valid file path."
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})
    raise RuntimeError("Model exceeded the bounded repository-read rounds")


def prepare_one() -> bool:
    worker_id = f"gha-{uuid.uuid4()}"
    claimed = api("claim", {"worker_id": worker_id})
    task = claimed.get("task")
    if not task:
        return False
    global ACTIVE_TASK
    ACTIVE_TASK = (task["task_id"], worker_id)
    heartbeat = LeaseHeartbeat(task["task_id"], worker_id, int(task.get("lease_seconds", 300)))
    # Immediately mask the user key; it is kept only in this process and is never written to disk.
    provider_key = task.pop("provider_key")
    print(f"::add-mask::{provider_key}")
    with heartbeat:
        api("event", {"task_id": task["task_id"], "worker_id": worker_id, "type": "working", "data": {"state": "working"}})
        base = task.get("base_sha") or os.environ["GITHUB_SHA"]
        if not re.fullmatch(r"[0-9a-f]{40}", base):
            base = os.environ["GITHUB_SHA"]
        with tempfile.TemporaryDirectory(prefix="kelp-demo-") as scratch:
            tree = pathlib.Path(scratch) / "repo"
            run(["git", "clone", "--no-checkout", f"https://github.com/{REPO}.git", str(tree)], pathlib.Path.cwd())
            run(["git", "fetch", "--depth=1", "origin", base], tree)
            run(["git", "checkout", "--detach", "FETCH_HEAD"], tree)
            os.environ.pop("DEMO_READ_TOKEN", None)
            base_sha = run(["git", "rev-parse", "HEAD"], tree).stdout.strip()
            if base_sha != base:
                raise RuntimeError("Pinned base SHA changed during checkout")

            patch = model_patch(tree, task["model"], task["prompt"], provider_key)
            heartbeat.check()
            del provider_key
            validate_patch(patch)
            patch_path = pathlib.Path(scratch) / "candidate.patch"
            patch_path.write_text(patch, encoding="utf-8")
            run(["git", "apply", "--check", str(patch_path)], tree)
            run(["git", "apply", str(patch_path)], tree)
            changed = sorted(set(run(["git", "diff", "--name-only"], tree).stdout.splitlines()) |
                             set(run(["git", "ls-files", "--others", "--exclude-standard"], tree).stdout.splitlines()))
            if not changed or len(changed) > 100:
                raise RuntimeError("Model patch changed no files or too many files")
            if run(["git", "diff", "--name-only", "--diff-filter=D"], tree).stdout.strip():
                raise RuntimeError("Model patch may not delete files")

            api("event", {"task_id": task["task_id"], "worker_id": worker_id, "type": "testing", "data": {"state": "testing"}})
            tests = run([sys.executable, "-m", "pytest", "-q"], tree, check=False, isolated_test_env=True)
            checks = {"pytest": {"passed": tests.returncode == 0, "exit_code": tests.returncode}}
            if tests.returncode:
                raise RuntimeError("Test suite failed")
            heartbeat.check()
            files = []
            for name in changed:
                relative = pathlib.PurePosixPath(name)
                target = tree.joinpath(*relative.parts)
                if relative.is_absolute() or ".." in relative.parts or target.is_symlink() or not target.resolve().is_relative_to(tree.resolve()):
                    raise RuntimeError("Candidate contains an unsafe path")
                files.append({"path": name, "content": target.read_text(encoding="utf-8")})
            patch_parts = [run(["git", "diff", "--no-ext-diff", "--", name], tree).stdout for name in changed if (tree / name).exists() and not (tree / name).is_symlink()]
            for name in changed:
                if run(["git", "ls-files", "--error-unmatch", name], tree, check=False).returncode:
                    patch_parts.append(run(["git", "diff", "--no-index", "--no-ext-diff", "/dev/null", name], tree, check=False).stdout)
            candidate = {"summary": "Prepared model edit; all repository tests passed.", "files": files,
                         "diff": "\n".join(patch_parts),
                         "checks": checks, "base_sha": base_sha}
            candidate_hash = hashlib.sha256(canonical(candidate)).hexdigest()
            heartbeat.check()
            heartbeat.stop()
            api("candidate", {"task_id": task["task_id"], "worker_id": worker_id, "base_sha": base_sha,
                              "candidate": candidate, "candidate_digest": candidate_hash})
    ACTIVE_TASK = None
    return True


def prepare() -> None:
    # A wake can find several queued requests; claim each under the DB workspace lease
    # so simultaneous dispatches cannot strand work after an empty overlapping claim.
    while prepare_one():
        pass


def publish() -> None:
    payload = json.loads(os.environ["GITHUB_EVENT_CLIENT_PAYLOAD"])
    task_id = payload["task_id"]
    candidate_hash = payload["candidate_digest"]
    result = api("publish-candidate", {"task_id": task_id, "candidate_digest": candidate_hash})
    task = result["task"]
    candidate = task["candidate"]
    if hashlib.sha256(canonical(candidate)).hexdigest() != candidate_hash or candidate.get("base_sha") != task["base_sha"]:
        raise RuntimeError("Immutable candidate digest/base verification failed")
    if task["repo"] != REPO:
        raise RuntimeError("Candidate repository does not match the running repository")
    marker = f"kelp-demo:{task_id}:{candidate_hash}"
    branch = f"kelp-demo/{task_id}"
    with tempfile.TemporaryDirectory(prefix="kelp-publish-") as scratch:
        tree = pathlib.Path(scratch) / "repo"
        run(["git", "clone", f"https://github.com/{REPO}.git", str(tree)], pathlib.Path.cwd())
        run(["git", "fetch", "origin", task["base_sha"]], tree)
        run(["git", "checkout", "--detach", task["base_sha"]], tree)
        existing = run(["git", "ls-remote", "--heads", "origin", branch], tree, check=False).stdout.strip()
        if existing:
            run(["git", "fetch", "origin", branch], tree)
            run(["git", "checkout", "--detach", "FETCH_HEAD"], tree)
            message = run(["git", "log", "-1", "--format=%B"], tree).stdout
            if marker not in message:
                raise RuntimeError("Publish branch already exists without the expected unique marker")
            parent = run(["git", "rev-parse", "HEAD^"], tree).stdout.strip()
            if parent != task["base_sha"]:
                raise RuntimeError("Existing publish branch is not based on the pinned candidate base")
            expected_paths = {entry["path"] for entry in candidate["files"]}
            actual_paths = set(run(["git", "diff", "--name-only", f"{task['base_sha']}...HEAD"], tree).stdout.splitlines())
            if actual_paths != expected_paths:
                raise RuntimeError("Existing publish branch file set differs from the immutable candidate")
            for entry in candidate["files"]:
                current = run(["git", "show", f"HEAD:{entry['path']}"], tree).stdout
                if current != entry["content"]:
                    raise RuntimeError("Existing publish branch content differs from the immutable candidate")
        else:
            for entry in candidate["files"]:
                rel = pathlib.PurePosixPath(entry["path"])
                if rel.is_absolute() or ".." in rel.parts or len(rel.parts) == 0:
                    raise RuntimeError("Candidate contains an unsafe path")
                target = tree.joinpath(*rel.parts)
                if any(parent.is_symlink() for parent in [target, *target.parents] if parent != tree.parent) or not target.resolve(strict=False).is_relative_to(tree.resolve()):
                    raise RuntimeError("Candidate path traverses a symlink")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(entry["content"], encoding="utf-8")
            run(["git", "config", "user.name", "github-actions[bot]"], tree)
            run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], tree)
            run(["git", "add", "--all"], tree)
            run(["git", "commit", "-m", marker], tree)
            commit = run(["git", "rev-parse", "HEAD"], tree).stdout.strip()
            run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], tree)
        commit = run(["git", "rev-parse", "HEAD"], tree).stdout.strip()
        base_branch = os.environ.get("DEMO_PUBLISH_BASE", "main")
        query = urllib.parse.urlencode({"head": f"{REPO.split('/')[0]}:{branch}", "state": "all"})
        response = github_request(f"https://api.github.com/repos/{REPO}/pulls?{query}")
        prs = json.loads(response)
        prs = [candidate_pr for candidate_pr in prs if candidate_pr.get("base", {}).get("ref") == base_branch]
        if prs:
            pr = prs[0]
        else:
            pr_data = json.dumps({"title": f"Kelp demo: {task_id[:8]}", "head": branch, "base": base_branch,
                                  "body": f"Prepared from an immutable, approved demo candidate.\n\nCandidate: `{candidate_hash}`\nMarker: `{marker}`"}).encode()
            pr = json.loads(github_request(f"https://api.github.com/repos/{REPO}/pulls", method="POST", data=pr_data))
        api("finish", {"task_id": task_id, "candidate_digest": candidate_hash, "publish_marker": marker,
                        "pr_url": pr["html_url"], "commit_sha": commit})


def github_request(url: str, *, method: str = "GET", data: bytes | None = None) -> bytes:
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except Exception as error:
        raise RuntimeError("GitHub publish API request failed") from error


if __name__ == "__main__":
    try:
        if os.environ["DEMO_JOB"] == "prepare":
            prepare()
        else:
            publish()
    except Exception as error:
        # Never print exception text or subprocess/model responses; prompt and credentials are private.
        if ACTIVE_TASK:
            try:
                api("fail", {"task_id": ACTIVE_TASK[0], "worker_id": ACTIVE_TASK[1], "message": "Prepare worker failed; see sanitized workflow status."})
            except Exception as notify_error:
                print(f"Demo failure notification failed: {type(notify_error).__name__}", file=sys.stderr)
        print(f"Demo {os.environ.get('DEMO_JOB', 'worker')} job failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
