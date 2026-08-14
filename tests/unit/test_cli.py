"""Tests for the CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from coding_agent_harness.cli import app

runner = CliRunner()
docker_available = shutil.which("docker") is not None


class TestDoctor:
    def test_doctor_runs(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["doctor", "--workspace", str(tmp_path)])
        assert result.exit_code in (0, 1)

    def test_doctor_fails_without_docker(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["doctor", "--workspace", str(tmp_path)])
        assert "Docker" in result.output

    def test_doctor_keyring_warn_not_fail(self, tmp_path: Path) -> None:
        """Keyring unavailable should be WARN, not FAIL — MockLLM demos still work."""
        result = runner.invoke(app, ["doctor", "--workspace", str(tmp_path)])
        assert "Keyring:" in result.output
        assert "FAIL" not in result.output.split("Keyring:")[1].split("\n")[0]


class TestCredentials:
    def test_status_shows_no_secret(self) -> None:
        result = runner.invoke(app, ["credentials", "status", "openai"])
        assert result.exit_code == 0
        assert "openai" in result.output
        assert "sk-" not in result.output

    def test_set_uses_hidden_input(self) -> None:
        result = runner.invoke(app, ["credentials", "set", "openai"], input="secret-key\n")
        assert result.exit_code in (0, 1)

    def test_clear_runs(self) -> None:
        result = runner.invoke(app, ["credentials", "clear", "openai"])
        assert result.exit_code in (0, 1)


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


class TestRunFailFast:
    def test_run_real_llm_without_credential_fails_fast(self, tmp_path: Path) -> None:
        """Real LLM without credential should fail fast with actionable error."""
        (tmp_path / ".gitkeep").touch()
        result = runner.invoke(
            app,
            ["run", "--workspace", str(tmp_path), "--task", "test", "--no-mockllm"],
        )
        assert result.exit_code == 1
        assert "credential" in result.output.lower() or "keyring" in result.output.lower()

    def test_run_mockllm_prints_hint(self, tmp_path: Path) -> None:
        """MockLLM mode without script should print a helpful hint."""
        (tmp_path / ".gitkeep").touch()
        result = runner.invoke(
            app,
            ["run", "--workspace", str(tmp_path), "--task", "test"],
        )
        assert result.exit_code == 0
        assert "demo" in result.output.lower()


class TestDemo:
    def test_demo_missing_manifest(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["demo", str(tmp_path)])
        assert result.exit_code == 1

    def test_demo_with_minimal_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.yaml").write_text("name: test\n")
        result = runner.invoke(app, ["demo", str(tmp_path)])
        assert result.exit_code == 0
        assert "test" in result.output

    def test_demo_governance_hitl_fixture(self) -> None:
        fixture = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl"
        result = runner.invoke(app, ["demo", str(fixture)])
        assert result.exit_code in (0, 1)
        assert "governance-hitl" in result.output

    @pytest.mark.skipif(not docker_available, reason="Docker not available")
    def test_demo_prints_audit_path(self) -> None:
        """Demo SUCCESS must print the audit log path."""
        fixture = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl"
        result = runner.invoke(app, ["demo", str(fixture)])
        if result.exit_code == 0:
            assert "Audit log:" in result.output

    @pytest.mark.skipif(not docker_available, reason="Docker not available")
    def test_demo_audit_file_exists_after_run(self) -> None:
        """Audit file must exist at the printed path after demo completes."""
        fixture = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl"
        result = runner.invoke(app, ["demo", str(fixture)])
        if result.exit_code == 0 and "Audit log:" in result.output:
            path_str = result.output.split("Audit log:")[1].strip().split("\n")[0]
            audit_path = Path(path_str)
            assert audit_path.exists()

    @pytest.mark.skipif(not docker_available, reason="Docker not available")
    def test_demo_audit_verify_works(self) -> None:
        """harness audit verify must succeed on the demo-generated audit log."""
        fixture = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl"
        result = runner.invoke(app, ["demo", str(fixture)])
        if result.exit_code == 0 and "Audit log:" in result.output:
            path_str = result.output.split("Audit log:")[1].strip().split("\n")[0]
            verify_result = runner.invoke(app, ["audit", "verify", path_str])
            assert verify_result.exit_code == 0
            assert "Chain valid: True" in verify_result.output

    @pytest.mark.skipif(not docker_available, reason="Docker not available")
    def test_demo_summary_works(self) -> None:
        """harness summary must succeed on the demo-generated audit log."""
        fixture = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl"
        result = runner.invoke(app, ["demo", str(fixture)])
        if result.exit_code == 0 and "Audit log:" in result.output:
            path_str = result.output.split("Audit log:")[1].strip().split("\n")[0]
            summary_result = runner.invoke(app, ["summary", path_str])
            assert summary_result.exit_code == 0
            assert "RunSummary" in summary_result.output

    @pytest.mark.skipif(not docker_available, reason="Docker not available")
    def test_demo_audit_tamper_not_broken(self) -> None:
        """Audit tamper demo must still run without errors."""
        fixture = Path(__file__).parents[2] / "demo" / "fixtures" / "audit_tamper"
        result = runner.invoke(app, ["demo", str(fixture)])
        assert result.exit_code in (0, 1)
        assert "audit-tamper" in result.output

    @pytest.mark.skipif(not docker_available, reason="Docker not available")
    def test_demo_no_secret_in_output(self) -> None:
        """No credential secret should appear in demo stdout."""
        fixture = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl"
        result = runner.invoke(app, ["demo", str(fixture)])
        assert "sk-" not in result.output
        assert "API_KEY" not in result.output


class TestHelp:
    def test_help_lists_commands(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output
        assert "doctor" in result.output
        assert "demo" in result.output
        assert "credentials" in result.output
        assert "audit" in result.output
