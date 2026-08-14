"""DEMO-3: Audit Tamper Evidence - verify demo fixture exists and is valid."""

from __future__ import annotations

from pathlib import Path

import yaml


class TestDemo3AuditTamper:
    def test_manifest_exists(self) -> None:
        manifest = Path(__file__).parents[2] / "demo" / "fixtures" / "audit_tamper" / "manifest.yaml"
        assert manifest.exists()

    def test_manifest_is_valid_yaml(self) -> None:
        manifest = Path(__file__).parents[2] / "demo" / "fixtures" / "audit_tamper" / "manifest.yaml"
        data = yaml.safe_load(manifest.read_text())
        assert data["name"] == "audit-tamper"
        assert "mockllm_script" in data

    def test_workspace_exists(self) -> None:
        ws = Path(__file__).parents[2] / "demo" / "fixtures" / "audit_tamper" / "workspace"
        assert ws.exists()

    def test_audit_tamper_detection(self, tmp_path: Path) -> None:
        """Verify that audit tamper detection works."""
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
        log.append(
            event_type="action.proposed",
            state="NORMALIZING",
            action_id="act-1",
            source="MODEL",
            payload={"type": "shell"},
        )
        log.close()

        # Verify original chain
        log2 = AuditLog(audit_path, run_id="run-1")
        v = log2.verify()
        assert v.chain_valid

        # Tamper: modify the audit file
        content = audit_path.read_text()
        tampered = content.replace("NORMALIZING", "TAMPERED")
        audit_path.write_text(tampered)

        log3 = AuditLog(audit_path, run_id="run-1")
        v3 = log3.verify()
        assert not v3.chain_valid
