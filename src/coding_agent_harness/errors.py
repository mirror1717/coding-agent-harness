"""Error hierarchy for the Governed Coding Agent Harness."""
from __future__ import annotations


class HarnessError(Exception):
    """Base error for all harness exceptions."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class ValidationError(HarnessError):
    """Action schema or parameter validation errors."""


class ConfigurationError(HarnessError):
    """Invalid policy, profile, or configuration errors."""


class SecurityError(HarnessError):
    """Security boundary violations and integrity failures."""


class EvidenceError(HarnessError):
    """Missing or corrupted evidence (artifacts, audit)."""
