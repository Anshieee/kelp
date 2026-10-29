"""Command line for the KEL-9 demo."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from demo.config import DemoPaths
from demo.errors import DemoError
from demo.runner import TOTAL_BUDGET_SECONDS, Demo

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m demo",
        description=(
            "Run the six-beat KEL-9 demo against recorded fixtures. "
            "No network, no API keys; exits 1 if any beat fails."
        ),
    )
    parser.add_argument(
        "--state",
        default=None,
        help="state directory (default: var/demo; recreated contents are reused)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON summary instead of progress lines",
    )
    args = parser.parse_args(argv)

    quiet = args.json
    console = (lambda line: None) if quiet else (lambda line: print(line, flush=True))
    paths = DemoPaths.build(args.state)
    if not quiet:
        console("KEL-9 demo: 6 beats, recorded fixtures, no network")
        console(f"state: {paths.state}")
        console("")

    demo: Demo | None = None
    try:
        demo = Demo(paths, console=console)
        beats = demo.run()
    except DemoError as exc:
        print(f"\nFAILED beat {exc.beat}: {exc.message}", file=sys.stderr)
        print(f"report: {paths.report}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a crash is a failed demo
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "state": str(paths.state),
                    "report": str(paths.report),
                    "beats": [
                        {
                            "beat": beat.number,
                            "name": beat.name,
                            "duration_ms": beat.duration_ms,
                            "checks": beat.checks,
                            "data": beat.data,
                        }
                        for beat in beats
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        total_ms = sum(beat.duration_ms for beat in beats)
        checks = sum(len(beat.checks) for beat in beats)
        console("")
        console(f"all {len(beats)} beats passed: {checks} checks, {total_ms} ms")
        console(f"budget {TOTAL_BUDGET_SECONDS:.0f} s; receipts in {paths.receipts}")
        console(f"report: {paths.report}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
