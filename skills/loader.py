"""Fail-fast skill loader and invocation runtime.

Contract: ``docs/SKILL_SPEC.md``. A skill is a directory containing
``manifest.json`` plus an async handler referenced by the manifest's ``entry``.
Adding a skill must not require editing this module.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from openshell.policy import Decision, PolicyConfig, PolicyEngine

from .context import ApprovalRequest, SkillContext
from .errors import (
    SkillHandlerError,
    SkillManifestError,
    Violation,
)
from .policy import (
    SCOPE_FORMATS,
    GuardedSkillContext,
    InvocationPolicy,
    PolicyDenied,
    active_invocation,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openshell.audit import AuditLog

__all__ = [
    "APPROVAL_TTL_SECONDS",
    "DEFAULT_PLUGIN_ROOT",
    "DEFAULT_SKILL_ROOT",
    "LoadedSkill",
    "PLUGIN_ALLOW_UNPINNED_ENV",
    "PLUGIN_LOCK_FILENAME",
    "PLUGIN_LOCK_VERSION",
    "PLUGIN_MAX_BYTES",
    "PLUGIN_ROOT_ENV",
    "PluginPackage",
    "PluginPackageError",
    "SkillRegistry",
    "default_plugin_root",
    "inspect_plugin_package",
    "load_registry",
    "materialize_plugin_package",
    "package_digest",
    "read_plugin_lock",
    "write_plugin_lock",
]

MANIFEST_FILENAME = "manifest.json"
MANIFEST_MAX_BYTES = 256 * 1024
SPEC_VERSION = "1.0"
APPROVAL_TTL_SECONDS = 600
DEFAULT_SKILL_ROOT = Path(__file__).resolve().parent / "builtin"
_META_SCHEMA_PATH = Path(__file__).resolve().parent / "manifest.schema.json"

# -- installed plugin packages (KEL-25 / M4-D) ------------------------------
PLUGIN_ROOT_ENV = "KELP_PLUGIN_ROOT"
PLUGIN_ALLOW_UNPINNED_ENV = "KELP_PLUGINS_ALLOW_UNPINNED"
PLUGIN_LOCK_FILENAME = "plugins.lock.json"
PLUGIN_LOCK_VERSION = 1
PLUGIN_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "var" / "plugins"
_PLUGIN_IGNORED_DIRS = frozenset({"__pycache__"})
_PLUGIN_IGNORED_SUFFIXES = (".pyc", ".pyo")

ApprovalHook = Callable[[ApprovalRequest], Awaitable[bool]]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}(\.[a-z][a-z0-9_]{1,31})+$")
_HOST_RE = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_REPO_RE = re.compile(r"^(?:\*|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$")
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def _load_meta_validator() -> Draft202012Validator:
    try:
        raw = json.loads(_META_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - packaging fault
        raise RuntimeError(
            f"skill manifest meta-schema unreadable at {_META_SCHEMA_PATH}: {exc}"
        ) from exc
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_META_VALIDATOR = _load_meta_validator()


def _ptr(parts: Iterable[Any]) -> str:
    escaped = [str(p).replace("~", "~0").replace("/", "~1") for p in parts]
    return "/" + "/".join(escaped) if escaped else ""


def _error_pointer(error: Any, instance: Any) -> str:
    """Point a jsonschema error at the offending field, not its parent.

    jsonschema reports ``required`` errors at the object itself; for fail-fast
    messages the missing property name is the useful address.
    """

    path = list(error.absolute_path)
    if error.validator == "required" and isinstance(instance, Mapping):
        required = error.validator_value if isinstance(error.validator_value, list) else []
        missing = [name for name in required if name not in instance]
        if missing:
            path.append(missing[0])
    return _ptr(path)


def _ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label(data: Mapping[str, Any], dirname: str) -> str:
    name = data.get("name")
    return name if isinstance(name, str) and name else dirname


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    version: str
    description: str
    manifest: Mapping[str, Any]
    manifest_path: Path
    handler: Callable[..., Awaitable[Mapping[str, Any]]]
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator
    tool_names: tuple[str, ...]
    timeout_ms: int
    idempotent: bool
    requires_approval: bool
    effects: tuple[str, ...]
    scopes: Mapping[str, tuple[str, ...]]
    declared_secrets: tuple[str, ...]
    cost_class: str
    #: Filesystem origin (skill directory for built-ins, package path for plugins).
    origin: str = ""
    #: ``sha256:…`` digest for installed plugins; ``None`` for built-ins.
    digest: str | None = None
    @property
    def tool_name(self) -> str:
        return self.tool_names[0]


def _read_manifest(path: Path, dirname: str) -> tuple[dict[str, Any] | None, list[Violation]]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, [Violation(dirname, "", f"cannot stat manifest.json: {exc}")]
    if size > MANIFEST_MAX_BYTES:
        return None, [
            Violation(
                dirname,
                "",
                f"manifest.json is {size} bytes; limit is {MANIFEST_MAX_BYTES}",
            )
        ]
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [Violation(dirname, "", f"cannot read manifest.json: {exc}")]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [
            Violation(dirname, "", f"manifest.json is not valid JSON: {exc.msg} (line {exc.lineno})")
        ]
    if not isinstance(data, dict):
        return None, [Violation(dirname, "", "manifest.json must contain a JSON object")]
    return data, []


class PluginPackageError(Exception):
    """A plugin package (directory or zip) violates the M4-D packaging rules."""


@dataclass(frozen=True)
class PluginPackage:
    """A validated package image: manifest plus every file's content.

    ``entries`` are ``(posix_relpath, mode, content)`` triples in the exact
    order the digest covers. The package is never executed at inspection time.
    """

    name: str
    version: str
    digest: str
    source: Path
    manifest: Mapping[str, Any]
    entries: tuple[tuple[str, int, bytes], ...]


def default_plugin_root(environ: Mapping[str, str] | None = None) -> Path:
    """``KELP_PLUGIN_ROOT`` if set, else ``<repo>/var/plugins``."""

    env = os.environ if environ is None else environ
    raw = str(env.get(PLUGIN_ROOT_ENV, "")).strip()
    return Path(raw).expanduser() if raw else DEFAULT_PLUGIN_ROOT


def package_digest(entries: Iterable[tuple[str, int, bytes]]) -> str:
    """SHA-256 over the sorted ``(relpath, mode, content)`` set.

    Framing: for each entry in ``relpath`` order, feed ``relpath``, NUL,
    ``mode`` as four octal digits, NUL, then the 32-byte SHA-256 of the
    content. Fixed-width frames keep the digest unambiguous. Unreadable
    caches (``__pycache__``, ``*.pyc``) never enter the set.
    """

    digest = hashlib.sha256()
    for relpath, mode, content in sorted(entries, key=lambda item: item[0]):
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(f"{mode & 0o777:04o}".encode("ascii"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(content).digest())
    return "sha256:" + digest.hexdigest()


def _scan_package_directory(path: Path) -> list[tuple[str, int, bytes]]:
    entries: list[tuple[str, int, bytes]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(name for name in dirnames if name not in _PLUGIN_IGNORED_DIRS)
        for name in dirnames:
            if (base / name).is_symlink():
                raise PluginPackageError(
                    f"package {path} contains a symlinked directory: "
                    f"{(base / name).relative_to(path).as_posix()}"
                )
        for name in sorted(filenames):
            file = base / name
            relpath = file.relative_to(path).as_posix()
            if file.is_symlink():
                raise PluginPackageError(f"package {path} contains a symlink: {relpath}")
            if name.endswith(_PLUGIN_IGNORED_SUFFIXES):
                continue
            if not file.is_file():
                raise PluginPackageError(f"package {path} contains a non-regular file: {relpath}")
            size = file.stat().st_size
            total += size
            if total > PLUGIN_MAX_BYTES:
                raise PluginPackageError(
                    f"package {path} exceeds the {PLUGIN_MAX_BYTES}-byte limit"
                )
            content = file.read_bytes()
            entries.append((relpath, stat.S_IMODE(file.stat().st_mode), content))
    return entries


def _scan_package_zip(path: Path) -> list[tuple[str, int, bytes]]:
    entries: list[tuple[str, int, bytes]] = []
    total = 0
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PluginPackageError(f"cannot read plugin archive {path}: {exc}") from exc
    with archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            name = info.filename
            if info.is_dir():
                continue
            pure = PurePosixPath(name)
            if not name or name.startswith(("/", "\\")) or pure.is_absolute():
                raise PluginPackageError(f"archive {path} contains an absolute member: {name!r}")
            if "\\" in name or ".." in pure.parts:
                raise PluginPackageError(f"archive {path} contains an unsafe member: {name!r}")
            if any(part in _PLUGIN_IGNORED_DIRS for part in pure.parts):
                continue
            if pure.suffix in _PLUGIN_IGNORED_SUFFIXES:
                continue
            raw_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(raw_mode):
                raise PluginPackageError(f"archive {path} contains a symlink member: {name!r}")
            total += info.file_size
            if total > PLUGIN_MAX_BYTES:
                raise PluginPackageError(
                    f"archive {path} exceeds the {PLUGIN_MAX_BYTES}-byte limit"
                )
            entries.append((pure.as_posix(), stat.S_IMODE(raw_mode) or 0o644, archive.read(info)))
    return entries


def _package_entries(path: Path) -> list[tuple[str, int, bytes]]:
    if path.is_symlink():
        raise PluginPackageError(f"plugin package {path} must not be a symlink")
    if path.is_dir():
        return _scan_package_directory(path)
    if path.is_file() and path.suffix.lower() == ".zip":
        return _scan_package_zip(path)
    raise PluginPackageError(
        f"plugin package {path} must be a directory or a .zip archive"
    )


def inspect_plugin_package(path: str | Path) -> PluginPackage:
    """Validate a package without importing or executing any of its code."""

    source = Path(path)
    entries = _package_entries(source)
    if not entries:
        raise PluginPackageError(f"plugin package {source} is empty")

    by_relpath = {relpath: content for relpath, _mode, content in entries}
    try:
        manifest_bytes = by_relpath[MANIFEST_FILENAME]
    except KeyError:
        raise PluginPackageError(
            f"plugin package {source} has no {MANIFEST_FILENAME} at its root"
        ) from None
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginPackageError(f"{MANIFEST_FILENAME} in {source} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PluginPackageError(f"{MANIFEST_FILENAME} in {source} must contain a JSON object")

    name = manifest.get("name")
    label = name if isinstance(name, str) and name else source.name
    structural = sorted(
        _META_VALIDATOR.iter_errors(manifest),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if structural:
        details = "; ".join(
            f"{_error_pointer(error, manifest)}: {_short(error.message)}"
            for error in structural[:5]
        )
        raise PluginPackageError(f"invalid plugin manifest in {source}: {details}")
    semantic = _check_semantics(manifest, label)
    if semantic:
        raise PluginPackageError(
            "invalid plugin manifest in "
            f"{source}: " + "; ".join(f"{v.field or '/'}: {v.message}" for v in semantic)
        )

    entry = manifest["entry"]
    if "module" in entry:
        raise PluginPackageError(
            f"plugin {name!r} in {source} uses entry.module; installed plugins MUST use "
            "entry.path so the loader never mutates sys.path"
        )
    entry_path = entry["path"]
    if entry_path not in by_relpath:
        raise PluginPackageError(
            f"plugin {name!r} in {source} declares entry.path {entry_path!r}, "
            "which is not present in the package"
        )

    return PluginPackage(
        name=manifest["name"],
        version=manifest["version"],
        digest=package_digest(entries),
        source=source,
        manifest=manifest,
        entries=tuple(entries),
    )


def materialize_plugin_package(package: PluginPackage) -> Path:
    """Return an importable directory for the package (zip archives unpack once)."""

    if package.source.is_dir():
        return package.source.resolve()
    slug = re.sub(r"[^0-9A-Za-z_]", "_", package.name)
    target = Path(tempfile.mkdtemp(prefix=f"kelp-plugin-{slug}-"))
    atexit.register(shutil.rmtree, target, True)
    for relpath, mode, content in package.entries:
        destination = target.joinpath(*PurePosixPath(relpath).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        os.chmod(destination, mode or 0o644)
    return target


def read_plugin_lock(root: str | Path) -> dict[str, dict[str, str]]:
    """Read ``plugins.lock.json``; a missing file means "nothing pinned"."""

    path = Path(root) / PLUGIN_LOCK_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginPackageError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise PluginPackageError(f"{path} must contain a JSON object")
    if raw.get("version", PLUGIN_LOCK_VERSION) != PLUGIN_LOCK_VERSION:
        raise PluginPackageError(f"{path} has an unsupported lock version {raw.get('version')!r}")
    plugins = raw.get("plugins", {})
    if not isinstance(plugins, Mapping):
        raise PluginPackageError(f"{path} field 'plugins' must be an object")
    pins: dict[str, dict[str, str]] = {}
    for name, pin in plugins.items():
        if not isinstance(name, str) or not isinstance(pin, Mapping):
            raise PluginPackageError(f"{path} has a malformed pin for {name!r}")
        digest = pin.get("digest")
        version = pin.get("version")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise PluginPackageError(f"{path} pin for {name!r} has an invalid digest")
        if not isinstance(version, str) or not version:
            raise PluginPackageError(f"{path} pin for {name!r} has an invalid version")
        pins[name] = {"name": name, "version": version, "digest": digest}
    return pins


def write_plugin_lock(root: str | Path, pins: Mapping[str, Mapping[str, str]]) -> Path:
    """Atomically rewrite ``plugins.lock.json`` with sorted pins."""

    path = Path(root) / PLUGIN_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": PLUGIN_LOCK_VERSION,
        "plugins": {
            name: {
                "name": name,
                "version": str(pin["version"]),
                "digest": str(pin["digest"]),
            }
            for name, pin in sorted(pins.items())
        },
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def _collect_refs(node: Any, pointer: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"
            if key in {"$ref", "$dynamicRef"} and isinstance(value, str):
                found.append((child, value))
            found.extend(_collect_refs(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_collect_refs(value, f"{pointer}/{index}"))
    return found


def _collect_formats(node: Any, pointer: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"
            if key == "format" and isinstance(value, str):
                found.append((child, value))
            found.extend(_collect_formats(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_collect_formats(value, f"{pointer}/{index}"))
    return found


def _check_schema(
    schema: Any, field_name: str, label: str, *, require_closed: bool
) -> list[Violation]:
    violations: list[Violation] = []
    if not isinstance(schema, Mapping):
        return [Violation(label, f"/{field_name}", "must be a JSON Schema object")]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        pointer = _ptr(exc.path)
        return [
            Violation(
                label,
                f"/{field_name}{pointer}",
                f"invalid JSON Schema: {_short(exc.message)}",
            )
        ]
    for pointer, ref in _collect_refs(schema):
        if not ref.startswith("#"):
            violations.append(
                Violation(
                    label,
                    f"/{field_name}{pointer}",
                    f"remote references are not allowed ({ref!r}); inline or use local '#/' refs",
                )
            )
    for pointer, declared in _collect_formats(schema):
        if declared.startswith("kelp") and declared not in SCOPE_FORMATS:
            violations.append(
                Violation(
                    label,
                    f"/{field_name}{pointer}",
                    f"{declared!r} is not a supported scope format; use one of "
                    + ", ".join(sorted(SCOPE_FORMATS)),
                )
            )
    if require_closed and schema.get("additionalProperties") is not False:
        violations.append(
            Violation(
                label,
                f"/{field_name}/additionalProperties",
                "input_schema must set additionalProperties=false at the root",
            )
        )
    return violations


def _check_entry(
    data: Mapping[str, Any],
    label: str,
    *,
    base_dir: Path | None = None,
    module_key: str = "",
) -> tuple[Callable[..., Awaitable[Mapping[str, Any]]] | None, list[Violation]]:
    entry = data["entry"]
    callable_name = entry["callable"]
    if "path" in entry:
        if base_dir is None:
            return None, [
                Violation(
                    label,
                    "/entry/path",
                    "entry.path is only valid for installed plugin packages; "
                    "built-in skills must use entry.module",
                )
            ]
        return _check_entry_path(base_dir, entry["path"], callable_name, label, module_key)
    module_name = entry["module"]
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - import can fail in arbitrary ways
        return None, [
            Violation(
                label,
                "/entry/module",
                f"cannot import entry module {module_name!r}: {type(exc).__name__}: {_short(exc)}",
            )
        ]
    handler = getattr(module, callable_name, None)
    if handler is None:
        return None, [
            Violation(label, "/entry/callable", f"module {module_name!r} has no attribute {callable_name!r}")
        ]
    if not inspect.iscoroutinefunction(handler):
        return None, [
            Violation(label, "/entry/callable", f"{module_name}.{callable_name} must be an async function")
        ]
    return handler, []


def _check_entry_path(
    base_dir: Path,
    entry_path: str,
    callable_name: str,
    label: str,
    module_key: str,
) -> tuple[Callable[..., Awaitable[Mapping[str, Any]]] | None, list[Violation]]:
    """Import a plugin entry from its file path without touching ``sys.path``."""

    pure = PurePosixPath(entry_path)
    if pure.is_absolute() or ".." in pure.parts:
        return None, [
            Violation(label, "/entry/path", f"entry.path {entry_path!r} must stay inside the package")
        ]
    module_file = base_dir.joinpath(*pure.parts)
    try:
        resolved = module_file.resolve(strict=True)
    except OSError as exc:
        return None, [
            Violation(label, "/entry/path", f"entry.path {entry_path!r} is unreadable: {exc}")
        ]
    try:
        resolved.relative_to(base_dir.resolve())
    except ValueError:
        return None, [
            Violation(label, "/entry/path", f"entry.path {entry_path!r} escapes the package root")
        ]
    if not resolved.is_file():
        return None, [
            Violation(label, "/entry/path", f"entry.path {entry_path!r} is not a regular file")
        ]

    module_name = "_kelp_plugin_" + (module_key or hashlib.sha256(
        (label + entry_path).encode("utf-8")
    ).hexdigest()[:16])
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return None, [
            Violation(label, "/entry/path", f"cannot build an import spec for {entry_path!r}")
        ]
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - plugin code can fail in arbitrary ways
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        return None, [
            Violation(
                label,
                "/entry/path",
                f"cannot execute entry module {entry_path!r}: {type(exc).__name__}: {_short(exc)}",
            )
        ]
    handler = getattr(module, callable_name, None)
    if handler is None:
        return None, [
            Violation(label, "/entry/callable", f"module {entry_path!r} has no attribute {callable_name!r}")
        ]
    if not inspect.iscoroutinefunction(handler):
        return None, [
            Violation(label, "/entry/callable", f"{entry_path}:{callable_name} must be an async function")
        ]
    return handler, []


def _check_semantics(data: Mapping[str, Any], label: str) -> list[Violation]:
    violations: list[Violation] = []
    name = data["name"]
    tools = data["tools"]
    permissions = data["permissions"]
    effects = permissions["effects"]
    scopes = permissions["scopes"]
    requires_approval = permissions["requires_approval"]

    if name not in tools:
        violations.append(
            Violation(
                label,
                "/tools",
                f"v1.0 binds exactly one model-facing tool, and it must equal the skill name {name!r}",
            )
        )

    if "execute" in effects:
        violations.append(
            Violation(
                label,
                "/permissions/effects",
                "the 'execute' effect is forbidden in v1.0 (shell execution is deny-by-default)",
            )
        )

    if "write" in effects and not requires_approval:
        violations.append(
            Violation(
                label,
                "/permissions/requires_approval",
                "skills declaring the 'write' effect must set requires_approval=true",
            )
        )

    if "write" in effects and not (scopes["paths"] or scopes["repos"]):
        violations.append(
            Violation(
                label,
                "/permissions/scopes",
                "skills declaring the 'write' effect must allowlist at least one path or repo",
            )
        )

    if "network" in effects and not scopes["hosts"]:
        violations.append(
            Violation(
                label,
                "/permissions/scopes/hosts",
                "skills declaring the 'network' effect must allowlist at least one host",
            )
        )

    if not data["idempotent"] and not requires_approval:
        violations.append(
            Violation(
                label,
                "/permissions/requires_approval",
                "non-idempotent skills must set requires_approval=true",
            )
        )

    if not any(trigger.get("kind") == "model" for trigger in data["triggers"]):
        violations.append(
            Violation(
                label,
                "/triggers",
                "at least one trigger of kind 'model' is required so the kernel can expose the tool",
            )
        )

    for index, host in enumerate(scopes["hosts"]):
        pointer = f"/permissions/scopes/hosts/{index}"
        if "://" in host or "/" in host or ":" in host:
            violations.append(
                Violation(label, pointer, f"{host!r} must be a bare hostname (no scheme, port, or path)")
            )
        elif not _HOST_RE.match(host):
            violations.append(
                Violation(
                    label,
                    pointer,
                    f"{host!r} is not a valid hostname; only lowercase DNS names and a leading '*.' wildcard are allowed",
                )
            )

    for index, path in enumerate(scopes["paths"]):
        pointer = f"/permissions/scopes/paths/{index}"
        if "\\" in path or ".." in path.split("/"):
            violations.append(
                Violation(label, pointer, f"{path!r} must not contain '..' or backslashes")
            )
        elif not path.startswith("${WORKSPACE}/"):
            violations.append(
                Violation(
                    label,
                    pointer,
                    f"{path!r} must start with '${{WORKSPACE}}/' (workspace-relative scopes only)",
                )
            )

    for index, repo in enumerate(scopes["repos"]):
        if not _REPO_RE.match(repo):
            violations.append(
                Violation(
                    label,
                    f"/permissions/scopes/repos/{index}",
                    f"{repo!r} must be 'owner/name', '*' or a GitHub owner/repo pattern",
                )
            )

    for index, secret in enumerate(permissions["secrets"]):
        if not _SECRET_NAME_RE.match(secret):
            violations.append(
                Violation(
                    label,
                    f"/permissions/secrets/{index}",
                    f"{secret!r} must be an env var name (A-Z, 0-9, underscore; 3-64 chars)",
                )
            )

    violations.extend(_check_schema(data["input_schema"], "input_schema", label, require_closed=True))
    violations.extend(_check_schema(data["output_schema"], "output_schema", label, require_closed=False))
    return violations


def _build_skill(
    data: Mapping[str, Any],
    manifest_path: Path,
    handler: Callable[..., Awaitable[Mapping[str, Any]]],
    *,
    origin: str,
    digest: str | None = None,
) -> LoadedSkill:
    permissions = data["permissions"]
    return LoadedSkill(
        name=data["name"],
        version=data["version"],
        description=data["description"],
        manifest=data,
        manifest_path=manifest_path,
        handler=handler,
        input_validator=Draft202012Validator(data["input_schema"]),
        output_validator=Draft202012Validator(data["output_schema"]),
        tool_names=tuple(data["tools"]),
        timeout_ms=int(data["timeout_ms"]),
        idempotent=bool(data["idempotent"]),
        requires_approval=bool(permissions["requires_approval"]),
        effects=tuple(permissions["effects"]),
        scopes={key: tuple(values) for key, values in permissions["scopes"].items()},
        declared_secrets=tuple(permissions["secrets"]),
        cost_class=data["cost_class"],
        origin=origin,
        digest=digest,
    )


def _load_plugins(
    root: Path,
    loaded: dict[str, LoadedSkill],
    origins: dict[str, str],
    violations: list[Violation],
) -> None:
    """Discover installed packages after built-ins, pinned by digest."""

    if not root.exists():
        return
    if not root.is_dir():
        violations.append(
            Violation("*", "", f"plugin root {str(root)!r} is not a directory")
        )
        return
    try:
        pins = read_plugin_lock(root)
    except PluginPackageError as exc:
        violations.append(Violation("*", "", str(exc)))
        pins = {}

    allow_unpinned = os.environ.get(PLUGIN_ALLOW_UNPINNED_ENV, "").strip() == "1"
    candidates: list[Path] = []
    for candidate in sorted(root.iterdir(), key=lambda item: item.name):
        if candidate.name.startswith((".", "_")):
            continue
        if candidate.is_file() and candidate.suffix.lower() != ".zip":
            continue  # plugins.lock.json and operator scratch files
        candidates.append(candidate)

    for candidate in candidates:
        label = candidate.name
        try:
            package = inspect_plugin_package(candidate)
        except PluginPackageError as exc:
            violations.append(Violation(label, "", str(exc)))
            continue

        pin = pins.get(package.name)
        if pin is None:
            if not allow_unpinned:
                violations.append(
                    Violation(
                        package.name,
                        "",
                        f"plugin package {str(candidate)!r} is not pinned in "
                        f"{PLUGIN_LOCK_FILENAME}; install it with "
                        f"`python -m skills.cli install <package>` or set "
                        f"{PLUGIN_ALLOW_UNPINNED_ENV}=1 for a local experiment",
                    )
                )
                continue
        else:
            if pin["digest"] != package.digest:
                violations.append(
                    Violation(
                        package.name,
                        "",
                        f"digest mismatch for plugin {package.name!r} at {str(candidate)!r}: "
                        f"pinned {pin['digest']}, computed {package.digest}; refusing to start",
                    )
                )
                continue
            if pin["version"] != package.version:
                violations.append(
                    Violation(
                        package.name,
                        "/version",
                        f"pinned version {pin['version']!r} does not match plugin "
                        f"{package.name!r} version {package.version!r} at {str(candidate)!r}",
                    )
                )
                continue

        if package.name in loaded:
            violations.append(
                Violation(
                    package.name,
                    "/name",
                    f"duplicate skill name {package.name!r}; provided by "
                    f"{origins.get(package.name, 'an earlier package')!r} and plugin "
                    f"package {str(candidate)!r}",
                )
            )
            continue

        materialized = materialize_plugin_package(package)
        module_key = re.sub(r"[^0-9A-Za-z_]", "_", package.name) + "_" + package.digest[7:19]
        handler, entry_violations = _check_entry(
            package.manifest,
            package.name,
            base_dir=materialized,
            module_key=module_key,
        )
        if entry_violations:
            violations.extend(entry_violations)
            continue

        skill = _build_skill(
            package.manifest,
            materialized / MANIFEST_FILENAME,
            handler,  # type: ignore[arg-type]
            origin=str(candidate),
            digest=package.digest,
        )
        loaded[skill.name] = skill
        origins[skill.name] = str(candidate)


class SkillRegistry:
    """Immutable set of loaded skills plus the validated invocation pipeline.

    ``policy`` and ``audit`` are optional registry-wide defaults. A per-call
    ``invoke(..., policy=…)`` lets a deployment tighten policy without a
    restart; the manifest's declared scope can never be widened.
    """

    def __init__(
        self,
        skills: Mapping[str, LoadedSkill],
        root: Path,
        *,
        policy: "PolicyConfig | PolicyEngine | None" = None,
        audit: "AuditLog | None" = None,
        plugin_root: str | Path | None = None,
    ):
        self.root = Path(root)
        self.plugin_root = Path(plugin_root) if plugin_root is not None else None
        self._skills: dict[str, LoadedSkill] = dict(skills)
        self._by_tool: dict[str, LoadedSkill] = {
            tool: skill for skill in self._skills.values() for tool in skill.tool_names
        }
        self._policy = policy
        self._audit = audit

    # -- discovery ---------------------------------------------------------

    @classmethod
    def load(
        cls,
        root: str | Path | None = None,
        *,
        policy: "PolicyConfig | PolicyEngine | None" = None,
        audit: "AuditLog | None" = None,
        plugin_root: str | Path | None = None,
    ) -> "SkillRegistry":
        root_path = Path(root) if root is not None else DEFAULT_SKILL_ROOT
        if not root_path.is_dir():
            raise SkillManifestError(
                root_path,
                [Violation("*", "", f"skill root {str(root_path)!r} is missing or not a directory")],
            )

        dirs = sorted(
            p
            for p in root_path.iterdir()
            if p.is_dir() and not p.name.startswith((".", "_"))
        )
        if not dirs:
            raise SkillManifestError(
                root_path, [Violation("*", "", f"no skill directories found under {str(root_path)!r}")]
            )

        violations: list[Violation] = []
        loaded: dict[str, LoadedSkill] = {}
        origins: dict[str, str] = {}

        for skill_dir in dirs:
            dirname = skill_dir.name
            manifest_path = skill_dir / MANIFEST_FILENAME
            if not manifest_path.is_file():
                violations.append(Violation(dirname, "", f"{MANIFEST_FILENAME} is missing"))
                continue

            data, read_violations = _read_manifest(manifest_path, dirname)
            violations.extend(read_violations)
            if data is None:
                continue

            label = _label(data, dirname)
            structural = list(_META_VALIDATOR.iter_errors(data))
            if structural:
                structural.sort(key=lambda e: (list(e.absolute_path), e.message))
                for error in structural:
                    violations.append(
                        Violation(label, _error_pointer(error, data), error.message)
                    )
                continue

            semantic = _check_semantics(data, label)
            handler, entry_violations = _check_entry(data, label)
            semantic.extend(entry_violations)
            violations.extend(semantic)
            if semantic:
                continue

            name = data["name"]
            if name in loaded:
                violations.append(
                    Violation(
                        label,
                        "/name",
                        f"duplicate skill name {name!r}; already provided by {origins[name]!r}",
                    )
                )
                continue

            loaded[name] = _build_skill(
                data, manifest_path, handler, origin=str(skill_dir)  # type: ignore[arg-type]
            )
            origins[name] = str(skill_dir)

        # KEL-25: installed packages load after built-ins so a fresh plugin can
        # never redefine a shipped tool. Any violation here aborts startup.
        plugins_root = (
            Path(plugin_root) if plugin_root is not None else default_plugin_root()
        )
        _load_plugins(plugins_root, loaded, origins, violations)

        if violations:
            raise SkillManifestError(root_path, violations)

        return cls(loaded, root_path, policy=policy, audit=audit, plugin_root=plugins_root)

    # -- introspection -----------------------------------------------------

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._skills))

    def tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_tool))

    def get(self, skill: str) -> LoadedSkill:
        try:
            return self._skills[skill]
        except KeyError:
            raise KeyError(f"skill {skill!r} is not loaded") from None

    def __contains__(self, skill: object) -> bool:
        return skill in self._skills

    def __len__(self) -> int:
        return len(self._skills)

    # -- runtime -----------------------------------------------------------

    async def invoke(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        approval: ApprovalHook | None = None,
        secrets: Mapping[str, str] | None = None,
        workspace_root: str | Path | None = None,
        policy: "PolicyConfig | PolicyEngine | None" = None,
        audit: "AuditLog | None" = None,
        turn_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        effective_audit = audit if audit is not None else self._audit
        skill = self._by_tool.get(tool)
        if skill is None:
            _audit_invocation(
                effective_audit,
                tool=tool,
                skill_name=tool,
                version=None,
                effects=(),
                decision="deny",
                outcome="unknown_skill",
                duration_ms=_ms(started),
                turn_id=turn_id,
                actor=actor,
            )
            return _error_envelope(
                tool,
                tool,
                "unknown_skill",
                f"Unknown skill {tool!r}.",
                retryable=False,
                user_fixable=True,
                duration_ms=_ms(started),
            )

        explicit_root = Path(workspace_root) if workspace_root is not None else None
        config = _resolve_policy_config(
            policy if policy is not None else self._policy, explicit_root
        )
        invocation_policy = InvocationPolicy.from_skill(skill, config)
        env: Mapping[str, str] = os.environ if secrets is None else secrets
        inner = SkillContext(
            skill.name,
            config.workspace_root,
            {name: env[name] for name in skill.declared_secrets if name in env},
        )
        ctx = GuardedSkillContext(
            inner,
            invocation_policy,
            audit=effective_audit,
            turn_id=turn_id,
        )

        def finish(
            envelope: dict[str, Any],
            *,
            decision: str,
            outcome: str,
            policy_category: str | None = None,
            approval_hash: str | None = None,
        ) -> dict[str, Any]:
            _audit_invocation(
                effective_audit,
                tool=tool,
                skill_name=skill.name,
                version=skill.version,
                effects=skill.effects,
                decision=decision,
                outcome=outcome,
                duration_ms=_ms(started),
                turn_id=turn_id,
                actor=actor,
                policy_category=policy_category,
                approval_hash=approval_hash,
            )
            return envelope

        args: Mapping[str, Any] = {} if arguments is None else arguments
        if not isinstance(args, Mapping):
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_arguments",
                    f"Arguments for {tool!r} must be a JSON object.",
                    retryable=False,
                    user_fixable=True,
                    field="",
                    duration_ms=_ms(started),
                ),
                decision="allow",
                outcome="invalid_arguments",
            )

        arg_errors = _schema_errors(skill.input_validator, args)
        if arg_errors:
            first = arg_errors[0]
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_arguments",
                    ctx.scrub(
                        f"Invalid arguments for {tool!r}: {first['field'] or '/'}: {first['message']}"
                    ),
                    retryable=False,
                    user_fixable=True,
                    field=first["field"],
                    details={"errors": _scrub_errors(arg_errors, ctx)},
                    duration_ms=_ms(started),
                ),
                decision="allow",
                outcome="invalid_arguments",
            )

        # KEL-20: concrete arguments are checked against the manifest on every
        # call, before approval. A deny here is terminal and never produces an
        # approval request.
        try:
            verdict = invocation_policy.evaluate_arguments(args)
        except PolicyDenied as exc:
            return finish(
                _policy_denied_envelope(skill, tool, exc, ctx, _ms(started)),
                decision="deny",
                outcome="policy_denied",
                policy_category=_policy_category(exc),
            )

        if verdict.decision is Decision.DENY:
            exc = PolicyDenied(verdict.reason, verdict.blast_radius)
            return finish(
                _policy_denied_envelope(skill, tool, exc, ctx, _ms(started)),
                decision="deny",
                outcome="policy_denied",
                policy_category=_policy_category(exc),
            )

        # Policy may ask for approval (an in-scope write), but the manifest's
        # requires_approval remains the only thing that produces a prompt. A
        # policy requirement without the manifest flag is a fail-closed deny.
        if (
            verdict.decision is Decision.REQUIRE_APPROVAL
            and not skill.requires_approval
        ):
            exc = PolicyDenied(
                "policy requires approval but the skill does not declare it",
                ("approval:undeclared",),
            )
            return finish(
                _policy_denied_envelope(skill, tool, exc, ctx, _ms(started)),
                decision="deny",
                outcome="policy_denied",
                policy_category="approval",
            )

        if skill.requires_approval:
            request = _build_approval_request(skill, args)
            envelope_approval = request.to_dict()
            if approval is None:
                return finish(
                    _error_envelope(
                        skill.name,
                        tool,
                        "approval_required",
                        f"Skill {tool!r} needs approval before it can run.",
                        retryable=False,
                        user_fixable=True,
                        approval=envelope_approval,
                        duration_ms=_ms(started),
                    ),
                    decision=verdict.decision.value,
                    outcome="approval_required",
                    approval_hash=request.argument_hash,
                )
            try:
                allowed = await approval(request)
            except Exception:  # noqa: BLE001 - a broken gate must fail closed
                allowed = False
            if not allowed:
                return finish(
                    _error_envelope(
                        skill.name,
                        tool,
                        "approval_denied",
                        f"Approval for {tool!r} was denied; nothing ran.",
                        retryable=False,
                        user_fixable=False,
                        duration_ms=_ms(started),
                    ),
                    decision=verdict.decision.value,
                    outcome="approval_denied",
                    approval_hash=request.argument_hash,
                )

        try:
            with active_invocation(
                invocation_policy,
                tool=tool,
                audit=effective_audit,
                turn_id=turn_id,
            ):
                output = await asyncio.wait_for(
                    skill.handler(dict(args), ctx), timeout=skill.timeout_ms / 1000
                )
        except PolicyDenied as exc:
            return finish(
                _policy_denied_envelope(skill, tool, exc, ctx, _ms(started)),
                decision="deny",
                outcome="policy_denied",
                policy_category=_policy_category(exc),
            )
        except asyncio.TimeoutError:
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "timeout",
                    f"Skill {tool!r} exceeded its {skill.timeout_ms} ms timeout.",
                    retryable=True,
                    user_fixable=False,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="timeout",
            )
        except SkillHandlerError as exc:
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    exc.code,
                    ctx.scrub(exc.message),
                    retryable=exc.retryable,
                    user_fixable=exc.user_fixable,
                    details=_scrub_obj(exc.details, ctx) if exc.details else None,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome=exc.code,
            )
        except Exception as exc:  # noqa: BLE001 - handler failures never escape
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "handler_error",
                    ctx.scrub(f"Skill {tool!r} failed: {type(exc).__name__}: {_short(exc)}"),
                    retryable=False,
                    user_fixable=False,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="handler_error",
            )

        if not isinstance(output, Mapping):
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_output",
                    f"Skill {tool!r} returned {type(output).__name__}; a JSON object is required.",
                    retryable=False,
                    user_fixable=False,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="invalid_output",
            )
        try:
            json.dumps(output)
        except (TypeError, ValueError):
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_output",
                    f"Skill {tool!r} returned a value that is not JSON-serializable.",
                    retryable=False,
                    user_fixable=False,
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="invalid_output",
            )

        output_errors = _schema_errors(skill.output_validator, output)
        if output_errors:
            first = output_errors[0]
            return finish(
                _error_envelope(
                    skill.name,
                    tool,
                    "invalid_output",
                    ctx.scrub(
                        f"Skill {tool!r} returned output that violates its schema: "
                        f"{first['field'] or '/'}: {first['message']}"
                    ),
                    retryable=False,
                    user_fixable=False,
                    field=first["field"],
                    details={"errors": _scrub_errors(output_errors, ctx)},
                    duration_ms=_ms(started),
                ),
                decision=verdict.decision.value,
                outcome="invalid_output",
            )

        return finish(
            {
                "ok": True,
                "skill": skill.name,
                "tool": tool,
                "duration_ms": _ms(started),
                "idempotent": skill.idempotent,
                "output": dict(output),
            },
            decision=verdict.decision.value,
            outcome="ok",
        )


def _resolve_policy_config(
    policy: "PolicyConfig | PolicyEngine | None", workspace_root: Path | None
) -> PolicyConfig:
    """Normalise a per-call policy without ever widening the manifest scope."""

    if policy is None:
        return PolicyConfig(workspace_root=workspace_root or Path.cwd())
    if isinstance(policy, PolicyEngine):
        config = policy.config
    elif isinstance(policy, PolicyConfig):
        config = policy
    else:  # pragma: no cover - defensive: caller wiring bug
        raise TypeError("policy must be a PolicyConfig, PolicyEngine, or None")
    if workspace_root is not None and Path(config.workspace_root) != Path(
        workspace_root
    ):
        config = replace(config, workspace_root=workspace_root)
    # A skill's declared paths are a hard boundary. The exec gate's opt-in
    # escalation for writes outside scope must not turn a manifest escape into
    # an approvable request.
    if config.allow_write_outside_scope:
        config = replace(config, allow_write_outside_scope=False)
    return config


def _policy_category(exc: PolicyDenied) -> str:
    """Coarse category for the audit line; never the raw target."""

    for item in exc.blast_radius:
        text = str(item)
        if text.startswith(("net:", "egress:")):
            return "network"
        if text.startswith("fs:"):
            return "path"
        if text.startswith("repo:"):
            return "repo"
        if text.startswith("effect:"):
            return "effect"
        if text.startswith("approval:"):
            return "approval"
        if text.startswith("arg:"):
            return "argument"
    return "policy"


def _policy_denied_envelope(
    skill: LoadedSkill,
    tool: str,
    exc: PolicyDenied,
    ctx: SkillContext,
    duration_ms: int,
) -> dict[str, Any]:
    reason = ctx.scrub(exc.reason)
    return _error_envelope(
        skill.name,
        tool,
        "policy_denied",
        f"Policy denied {tool!r}: {reason}",
        retryable=False,
        user_fixable=False,
        details={
            "decision": "deny",
            "reason": reason,
            "blast_radius": [ctx.scrub(str(item)) for item in exc.blast_radius],
        },
        duration_ms=duration_ms,
    )


def _audit_invocation(
    audit: "AuditLog | None",
    *,
    tool: str,
    skill_name: str,
    version: str | None,
    effects: Iterable[str],
    decision: str,
    outcome: str,
    duration_ms: int,
    turn_id: str | None,
    actor: str | None,
    policy_category: str | None = None,
    approval_hash: str | None = None,
) -> None:
    """Write the content-free ``skill_invoked`` line; never fail the call."""

    if audit is None:
        return
    extra: dict[str, Any] = {
        "skill": skill_name,
        "version": version,
        "effects": list(effects),
        "scope_decision": decision,
    }
    if policy_category is not None:
        extra["policy_category"] = policy_category
    if approval_hash is not None:
        extra["approval_argument_hash"] = approval_hash
    try:
        audit.record(
            "skill_invoked",
            turn_id=turn_id,
            tool=tool,
            actor=actor,
            decision=decision,
            outcome=outcome,
            duration_ms=duration_ms,
            extra=extra,
        )
    except Exception:  # noqa: BLE001 - a full disk must not end an invocation
        return


def _schema_errors(
    validator: Draft202012Validator, instance: Any, limit: int = 10
) -> list[dict[str, str]]:
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    return [
        {"field": _ptr(error.absolute_path), "message": _short(error.message)}
        for error in errors[:limit]
    ]


def _scrub_errors(errors: list[dict[str, str]], ctx: SkillContext) -> list[dict[str, str]]:
    return [
        {"field": item["field"], "message": ctx.scrub(item["message"])} for item in errors
    ]


def _scrub_obj(value: Any, ctx: SkillContext) -> Any:
    if isinstance(value, str):
        return ctx.scrub(value)
    if isinstance(value, Mapping):
        return {key: _scrub_obj(item, ctx) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_obj(item, ctx) for item in value]
    return value


def _build_approval_request(skill: LoadedSkill, arguments: Mapping[str, Any]) -> ApprovalRequest:
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    now = datetime.now(timezone.utc)
    return ApprovalRequest(
        skill=skill.name,
        tool=skill.tool_name,
        arguments=dict(arguments),
        argument_hash="sha256:" + hashlib.sha256(canonical).hexdigest(),
        effects=skill.effects,
        scopes=skill.scopes,
        manifest_version=str(skill.manifest.get("manifest_version", SPEC_VERSION)),
        requested_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=APPROVAL_TTL_SECONDS)).isoformat(),
    )


def _error_envelope(
    skill: str,
    tool: str,
    code: str,
    message: str,
    *,
    retryable: bool,
    user_fixable: bool,
    duration_ms: int,
    field: str | None = None,
    details: Mapping[str, Any] | None = None,
    approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "user_fixable": user_fixable,
    }
    if field is not None:
        error["field"] = field
    if details:
        error["details"] = dict(details)
    envelope: dict[str, Any] = {
        "ok": False,
        "skill": skill,
        "tool": tool,
        "duration_ms": duration_ms,
        "error": error,
    }
    if approval is not None:
        envelope["approval"] = dict(approval)
    return envelope


def load_registry(
    root: str | Path | None = None, *, plugin_root: str | Path | None = None
) -> SkillRegistry:
    """Convenience wrapper used at startup: ``registry = load_registry()``."""

    return SkillRegistry.load(root, plugin_root=plugin_root)
