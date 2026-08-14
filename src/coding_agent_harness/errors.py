"""Stable error types for harness boundaries."""

from typing import ClassVar


class HarnessError(Exception):
    """Base error carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class HarnessValidationError(HarnessError):
    ERROR_CODE: ClassVar[str] = "VALIDATION_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(self.ERROR_CODE, message)


class HarnessConfigurationError(HarnessError):
    ERROR_CODE: ClassVar[str] = "CONFIGURATION_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(self.ERROR_CODE, message)


class HarnessSecurityError(HarnessError):
    ERROR_CODE: ClassVar[str] = "SECURITY_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(self.ERROR_CODE, message)


class HarnessEvidenceError(HarnessError):
    ERROR_CODE: ClassVar[str] = "EVIDENCE_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(self.ERROR_CODE, message)
