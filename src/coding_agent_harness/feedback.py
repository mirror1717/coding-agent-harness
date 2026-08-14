"""Feedback engine that parses raw artifacts into bounded, redacted structured feedback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from coding_agent_harness.domain import StructuredFeedback

_MAX_FEEDBACK_BYTES = 65_536
_TOKEN_PATTERN = re.compile(r"(?i)(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36}|AKIA[A-Z0-9]{16})")
_PYTEST_FAILED_RE = re.compile(r"FAILED\s+(\S+)(?:\s+-\s.+)?$", re.MULTILINE)
_PYTEST_ERROR_RE = re.compile(r"ERROR\s+(\S+)(?:\s+-\s.+)?$", re.MULTILINE)
_PYTEST_VERBOSE_FAILED_RE = re.compile(r"^(\S+)\s+FAILED\b", re.MULTILINE)
_PYTEST_SHORT_TEST_SUMMARY = re.compile(r"^={3,}\s*short test summary info\s*={3,}$", re.MULTILINE)
_SYNTAX_ERROR_RE = re.compile(
    r"(?:SyntaxError|IndentationError|TabError):\s*(.+?)(?:\s*\^|$)",
    re.MULTILINE,
)
_LINT_RE = re.compile(r"^(.+?):(\d+)(?::(\d+))?:\s*(\w+):\s*(.+)$", re.MULTILINE)


class SecretDetector(Protocol):
    def contains_secret(self, value: str | bytes) -> bool: ...


class EmptySecretDetector:
    def contains_secret(self, value: str | bytes) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ParsedFeedback:
    category: str
    exit_code: int | None
    summary: str
    locations: tuple[str, ...]
    error_signature: str | None
    parse_error: bool


def _redact(text: str, detector: SecretDetector) -> str:
    redacted = _TOKEN_PATTERN.sub("[REDACTED]", text)
    if detector.contains_secret(redacted):
        lines = redacted.split("\n")
        safe_lines = [
            line if not detector.contains_secret(line) else "[REDACTED LINE]"
            for line in lines
        ]
        redacted = "\n".join(safe_lines)
    return redacted


def _truncate(text: str, limit: int = _MAX_FEEDBACK_BYTES) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    truncated = encoded[:limit].decode("utf-8", errors="ignore")
    return truncated, True


def _parse_pytest(stdout: str, stderr: str, exit_code: int | None) -> ParsedFeedback:
    locations: list[str] = []
    combined = stdout + "\n" + stderr

    for match in _PYTEST_FAILED_RE.finditer(combined):
        locations.append(match.group(1).strip())
    for match in _PYTEST_VERBOSE_FAILED_RE.finditer(combined):
        loc = match.group(1).strip()
        if loc not in locations:
            locations.append(loc)
    for match in _PYTEST_ERROR_RE.finditer(combined):
        loc = match.group(1).strip()
        if loc not in locations:
            locations.append(loc)

    if exit_code is not None and exit_code != 0:
        summary_lines: list[str] = []
        in_summary = False
        for line in combined.splitlines():
            if _PYTEST_SHORT_TEST_SUMMARY.match(line):
                in_summary = True
                continue
            if in_summary:
                if line.strip():
                    summary_lines.append(line.strip())
                else:
                    break
        if summary_lines:
            summary = "; ".join(summary_lines[:10])
        elif locations:
            summary = f"{len(locations)} test(s) failed"
        else:
            summary = f"pytest exited with code {exit_code}"
    else:
        summary = "pytest passed"

    sig = None
    if locations:
        sig = "|".join(sorted(locations)[:5])

    return ParsedFeedback(
        category="pytest",
        exit_code=exit_code,
        summary=summary,
        locations=tuple(locations),
        error_signature=sig,
        parse_error=False,
    )


def _parse_syntax_error(stdout: str, stderr: str, exit_code: int | None) -> ParsedFeedback:
    combined = stdout + "\n" + stderr
    match = _SYNTAX_ERROR_RE.search(combined)
    if match:
        msg = match.group(1).strip()
        return ParsedFeedback(
            category="pytest",
            exit_code=exit_code,
            summary=f"SyntaxError: {msg}",
            locations=(),
            error_signature=f"SyntaxError:{msg}",
            parse_error=False,
        )
    return ParsedFeedback(
        category="pytest",
        exit_code=exit_code,
        summary="syntax error detected",
        locations=(),
        error_signature="SyntaxError",
        parse_error=False,
    )


def _parse_shell(stdout: str, stderr: str, exit_code: int | None) -> ParsedFeedback:
    combined = stderr.strip() or stdout.strip()
    lines = combined.splitlines()[:5] if combined else []
    summary = "; ".join(lines) if lines else f"shell exited with code {exit_code}"
    return ParsedFeedback(
        category="shell",
        exit_code=exit_code,
        summary=summary,
        locations=(),
        error_signature=f"exit:{exit_code}" if exit_code else None,
        parse_error=False,
    )


def _parse_lint(stdout: str, stderr: str, exit_code: int | None) -> ParsedFeedback:
    combined = stdout + "\n" + stderr
    locations: list[str] = []
    for match in _LINT_RE.finditer(combined):
        path = match.group(1)
        line = match.group(2)
        col = match.group(3) or ""
        severity = match.group(4)
        msg = match.group(5)
        loc = f"{path}:{line}"
        if col:
            loc += f":{col}"
        locations.append(f"{loc} [{severity}] {msg}")

    summary = f"{len(locations)} lint issue(s)" if locations else "lint passed"
    return ParsedFeedback(
        category="lint",
        exit_code=exit_code,
        summary=summary,
        locations=tuple(locations[:20]),
        error_signature=None,
        parse_error=False,
    )


def _detect_category(action_type: str) -> str:
    if action_type == "pytest":
        return "pytest"
    if action_type == "shell":
        return "shell"
    if action_type in ("lint", "mypy", "ruff"):
        return "lint"
    return "unknown"


class FeedbackEngine:
    """Parse raw execution artifacts into bounded, redacted StructuredFeedback."""

    def __init__(
        self,
        *,
        secret_detector: SecretDetector | None = None,
        max_bytes: int = _MAX_FEEDBACK_BYTES,
    ) -> None:
        self._detector = secret_detector or EmptySecretDetector()
        self._max_bytes = max_bytes

    def parse(
        self,
        *,
        action_id: str,
        action_type: str,
        exit_code: int | None,
        stdout: bytes,
        stderr: bytes,
        artifact_refs: tuple[str, ...] = (),
        feedback_id: str = "",
    ) -> StructuredFeedback:
        try:
            stdout_str = stdout.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            stdout_str = ""
        try:
            stderr_str = stderr.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            stderr_str = ""

        category = _detect_category(action_type)

        try:
            if category == "pytest":
                if exit_code is not None and exit_code != 0:
                    parsed = _parse_pytest(stdout_str, stderr_str, exit_code)
                    if not parsed.locations and not parsed.error_signature:
                        parsed = _parse_syntax_error(stdout_str, stderr_str, exit_code)
                else:
                    parsed = _parse_pytest(stdout_str, stderr_str, exit_code)
            elif category == "shell":
                parsed = _parse_shell(stdout_str, stderr_str, exit_code)
            elif category == "lint":
                parsed = _parse_lint(stdout_str, stderr_str, exit_code)
            else:
                parsed = ParsedFeedback(
                    category="unknown",
                    exit_code=exit_code,
                    summary=f"action type '{action_type}' completed",
                    locations=(),
                    error_signature=None,
                    parse_error=False,
                )
        except (ValueError, KeyError, IndexError, AttributeError):
            parsed = ParsedFeedback(
                category=category,
                exit_code=exit_code,
                summary="feedback parse error",
                locations=(),
                error_signature=None,
                parse_error=True,
            )

        summary = _redact(parsed.summary, self._detector)
        locations = tuple(_redact(loc, self._detector) for loc in parsed.locations)

        combined = summary
        if locations:
            combined += "\n" + "\n".join(locations)
        combined, truncated = _truncate(combined, self._max_bytes)

        return StructuredFeedback(
            feedback_id=feedback_id,
            action_id=action_id,
            category=parsed.category,
            summary=combined,
            locations=locations,
            error_signature=parsed.error_signature,
            truncated=truncated or parsed.parse_error,
            artifact_refs=artifact_refs,
        )
