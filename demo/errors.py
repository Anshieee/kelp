"""A demo beat failed. The CLI turns this into a non-zero exit."""

from __future__ import annotations

__all__ = ["DemoError"]


class DemoError(Exception):
    def __init__(self, beat: int, message: str):
        super().__init__(f"beat {beat}: {message}")
        self.beat = beat
        self.message = message
