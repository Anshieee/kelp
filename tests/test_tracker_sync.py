"""The tracker copies agree: the written record and the embedded picture.

The runbook requires one pass to update `docs/SDLC_MINDMAP.md`, the Graphviz
source, the rendered SVG and PNG, and the copy of the SVG embedded in
`netlify/index.html`. Only the first and last of those are text that a test can
compare cheaply, so this file checks the two ways they drift: a regenerated
SVG that was never pasted into the page, and a snapshot date left behind in one
record after the other moved.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MINDMAP_MD = ROOT / "docs" / "SDLC_MINDMAP.md"
SVG = ROOT / "docs" / "sdlc-mindmap.svg"
PAGE = ROOT / "netlify" / "index.html"


def _graph_body(text: str) -> list[str]:
    """The graph markup only, without the root svg element, title, or indent."""

    start = text.index('<g id="graph0"')
    end = text.rindex("</svg>")
    body = text[start:end].splitlines()
    return [line.strip() for line in body if line.strip()]


def test_embedded_map_matches_the_rendered_svg() -> None:
    svg_body = _graph_body(SVG.read_text(encoding="utf-8"))
    page_body = _graph_body(PAGE.read_text(encoding="utf-8"))
    assert page_body == svg_body, "paste the new rendering into netlify/index.html"


def test_snapshot_date_agrees_between_the_record_and_the_site() -> None:
    record = MINDMAP_MD.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    date = next(
        line.split(":", 1)[1].strip()
        for line in record.splitlines()
        if line.startswith("Snapshot:")
    )
    assert date
    assert f"Snapshot {date}" in page
