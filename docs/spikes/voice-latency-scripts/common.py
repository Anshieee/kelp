"""Shared helpers for the Kelp voice-latency spike."""
from __future__ import annotations

import os
import statistics
from pathlib import Path

# env.sh sets SPIKE_DIR to this folder; models/, audio/ and results/ live under it.
SPIKE = Path(os.environ.get("SPIKE_DIR") or Path(__file__).resolve().parent)
AUDIO = SPIKE / "audio"
MODELS = SPIKE / "models"
RESULTS = SPIKE / "results"

UTTERANCES = {
    "short3s": "Hey Kelp, when does my next meeting start today?",
    "long8s": (
        "I have been working on this project all morning and I am a little stuck, "
        "so could you remind me to take a short break and then help me look at the failing test?"
    ),
}


def pct(values: list[float], q: float) -> float:
    """Return the q-th percentile (0-100) using linear interpolation."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(values: list[float]) -> dict[str, float]:
    """Median / p90 / min / max of a list of samples."""
    return {
        "n": len(values),
        "median": statistics.median(values),
        "p90": pct(values, 90),
        "min": min(values),
        "max": max(values),
    }
