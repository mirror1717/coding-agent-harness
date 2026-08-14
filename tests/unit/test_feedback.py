"""Tests for the feedback engine."""

from __future__ import annotations

import pytest

from coding_agent_harness.feedback import (
    EmptySecretDetector,
    FeedbackEngine,
)


class FakeSecretDetector:
    def __init__(self, secret: str = "sk-SECRET1234567890123456789") -> None:
        self._secret = secret

    def contains_secret(self, value: str | bytes) -> bool:
        if isinstance(value, bytes):
            return self._secret.encode() in value
        return self._secret in value


class TestPytestParsing:
    def test_pytest_failure_extracts_locations(self) -> None:
        stdout = (
            "test_app.py::test_add FAILED [ 33%]\n"
            "test_app.py::test_sub FAILED [ 66%]\n"
            "test_app.py::test_mul PASSED [100%]\n"
            "=== short test summary info ===\n"
            "FAILED test_app.py::test_add - assert 1 + 1 == 3\n"
            "FAILED test_app.py::test_sub - assert 2 - 1 == 5\n"
        )
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="pytest",
            exit_code=1,
            stdout=stdout.encode(),
            stderr=b"",
        )
        assert fb.category == "pytest"
        assert "test_add" in fb.summary or "2 test" in fb.summary.lower()
        assert len(fb.locations) >= 2
        assert any("test_add" in loc for loc in fb.locations)

    def test_pytest_pass_returns_pass_summary(self) -> None:
        stdout = "test_app.py::test_add PASSED [100%]\n1 passed in 0.01s\n"
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="pytest",
            exit_code=0,
            stdout=stdout.encode(),
            stderr=b"",
        )
        assert "passed" in fb.summary.lower()

    def test_syntax_error_detected(self) -> None:
        stderr = (
            "  File 'app.py', line 5\n"
            "    def 123bad(\n"
            "        ^\n"
            "SyntaxError: invalid syntax\n"
        )
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="pytest",
            exit_code=1,
            stdout=b"",
            stderr=stderr.encode(),
        )
        assert fb.category == "pytest"
        assert "SyntaxError" in fb.summary or "syntax" in fb.summary.lower()


class TestShellParsing:
    def test_shell_failure_extracts_stderr(self) -> None:
        stderr = "ls: cannot access '/nonexistent': No such file or directory\n"
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="shell",
            exit_code=2,
            stdout=b"",
            stderr=stderr.encode(),
        )
        assert fb.category == "shell"
        assert "No such file" in fb.summary or "cannot access" in fb.summary

    def test_shell_success(self) -> None:
        stdout = "file1.txt\nfile2.txt\n"
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="shell",
            exit_code=0,
            stdout=stdout.encode(),
            stderr=b"",
        )
        assert fb.category == "shell"


class TestLintParsing:
    def test_lint_issues_extracted(self) -> None:
        stdout = (
            "src/app.py:10:5: E225 missing whitespace around operator\n"
            "src/app.py:20:1: W291 trailing whitespace\n"
        )
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="lint",
            exit_code=1,
            stdout=stdout.encode(),
            stderr=b"",
        )
        assert fb.category == "lint"
        assert len(fb.locations) == 2
        assert "app.py:10" in fb.locations[0]


class TestUnknownBytes:
    def test_binary_output_uses_degraded_feedback(self) -> None:
        binary_data = bytes(range(256)) * 10
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="pytest",
            exit_code=1,
            stdout=binary_data,
            stderr=b"",
        )
        assert fb.category == "pytest"
        assert fb.truncated or fb.summary  # doesn't crash


class TestTruncation:
    def test_large_output_truncated(self) -> None:
        huge = "x" * 200_000
        engine = FeedbackEngine(max_bytes=1024)
        fb = engine.parse(
            action_id="act-1",
            action_type="shell",
            exit_code=0,
            stdout=huge.encode(),
            stderr=b"",
        )
        assert fb.truncated is True

    def test_normal_output_not_truncated(self) -> None:
        stdout = "hello world\n"
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="shell",
            exit_code=0,
            stdout=stdout.encode(),
            stderr=b"",
        )
        assert fb.truncated is False


class TestRedaction:
    def test_token_pattern_redacted(self) -> None:
        stdout = "sk-abcdefghijklmnopqrstuvwxyz1234567890\n"
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="shell",
            exit_code=0,
            stdout=stdout.encode(),
            stderr=b"",
        )
        assert "sk-abcdef" not in fb.summary
        assert "[REDACTED]" in fb.summary

    def test_exact_secret_redacted(self) -> None:
        secret = "sk-MYSECRET1234567890123456"
        stdout = f"Using key: {secret}\n"
        engine = FeedbackEngine(secret_detector=FakeSecretDetector(secret=secret))
        fb = engine.parse(
            action_id="act-1",
            action_type="shell",
            exit_code=0,
            stdout=stdout.encode(),
            stderr=b"",
        )
        assert secret not in fb.summary
        assert "REDACTED" in fb.summary

    def test_ghp_token_redacted(self) -> None:
        stdout = "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB\n"
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="shell",
            exit_code=0,
            stdout=stdout.encode(),
            stderr=b"",
        )
        assert "ghp_abcdef" not in fb.summary


class TestArtifactRefs:
    def test_artifact_refs_preserved(self) -> None:
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="pytest",
            exit_code=0,
            stdout=b"1 passed\n",
            stderr=b"",
            artifact_refs=("art-1", "art-2"),
        )
        assert fb.artifact_refs == ("art-1", "art-2")


class TestParseError:
    def test_parse_error_marked(self) -> None:
        engine = FeedbackEngine()
        fb = engine.parse(
            action_id="act-1",
            action_type="unknown_type",
            exit_code=1,
            stdout=b"some output",
            stderr=b"",
        )
        assert fb.category == "unknown"
