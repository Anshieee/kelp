"""`docs.answer` handler stub — PDF/CSV question answering.

ADR: this is one skill with an internal ``kind`` discriminator (pdf|csv), not
two separate skills. See docs/adr/0001-docs-answer-single-skill.md.
"""

from __future__ import annotations

from typing import Any, Mapping

from skills.context import SkillContext
from skills.errors import SkillNotImplementedError


async def run(arguments: Mapping[str, Any], context: SkillContext) -> Mapping[str, Any]:
    """Answer a question about a local artifact.

    Contract:
      - resolve artifact handles only under ``${WORKSPACE}/var/artifacts``
      - no network access; no writes outside the workspace
      - return an answer, the artifact descriptor, and citations (page or row)
    """

    raise SkillNotImplementedError("docs.answer")
