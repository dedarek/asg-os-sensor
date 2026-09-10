"""Track MCP tool-process liveness across periodic health checks.

None (cannot observe) never counts toward transport loss; only explicit
False results after a prior confirmed alive observation do.
"""
from __future__ import annotations


class ToolTransportHealth:
    """Stateful tracker; call observe() on each health-check cycle."""

    def __init__(self, threshold: int = 3) -> None:
        if threshold < 1:
            raise ValueError('threshold must be >= 1')
        self._threshold = threshold
        self._consecutive_missing = 0
        self._ever_alive = False

    def observe(self, seen_alive: bool | None) -> str:
        """Feed one observation; returns the current transport status label."""
        if seen_alive is True:
            self._ever_alive = True
            self._consecutive_missing = 0
            return 'alive'
        if seen_alive is None:
            self._consecutive_missing = 0
            return 'unknown'
        if seen_alive is not False:
            raise ValueError('seen_alive must be True, False or None')
        # seen_alive is False
        if not self._ever_alive:
            return 'never_alive'
        self._consecutive_missing += 1
        if self._consecutive_missing >= self._threshold:
            return 'transport_lost'
        return 'missing'
