"""KEL-9 demo: a scripted six-beat run of the prototype, offline.

Entry points::

    python -m demo
    make demo
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from .cli import main as _main

    return _main(argv)
