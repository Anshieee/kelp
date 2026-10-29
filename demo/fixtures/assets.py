"""Recorded inputs the demo plays back: fixture files and saved responses.

Everything is read from ``demo/fixtures/files`` and cached for the run. A
missing file is a packaging bug, so these helpers raise rather than return an
empty result.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["ASSETS", "csv_bytes", "github", "pdf_bytes", "web_results"]

ASSETS = Path(__file__).resolve().parent / "files"


def _read_bytes(name: str) -> bytes:
    path = ASSETS / name
    if not path.is_file():
        raise FileNotFoundError(f"demo fixture is missing: {path}")
    return path.read_bytes()


@lru_cache(maxsize=None)
def _json(name: str) -> dict[str, Any]:
    value = json.loads(_read_bytes(name).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"demo fixture {name!r} must contain a JSON object")
    return value


def web_results() -> dict[str, Any]:
    return _json("web_results.json")


def github() -> dict[str, Any]:
    return _json("github.json")


def pdf_bytes() -> bytes:
    return _read_bytes("nebius-overview.pdf")


def csv_bytes() -> bytes:
    return _read_bytes("q3-revenue.csv")
