"""DEMO-1: Governance + HITL - verify demo fixture exists and is valid."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


class TestDemo1GovernanceHitl:
    def test_manifest_exists(self) -> None:
        manifest = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl" / "manifest.yaml"
        assert manifest.exists()

    def test_manifest_is_valid_yaml(self) -> None:
        manifest = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl" / "manifest.yaml"
        data = yaml.safe_load(manifest.read_text())
        assert data["name"] == "governance-hitl"
        assert "mockllm_script" in data
        assert "approvals" in data

    def test_manifest_has_edit_and_approve(self) -> None:
        manifest = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl" / "manifest.yaml"
        data = yaml.safe_load(manifest.read_text())
        approvals = data["approvals"]
        decisions = [a["decision"] for a in approvals]
        assert "edit_and_execute" in decisions
        assert "approve_once" in decisions

    def test_workspace_exists(self) -> None:
        ws = Path(__file__).parents[2] / "demo" / "fixtures" / "governance_hitl" / "workspace"
        assert ws.exists()
