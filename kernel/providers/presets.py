"""Documented example provider presets.

This module is not a second provider implementation: it holds the one set of
vendor model ids and list prices the runtime uses as *examples*. Everything the
runtime needs is still configured through ``KernelConfig`` / ``KELP_*``; an
operator pointing at another OpenAI-compatible service should override the model
ids, and optionally the prices, with that service's published values.

The values below are the OpenRouter/Nemotron pair this repository documents. They
are examples, not a requirement: any OpenAI-compatible model id works.
"""

from __future__ import annotations

__all__ = [
    "EXAMPLE_BACKGROUND_MODEL",
    "EXAMPLE_PRICING_USD_PER_MILLION",
    "EXAMPLE_PRIMARY_MODEL",
]

#: Example conversational model: multimodal, documents and screenshots.
EXAMPLE_PRIMARY_MODEL = "nvidia/nemotron-3.5-super-vl"

#: Example cheap model for titles, summaries, compaction, and background work.
EXAMPLE_BACKGROUND_MODEL = "nvidia/nemotron-3.5-lightning"

#: Example USD per one million tokens, from the documented provider's public
#: model list (``https://openrouter.ai/api/v1/models``, read 2026-11-02).
#: A model absent here has no price, so its cost stays unknown rather than
#: becoming a fabricated number. Override or extend with ``KELP_PRICING_JSON``.
EXAMPLE_PRICING_USD_PER_MILLION: dict[str, tuple[float, float]] = {
    "nvidia/nemotron-3.5-super-vl": (0.15, 0.50),
    "nvidia/nemotron-3.5-lightning": (0.07, 0.20),
}
