"""Installed plugin package tests (KEL-25 / M4-D).

Covers the operator CLI, digest pinning, fail-fast boot, package safety
(traversal/absolute/symlink/size), builtin shadowing, uninstall, and the
no-``sys.path``-mutation guarantee.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

from skills import SkillManifestError, SkillRegistry, cli
from skills.errors import Violation
from skills.loader import (
    PLUGIN_LOCK_FILENAME,
    PluginPackageError,
    inspect_plugin_package,
    write_plugin_lock,
)

BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "skills" / "builtin"
HANDLER_SOURCE = '''\
"""Read-only fixture plugin handler."""

from __future__ import annotations

from typing import Any, Mapping


async def run(arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    text = str(arguments["text"])
    return {"echo": text, "chars": len(text)}
'''


def _manifest(
    *,
    name: str = "plugins.echo",
    version: str = "0.1.0",
    effects: tuple[str, ...] = ("read",),
    entry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "manifest_version": "1.0",
        "name": name,
        "version": version,
        "description": "Fixture plugin used to exercise the M4-D package loader.",
        "triggers": [
            {"kind": "model", "description": "Echo the supplied text back.", "examples": ["echo hi"]}
        ],
        "tools": [name],
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 100}},
        },
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["echo", "chars"],
            "properties": {"echo": {"type": "string"}, "chars": {"type": "integer"}},
        },
        "permissions": {
            "effects": list(effects),
            "scopes": {"paths": [], "hosts": [], "repos": []},
            "requires_approval": False,
            "secrets": [],
        },
        "entry": dict(entry) if entry is not None else {"path": "handler.py", "callable": "run"},
        "timeout_ms": 2000,
        "idempotent": True,
        "cost_class": "free",
    }


def _write_plugin(
    directory: Path,
    *,
    name: str = "plugins.echo",
    version: str = "0.1.0",
    effects: tuple[str, ...] = ("read",),
    handler_source: str = HANDLER_SOURCE,
    entry: Mapping[str, Any] | None = None,
) -> Path:
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(_manifest(name=name, version=version, effects=effects, entry=entry)),
        encoding="utf-8",
    )
    (directory / "handler.py").write_text(handler_source, encoding="utf-8")
    return directory


def _load(plugin_root: Path, **kwargs: Any) -> SkillRegistry:
    return SkillRegistry.load(BUILTIN_ROOT, plugin_root=plugin_root, **kwargs)


def _install(tmp_path: Path, **plugin_kwargs: Any) -> tuple[Path, dict[str, Any]]:
    plugin_root = tmp_path / "plugins"
    source = _write_plugin(tmp_path / "src" / "echo", **plugin_kwargs)
    return plugin_root, cli.install(source, root=plugin_root)


def _write_zip(path: Path, members: Iterable[tuple[str, bytes, int]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data, mode in members:
            info = zipfile.ZipInfo(name)
            info.external_attr = (mode & 0xFFFF) << 16
            archive.writestr(info, data)
    return path


def _zip_package(tmp_path: Path, **plugin_kwargs: Any) -> Path:
    source = _write_plugin(tmp_path / "src" / "zip_echo", **plugin_kwargs)
    return _write_zip(
        tmp_path / "plugin.zip",
        [
            ("manifest.json", (source / "manifest.json").read_bytes(), 0o644),
            ("handler.py", (source / "handler.py").read_bytes(), 0o644),
        ],
    )


@pytest.fixture(autouse=True)
def _clean_plugin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELP_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("KELP_PLUGINS_ALLOW_UNPINNED", raising=False)


# ---------------------------------------------------------------------------
# Happy path: install, list in tools(), invoke through the loop
# ---------------------------------------------------------------------------


def test_read_only_plugin_installs_lists_and_invokes(tmp_path: Path) -> None:
    plugin_root, info = _install(tmp_path)
    assert info["name"] == "plugins.echo"
    assert info["digest"].startswith("sha256:") and len(info["digest"]) == 71

    lock = json.loads((plugin_root / PLUGIN_LOCK_FILENAME).read_text(encoding="utf-8"))
    assert lock["plugins"]["plugins.echo"] == {
        "digest": info["digest"],
        "name": "plugins.echo",
        "version": "0.1.0",
    }

    registry = _load(plugin_root)
    assert "plugins.echo" in registry.tools()
    assert registry.get("plugins.echo").effects == ("read",)
    assert registry.get("plugins.echo").digest == info["digest"]

    # The plugin runs inside a normal event loop and returns the standard envelope.
    result = asyncio.run(registry.invoke("plugins.echo", {"text": "hello plugin"}))
    assert set(result) == {"ok", "skill", "tool", "duration_ms", "idempotent", "output"}
    assert result["ok"] is True
    assert result["skill"] == "plugins.echo"
    assert result["tool"] == "plugins.echo"
    assert result["idempotent"] is True
    assert result["output"] == {"echo": "hello plugin", "chars": 12}
    assert isinstance(result["duration_ms"], int)


def test_zip_package_installs_and_loads(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    info = cli.install(_zip_package(tmp_path), root=plugin_root)
    assert info["digest"].startswith("sha256:")

    registry = _load(plugin_root)
    assert "plugins.echo" in registry.tools()
    result = asyncio.run(registry.invoke("plugins.echo", {"text": "zipped"}))
    assert result["ok"] is True and result["output"] == {"echo": "zipped", "chars": 6}
    assert str(plugin_root) not in sys.path


def test_cli_list_reports_pins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugin_root, info = _install(tmp_path)
    assert cli.main(["list", "--root", str(plugin_root)]) == 0
    output = capsys.readouterr().out
    assert "plugins.echo" in output
    assert "0.1.0" in output
    assert info["digest"] in output
    assert "pinned" in output

    assert cli.main(["list", "--root", str(plugin_root), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "plugins.echo"
    assert rows[0]["pinned"] is True and rows[0]["pin_matches"] is True


def test_cli_main_install_and_uninstall(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _write_plugin(tmp_path / "src" / "echo")
    plugin_root = tmp_path / "plugins"

    assert cli.main(["install", str(source), "--root", str(plugin_root)]) == 0
    output = capsys.readouterr().out
    assert "installed plugins.echo 0.1.0 sha256:" in output

    assert cli.main(["uninstall", "plugins.echo", "--root", str(plugin_root)]) == 0
    assert "uninstalled plugins.echo" in capsys.readouterr().out
    assert not (plugin_root / "plugins.echo").exists()


# ---------------------------------------------------------------------------
# Digest pinning is fatal on boot
# ---------------------------------------------------------------------------


def test_one_byte_change_fails_boot_naming_the_plugin(tmp_path: Path) -> None:
    plugin_root, _info = _install(tmp_path)
    handler = plugin_root / "plugins.echo" / "handler.py"
    handler.write_bytes(handler.read_bytes() + b"\n")

    with pytest.raises(SkillManifestError) as info:
        _load(plugin_root)
    message = str(info.value)
    assert "plugins.echo" in message
    assert "digest mismatch" in message
    assert "refusing to start" in message


def test_unpinned_plugin_is_refused_unless_env_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = tmp_path / "plugins"
    shutil.copytree(_write_plugin(tmp_path / "src" / "echo"), plugin_root / "echo")

    with pytest.raises(SkillManifestError) as info:
        _load(plugin_root)
    assert "not pinned" in str(info.value)
    assert "KELP_PLUGINS_ALLOW_UNPINNED=1" in str(info.value)

    monkeypatch.setenv("KELP_PLUGINS_ALLOW_UNPINNED", "1")
    registry = _load(plugin_root)
    assert "plugins.echo" in registry.tools()


def test_version_pin_must_match(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    source = _write_plugin(tmp_path / "src" / "echo")
    installed = plugin_root / "echo"
    shutil.copytree(source, installed)
    package = inspect_plugin_package(installed)
    write_plugin_lock(
        plugin_root,
        {package.name: {"name": package.name, "version": "9.9.9", "digest": package.digest}},
    )
    with pytest.raises(SkillManifestError) as info:
        _load(plugin_root)
    assert "pinned version" in str(info.value)


# ---------------------------------------------------------------------------
# Package safety
# ---------------------------------------------------------------------------


def test_zip_traversal_member_rejected(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "evil.zip",
        [
            ("manifest.json", b"{}", 0o644),
            ("../escape.py", b"print('x')\n", 0o644),
        ],
    )
    with pytest.raises(PluginPackageError) as info:
        cli.install(archive, root=tmp_path / "plugins")
    assert "unsafe member" in str(info.value)
    assert ".." in str(info.value)


def test_zip_absolute_member_rejected(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "absolute.zip",
        [
            ("manifest.json", b"{}", 0o644),
            ("/etc/escape.py", b"print('x')\n", 0o644),
        ],
    )
    with pytest.raises(PluginPackageError) as info:
        cli.install(archive, root=tmp_path / "plugins")
    assert "absolute member" in str(info.value)


def test_zip_symlink_member_rejected(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "symlink.zip",
        [
            ("manifest.json", b"{}", 0o644),
            ("handler.py", b"# handler\n", 0o644),
            ("link.py", b"handler.py", stat.S_IFLNK | 0o777),
        ],
    )
    with pytest.raises(PluginPackageError) as info:
        cli.install(archive, root=tmp_path / "plugins")
    assert "symlink member" in str(info.value)


def test_directory_symlink_member_rejected(tmp_path: Path) -> None:
    source = _write_plugin(tmp_path / "src" / "echo")
    (source / "link.py").symlink_to(source / "handler.py")
    with pytest.raises(PluginPackageError) as info:
        cli.install(source, root=tmp_path / "plugins")
    assert "symlink" in str(info.value)


def test_oversized_package_rejected(tmp_path: Path) -> None:
    source = _write_plugin(
        tmp_path / "src" / "echo",
        handler_source="# padding\n" + ("x" * (8 * 1024 * 1024 + 1)),
    )
    with pytest.raises(PluginPackageError) as info:
        cli.install(source, root=tmp_path / "plugins")
    assert "exceeds" in str(info.value)
    assert str(8 * 1024 * 1024) in str(info.value)


def test_zip_oversized_package_rejected(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "big.zip",
        [("handler.py", b"x" * (8 * 1024 * 1024 + 1), 0o644)],
    )
    with pytest.raises(PluginPackageError) as info:
        cli.install(archive, root=tmp_path / "plugins")
    assert "exceeds" in str(info.value)


def test_plugin_must_use_entry_path_not_module(tmp_path: Path) -> None:
    source = _write_plugin(
        tmp_path / "src" / "echo",
        entry={"module": "os", "callable": "system"},
    )
    with pytest.raises(PluginPackageError) as info:
        cli.install(source, root=tmp_path / "plugins")
    assert "entry.path" in str(info.value)
    assert "sys.path" in str(info.value)


def test_execute_effect_rejected_for_plugins(tmp_path: Path) -> None:
    source = _write_plugin(tmp_path / "src" / "echo", effects=("execute",))
    with pytest.raises(PluginPackageError) as info:
        cli.install(source, root=tmp_path / "plugins")
    assert "execute" in str(info.value)


# ---------------------------------------------------------------------------
# Shadowing and duplicates
# ---------------------------------------------------------------------------


def test_shadowing_builtin_fails_naming_both_origins(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    source = _write_plugin(tmp_path / "src" / "shadow", name="web.search")

    with pytest.raises(PluginPackageError) as info:
        cli.install(source, root=plugin_root)
    assert "web.search" in str(info.value)
    assert "skills/builtin/web_search" in str(info.value)

    # Operators can drop a package in by hand; boot must still refuse it.
    installed = plugin_root / "web.search"
    shutil.copytree(source, installed)
    package = inspect_plugin_package(installed)
    write_plugin_lock(
        plugin_root,
        {package.name: {"name": package.name, "version": package.version, "digest": package.digest}},
    )
    with pytest.raises(SkillManifestError) as boot:
        _load(plugin_root)
    text = str(boot.value)
    assert "duplicate skill name 'web.search'" in text
    assert "skills/builtin/web_search" in text
    assert str(installed) in text


def test_duplicate_plugin_names_fail_naming_both(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    first = plugin_root / "a"
    second = plugin_root / "b"
    shutil.copytree(_write_plugin(tmp_path / "src" / "one"), first)
    shutil.copytree(_write_plugin(tmp_path / "src" / "two"), second)
    pins = {}
    for path in (first, second):
        package = inspect_plugin_package(path)
        pins[package.name] = {
            "name": package.name,
            "version": package.version,
            "digest": package.digest,
        }
    write_plugin_lock(plugin_root, pins)

    with pytest.raises(SkillManifestError) as info:
        _load(plugin_root)
    text = str(info.value)
    assert "duplicate skill name" in text
    assert str(first) in text and str(second) in text


# ---------------------------------------------------------------------------
# Uninstall and sys.path
# ---------------------------------------------------------------------------


def test_uninstall_removes_the_tool_next_boot(tmp_path: Path) -> None:
    plugin_root, _info = _install(tmp_path)
    assert "plugins.echo" in _load(plugin_root).tools()

    result = cli.uninstall("plugins.echo", root=plugin_root)
    assert result["removed"]
    assert not (plugin_root / "plugins.echo").exists()
    lock = json.loads((plugin_root / PLUGIN_LOCK_FILENAME).read_text(encoding="utf-8"))
    assert lock["plugins"] == {}

    registry = _load(plugin_root)
    assert "plugins.echo" not in registry.tools()
    assert "plugins.echo" not in registry.names()
    with pytest.raises(PluginPackageError):
        cli.uninstall("plugins.echo", root=plugin_root)


def test_loading_never_mutates_sys_path(tmp_path: Path) -> None:
    plugin_root, _info = _install(tmp_path)
    before = list(sys.path)

    registry = _load(plugin_root)
    skill = registry.get("plugins.echo")

    assert sys.path == before
    assert str(plugin_root) not in sys.path
    assert str(plugin_root / "plugins.echo") not in sys.path
    assert skill.handler.__module__.startswith("_kelp_plugin_")
    module = sys.modules[skill.handler.__module__]
    assert Path(module.__file__).name == "handler.py"
    assert Path(module.__file__).parent == plugin_root / "plugins.echo"


def test_digest_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    first = inspect_plugin_package(_write_plugin(tmp_path / "one"))
    second = inspect_plugin_package(_write_plugin(tmp_path / "two"))
    assert first.digest == second.digest

    (tmp_path / "two" / "handler.py").write_text(HANDLER_SOURCE + "\n", encoding="utf-8")
    third = inspect_plugin_package(tmp_path / "two")
    assert third.digest != first.digest


def test_invalid_plugin_manifest_reports_pointer(tmp_path: Path) -> None:
    source = _write_plugin(tmp_path / "src" / "echo")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["timeout_ms"] = 5
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PluginPackageError) as info:
        cli.install(source, root=tmp_path / "plugins")
    assert "/timeout_ms" in str(info.value)


def test_plugin_root_missing_is_not_an_error(tmp_path: Path) -> None:
    registry = _load(tmp_path / "no-plugins-here")
    assert "plugins.echo" not in registry.tools()
    assert len(registry.tools()) == 4


def test_violation_type_used_for_boot_errors(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins"
    shutil.copytree(_write_plugin(tmp_path / "src" / "echo"), plugin_root / "echo")
    with pytest.raises(SkillManifestError) as info:
        _load(plugin_root)
    assert all(isinstance(violation, Violation) for violation in info.value.violations)
