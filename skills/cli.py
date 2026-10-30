"""Deploy-time plugin package management (KEL-25 / M4-D).

Usage::

    python -m skills.cli install path/to/plugin
    python -m skills.cli list
    python -m skills.cli uninstall plugins.echo

This is an operator tool, run before the process starts. It is deliberately
**not** a model-facing skill and MUST NOT be reachable from chat. Installing a
package pins its manifest name, version, and SHA-256 digest in
``<root>/plugins.lock.json``; the loader refuses to start when the bytes on
disk no longer match the pin.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .loader import (
    DEFAULT_SKILL_ROOT,
    MANIFEST_FILENAME,
    PLUGIN_LOCK_FILENAME,
    PluginPackage,
    PluginPackageError,
    default_plugin_root,
    inspect_plugin_package,
    read_plugin_lock,
    write_plugin_lock,
)

__all__ = ["install", "list_plugins", "main", "uninstall"]


def _resolve_root(root: str | Path | None) -> Path:
    return Path(root) if root is not None else default_plugin_root()


def _builtin_names() -> dict[str, str]:
    """Map built-in skill name -> its directory, without importing handlers."""

    names: dict[str, str] = {}
    if not DEFAULT_SKILL_ROOT.is_dir():
        return names
    for directory in sorted(DEFAULT_SKILL_ROOT.iterdir()):
        manifest = directory / MANIFEST_FILENAME
        if not directory.is_dir() or directory.name.startswith((".", "_")):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, Mapping) and isinstance(data.get("name"), str):
            names[data["name"]] = str(directory)
    return names


def _copy_package(package: PluginPackage, destination: Path) -> None:
    """Copy the package verbatim so the boot-time digest matches the pin."""

    if package.source.is_dir():
        shutil.copytree(
            package.source,
            destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
    else:
        shutil.copy2(package.source, destination)


def install(
    package: str | Path,
    *,
    root: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Validate, copy, and pin one directory or zip package."""

    root_path = _resolve_root(root)
    inspected = inspect_plugin_package(package)
    builtins = _builtin_names()
    if inspected.name in builtins:
        raise PluginPackageError(
            f"plugin {inspected.name!r} from {str(inspected.source)!r} would shadow "
            f"built-in skill {inspected.name!r} at {builtins[inspected.name]!r}"
        )

    root_path.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if inspected.source.is_file() else ""
    destination = root_path / (inspected.name + suffix)
    for candidate in _installed_packages(root_path):
        if candidate == destination:
            continue
        try:
            existing_name = inspect_plugin_package(candidate).name
        except PluginPackageError:
            existing_name = candidate.name
        if existing_name != inspected.name:
            continue
        if not force:
            raise PluginPackageError(
                f"{inspected.name!r} is already installed at {str(candidate)!r}; "
                "re-run with --force to replace it"
            )
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
    if destination.exists():
        if not force:
            raise PluginPackageError(
                f"{inspected.name!r} is already installed at {str(destination)!r}; "
                "re-run with --force to replace it"
            )
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()

    try:
        _copy_package(inspected, destination)
        pins = read_plugin_lock(root_path)
        pins[inspected.name] = {
            "name": inspected.name,
            "version": inspected.version,
            "digest": inspected.digest,
        }
        write_plugin_lock(root_path, pins)
    except Exception:
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination, ignore_errors=True)
        elif destination.exists():
            destination.unlink(missing_ok=True)
        raise

    return {
        "name": inspected.name,
        "version": inspected.version,
        "digest": inspected.digest,
        "path": str(destination),
        "root": str(root_path),
    }


def _installed_packages(root_path: Path) -> list[Path]:
    if not root_path.is_dir():
        return []
    candidates: list[Path] = []
    for candidate in sorted(root_path.iterdir(), key=lambda item: item.name):
        if candidate.name.startswith((".", "_")) or candidate.name == PLUGIN_LOCK_FILENAME:
            continue
        if candidate.is_file() and candidate.suffix.lower() != ".zip":
            continue
        candidates.append(candidate)
    return candidates


def uninstall(name: str, *, root: str | Path | None = None) -> dict[str, Any]:
    """Remove a plugin package and its pin. Unknown names are an error."""

    root_path = _resolve_root(root)
    pins = read_plugin_lock(root_path)
    removed: list[str] = []
    for candidate in _installed_packages(root_path):
        try:
            package_name = inspect_plugin_package(candidate).name
        except PluginPackageError:
            package_name = candidate.name
        if package_name != name and candidate.name != name:
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        removed.append(str(candidate))

    if not removed and name not in pins:
        raise PluginPackageError(
            f"plugin {name!r} is not installed under {str(root_path)!r}"
        )
    if name in pins:
        pins.pop(name)
        write_plugin_lock(root_path, pins)
    return {"name": name, "removed": removed, "root": str(root_path)}


def list_plugins(*, root: str | Path | None = None) -> list[dict[str, Any]]:
    """Describe every package under the plugin root, pinned or not."""

    root_path = _resolve_root(root)
    pins = read_plugin_lock(root_path)
    rows: list[dict[str, Any]] = []
    for candidate in _installed_packages(root_path):
        row: dict[str, Any] = {"path": str(candidate)}
        try:
            package = inspect_plugin_package(candidate)
        except PluginPackageError as exc:
            row.update(
                {
                    "name": candidate.stem,
                    "version": "?",
                    "digest": None,
                    "pinned": False,
                    "valid": False,
                    "error": str(exc),
                }
            )
            rows.append(row)
            continue
        pin = pins.get(package.name)
        row.update(
            {
                "name": package.name,
                "version": package.version,
                "digest": package.digest,
                "pinned": pin is not None,
                "pin_matches": pin is not None and pin["digest"] == package.digest,
                "valid": True,
                "root": str(root_path),
            }
        )
        rows.append(row)
    return rows


def _format_row(row: Mapping[str, Any]) -> str:
    if not row.get("valid"):
        return f"{row['name']}\tunreadable\t{row['error']}"
    if not row.get("pinned"):
        status = "unpinned"
    elif row.get("pin_matches"):
        status = "pinned"
    else:
        status = "DIGEST MISMATCH"
    return f"{row['name']}\t{row['version']}\t{row['digest']}\t{status}\t{row['path']}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m skills.cli",
        description="Deploy-time plugin package management (not a chat tool).",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    install_parser = subcommands.add_parser("install", help="validate, copy, and pin a package")
    install_parser.add_argument("package")
    install_parser.add_argument("--force", action="store_true", help="replace an existing install")
    install_parser.add_argument("--root", default=None, help="plugin root (default: KELP_PLUGIN_ROOT or var/plugins)")

    uninstall_parser = subcommands.add_parser("uninstall", help="remove a package and its pin")
    uninstall_parser.add_argument("name")
    uninstall_parser.add_argument("--root", default=None)

    list_parser = subcommands.add_parser("list", help="show installed packages and pin status")
    list_parser.add_argument("--root", default=None)
    list_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            result = install(args.package, root=args.root, force=args.force)
            print(
                f"installed {result['name']} {result['version']} "
                f"{result['digest']} -> {result['path']}"
            )
            return 0
        if args.command == "uninstall":
            result = uninstall(args.name, root=args.root)
            print(f"uninstalled {result['name']} from {result['root']}")
            return 0
        if args.command == "list":
            rows = list_plugins(root=args.root)
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            elif not rows:
                print(f"no plugins installed under {_resolve_root(args.root)}")
            else:
                print(f"plugin root: {_resolve_root(args.root)}")
                for row in rows:
                    print(_format_row(row))
            return 0
    except PluginPackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2  # pragma: no cover - argparse enforces a known command


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
