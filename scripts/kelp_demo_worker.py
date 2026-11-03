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
import urllib.parse
import urllib.error
import urllib.request
import uuid


API = os.environ["DEMO_API_BASE"].rstrip("/")
WORKER_TOKEN = os.environ["DEMO_WORKER_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
ACTIVE_TASK: tuple[str, str] | None = None


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


def run(args: list[str], cwd: pathlib.Path, *, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
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


def prepare_one() -> bool:
    worker_id = f"gha-{uuid.uuid4()}"
    claimed = api("claim", {"worker_id": worker_id})
    task = claimed.get("task")
    if not task:
        return False
    global ACTIVE_TASK
    ACTIVE_TASK = (task["task_id"], worker_id)
    # Immediately mask the user key; it is kept only in this process and is never written to disk.
    provider_key = task.pop("provider_key")
    print(f"::add-mask::{provider_key}")
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

        prompt = (
            "Make the smallest safe code change requested below in this repository. "
            "Return ONLY a unified git diff patch, no markdown. Do not edit credentials, "
            ".env files, GitHub workflows, dependencies, or tests unless the request explicitly asks.\n\n"
            f"Request:\n{task['prompt']}"
        )
        request_data = json.dumps({"model": task["model"], "messages": [{"role": "user", "content": prompt}],
                                   "max_tokens": 6000, "temperature": 0}, separators=(",", ":")).encode()
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
        del provider_key
        patch = result.get("choices", [{}])[0].get("message", {}).get("content", "")
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
        tests = run([sys.executable, "-m", "pytest", "-q"], tree, check=False)
        checks = {"pytest": {"passed": tests.returncode == 0, "exit_code": tests.returncode}}
        if tests.returncode:
            raise RuntimeError("Test suite failed")
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
            except Exception:
                pass
        print(f"Demo {os.environ.get('DEMO_JOB', 'worker')} job failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
