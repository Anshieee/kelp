"""`github.write` handler: commit file changes on a branch and open a PR."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from skills.errors import SkillHandlerError
from skills.httpx_transport import default_transport_factory, request as httpx_request

__all__ = ["run"]

_MANIFEST = Path(__file__).with_name("manifest.json")
_TRANSPORT_FACTORY: Callable[[], Any] = default_transport_factory


class GitHubWriteContext(Protocol):
    """Context capabilities used by the handler, including guarded egress."""

    def secret(self, name: str) -> str: ...

    def check_repo(self, repo: str) -> str: ...

    def scrub(self, text: str) -> str: ...

    def egress(self, *, transport: Callable[..., Any]) -> Any: ...


def _invalid_arguments(arguments: Mapping[str, Any]) -> None:
    try:
        schema = json.loads(_MANIFEST.read_text(encoding="utf-8"))["input_schema"]
    except (OSError, json.JSONDecodeError, KeyError):  # pragma: no cover - packaging fault
        raise SkillHandlerError(
            "handler_error", "The GitHub write contract could not be loaded."
        ) from None
    errors = sorted(
        Draft202012Validator(schema).iter_errors(arguments),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        first = errors[0]
        pointer = "/" + "/".join(str(part) for part in first.absolute_path)
        raise SkillHandlerError(
            "invalid_arguments",
            f"Invalid GitHub write arguments at {pointer or '/'}.",
            user_fixable=True,
            details={"field": pointer},
        )


async def _request(
    egress: Any, method: str, url: str, token: str, payload: Mapping[str, Any] | None = None
) -> Mapping[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
    try:
        response = await egress.request(
            method,
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            body=body,
        )
    except Exception:
        # Transport exceptions can contain request headers; never expose them.
        raise SkillHandlerError(
            "handler_error", "The GitHub API request failed.", retryable=False
        ) from None
    if not 200 <= response.status < 300:
        raise SkillHandlerError(
            "handler_error",
            f"The GitHub API rejected the operation (HTTP {response.status}).",
            retryable=False,
            user_fixable=response.status in {401, 403, 404, 422},
        )
    try:
        result = response.json()
    except (ValueError, UnicodeDecodeError):
        raise SkillHandlerError(
            "handler_error", "The GitHub API returned an invalid response."
        ) from None
    if not isinstance(result, Mapping):
        raise SkillHandlerError(
            "handler_error", "The GitHub API returned an invalid response."
        )
    return result


async def run(arguments: Mapping[str, Any], context: GitHubWriteContext) -> Mapping[str, Any]:
    """Create a commit and pull request; the registry gates this handler on approval."""

    if not isinstance(arguments, Mapping):
        raise SkillHandlerError(
            "invalid_arguments", "Arguments must be an object.", user_fixable=True
        )
    _invalid_arguments(arguments)
    repo = context.check_repo(str(arguments["repo"]))
    token = context.secret("GITHUB_TOKEN")
    encoded_repo = quote(repo, safe="/")
    base = str(arguments["base"])
    branch = str(arguments["branch"])
    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        timeout: float,
    ) -> Any:
        return httpx_request(
            method,
            url,
            headers,
            body,
            timeout,
            transport_factory=_TRANSPORT_FACTORY,
        )

    egress = context.egress(transport=transport)

    base_ref = await _request(
        egress,
        "GET",
        f"https://api.github.com/repos/{encoded_repo}/git/ref/heads/{quote(base, safe='/')}" ,
        token,
    )
    base_sha = base_ref.get("object", {}).get("sha")
    if not isinstance(base_sha, str) or len(base_sha) != 40:
        raise SkillHandlerError("handler_error", "The GitHub base reference was invalid.")
    await _request(
        egress,
        "POST",
        f"https://api.github.com/repos/{encoded_repo}/git/refs",
        token,
        {"ref": f"refs/heads/{branch}", "sha": base_sha},
    )

    tree_entries: list[dict[str, str]] = []
    for item in arguments["files"]:
        content = str(item["content"]).encode("utf-8")
        blob = await _request(
            egress,
            "POST",
            f"https://api.github.com/repos/{encoded_repo}/git/blobs",
            token,
            {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        sha = blob.get("sha")
        if not isinstance(sha, str) or len(sha) != 40:
            raise SkillHandlerError("handler_error", "The GitHub blob response was invalid.")
        tree_entries.append(
            {"path": str(item["path"]), "mode": "100644", "type": "blob", "sha": sha}
        )

    commit_response = await _request(
        egress,
        "GET",
        f"https://api.github.com/repos/{encoded_repo}/git/commits/{base_sha}",
        token,
    )
    base_tree = commit_response.get("tree", {}).get("sha")
    if not isinstance(base_tree, str) or len(base_tree) != 40:
        raise SkillHandlerError("handler_error", "The GitHub base tree was invalid.")
    tree = await _request(
        egress,
        "POST",
        f"https://api.github.com/repos/{encoded_repo}/git/trees",
        token,
        {"base_tree": base_tree, "tree": tree_entries},
    )
    tree_sha = tree.get("sha")
    if not isinstance(tree_sha, str) or len(tree_sha) != 40:
        raise SkillHandlerError("handler_error", "The GitHub tree response was invalid.")
    commit = await _request(
        egress,
        "POST",
        f"https://api.github.com/repos/{encoded_repo}/git/commits",
        token,
        {
            "message": str(arguments["title"]),
            "tree": tree_sha,
            "parents": [base_sha],
        },
    )
    commit_sha = commit.get("sha")
    if not isinstance(commit_sha, str) or len(commit_sha) != 40:
        raise SkillHandlerError("handler_error", "The GitHub commit response was invalid.")
    await _request(
        egress,
        "PATCH",
        f"https://api.github.com/repos/{encoded_repo}/git/refs/heads/{quote(branch, safe='/')}" ,
        token,
        {"sha": commit_sha, "force": False},
    )
    pull = await _request(
        egress,
        "POST",
        f"https://api.github.com/repos/{encoded_repo}/pulls",
        token,
        {
            "title": str(arguments["title"]),
            "body": str(arguments["body"]),
            "head": branch,
            "base": base,
        },
    )
    pull_number = pull.get("number")
    pull_url = pull.get("html_url")
    if not isinstance(pull_number, int) or isinstance(pull_number, bool) or not isinstance(pull_url, str):
        raise SkillHandlerError("handler_error", "The GitHub pull request response was invalid.")
    return {
        "repo": repo,
        "branch": branch,
        "commit_sha": commit_sha,
        "pull_request": {"number": pull_number, "url": context.scrub(pull_url), "state": "open"},
    }
