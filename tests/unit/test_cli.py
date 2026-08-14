"""Tests for the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from coding_agent_harness.cli import app

runner = CliRunner()


class TestDoctor:
    def test_doctor_runs(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["doctor", "--workspace", str(tmp_path)])
        assert result.exit_code in (0, 1)

    def test_doctor_fails_without_docker(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["doctor", "--workspace", str(tmp_path)])
        assert "Docker" in result.output


class TestCredentials:
    def test_status_shows_no_secret(self) -> None:
        result = runner.invoke(app, ["credentials", "status", "openai"])
        assert result.exit_code == 0
        assert "openai" in result.output
        assert "sk-" not in result.output

    def test_set_uses_hidden_input(self) -> None:
        result = runner.invoke(app, ["credentials", "set", "openai"], input="secret-key\n")
        assert result.exit_code == 0

    def test_clear_runs(self) -> None:
        result = runner.invoke(app, ["credentials", "clear", "openai"])
        assert result.exit_code == 0


class TestAuditVerify:
    def test_verify_missing_file(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["audit", "verify", str(tmp_path / "missing.jsonl")])
        assert result.exit_code == 1

    def test_verify_valid_chain(self, tmp_path: Path) -> None:
        from coding_agent_harness.audit import AuditLog
        audit_path = tmp_path / "audit.jsonl"
        log = AuditLog(audit_path, run_id="run-1")
        log.append(
            event_type="session.created",
            state="PREPARING",
            action_id="",
            source="SYSTEM",
            payload={},
        )
        log.close()
        result = runner.invoke(app, ["audit", "verify", str(audit_path)])
        assert result.exit_code == 0
        assert "Chain valid" in result.output


class TestSummary:
    def test_summary_missing_file(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["summary", str(tmp_path / "missing.jsonl")])
        assert result.exit_code == 1


class TestDemo:
    def test_demo_missing_manifest(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["demo", str(tmp_path)])
        assert result.exit_code == 1

    def test_demo_with_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.yaml").write_text("name: test\n")
        result = runner.invoke(app, ["demo", str(tmp_path)])
        assert result.exit_code == 0


class TestHelp:
    def test_help_lists_commands(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output
        assert "doctor" in result.output
        assert "demo" in result.output
        assert "credentials" in result.output
        assert "audit" in result.output
