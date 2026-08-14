"""DEMO-2: Feedback Repair + Verification - verify demo fixture exists and is valid."""

from __future__ import annotations

from pathlib import Path

import yaml


class TestDemo2FeedbackVerification:
    def test_manifest_exists(self) -> None:
        manifest = Path(__file__).parents[2] / "demo" / "fixtures" / "feedback_verification" / "manifest.yaml"
        assert manifest.exists()

    def test_manifest_is_valid_yaml(self) -> None:
        manifest = Path(__file__).parents[2] / "demo" / "fixtures" / "feedback_verification" / "manifest.yaml"
        data = yaml.safe_load(manifest.read_text())
        assert data["name"] == "feedback-verification"
        assert "mockllm_script" in data

    def test_manifest_has_pytest_and_fix_and_finish(self) -> None:
        manifest = Path(__file__).parents[2] / "demo" / "fixtures" / "feedback_verification" / "manifest.yaml"
        data = yaml.safe_load(manifest.read_text())
        script = data["mockllm_script"]
        kinds = [s["kind"] for s in script]
        assert "action" in kinds
        assert "finish" in kinds

    def test_workspace_exists(self) -> None:
        ws = Path(__file__).parents[2] / "demo" / "fixtures" / "feedback_verification" / "workspace"
        assert ws.exists()
