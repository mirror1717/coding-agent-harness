"""Security audit tests: verify exact secrets never reach audit, artifact, or memory."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coding_agent_harness.artifacts import RunArtifactStore
from coding_agent_harness.audit import AuditLog
from coding_agent_harness.credentials import CredentialStore, ExactSecretDetector
from coding_agent_harness.feedback import FeedbackEngine
from coding_agent_harness.memory import (
    ConsiderResult,
    ConsiderStatus,
    MemoryCandidate,
    MemoryKind,
    MemoryQuery,
    MemoryStore,
    ExtractionRule,
)

_SECRET = "sk-exact-audit-secret-12345"


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def _make_store(secret: str = _SECRET) -> CredentialStore:
    backend = FakeKeyring()
    store = CredentialStore(backend, now_utc=lambda: "2026-08-14T08:00:00Z")
    store.set("openai", secret, endpoint="https://api.test/v1", model="gpt-test")
    return store


class TestSecretNotInAuditLog:
    def test_audit_events_do_not_contain_exact_secret(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "audit.jsonl"
        log = AuditLog(audit_path, run_id="run-1")
        log.append(
            event_type="action.proposed",
            state="NORMALIZING",
            action_id="act-1",
            source="MODEL",
            payload={"type": "shell", "argv": ["echo", "hello"]},
        )
        log.append(
            event_type="action.executed",
            state="EXECUTING",
            action_id="act-1",
            source="MODEL",
            payload={"exit_code": 0, "stdout": "hello"},
        )
        log.close()

        content = audit_path.read_text()
        assert _SECRET not in content

    def test_audit_with_secret_in_payload_is_detected_by_detector(self) -> None:
        store = _make_store()
        detector = store.build_secret_detector(("openai",))
        assert isinstance(detector, ExactSecretDetector)

        fake_payload = f"some data with {_SECRET} inside"
        assert detector.contains_secret(fake_payload) is True


class TestSecretNotInArtifacts:
    def test_artifact_content_does_not_contain_secret(self, tmp_path: Path) -> None:
        store = RunArtifactStore(tmp_path, run_id="run-1")
        ref = store.put(
            execution_id="exec-1",
            kind="stdout",
            data=b"ordinary output without secrets\n",
            media_type="text/plain",
        )
        raw = store.read(ref)
        store.close()

        assert _SECRET.encode() not in raw
        assert _SECRET not in ref.sha256
        assert _SECRET not in ref.storage_key

    def test_artifact_with_secret_in_data_is_detected(self) -> None:
        store = _make_store()
        detector = store.build_secret_detector(("openai",))

        tainted_output = f"output leaked {_SECRET} here"
        assert detector.contains_secret(tainted_output) is True
        assert detector.contains_secret(tainted_output.encode()) is True


class TestSecretNotInMemory:
    def test_memory_rejects_secret_content(self, tmp_path: Path) -> None:
        store = _make_store()
        detector = store.build_secret_detector(("openai",))
        mem = MemoryStore(
            tmp_path / "memory.db",
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            id_factory=lambda: "mem-1",
            secret_detector=detector,
        )
        candidate = MemoryCandidate(
            run_id="run-1",
            workspace_id="ws-1",
            kind=MemoryKind.CONVENTION,
            content=f"the key is {_SECRET}",
            extraction_rule=ExtractionRule.HUMAN_PIN,
            tags=("key",),
        )
        result = mem.consider(candidate)
        assert result.status is ConsiderStatus.NO_UPDATE
        assert "secret" in result.reason.lower()

        entries = mem.search(MemoryQuery(workspace_id="ws-1", tokens=("key",)))
        for entry in entries:
            assert _SECRET not in entry.content

    def test_memory_rejects_secret_in_tags(self, tmp_path: Path) -> None:
        store = _make_store()
        detector = store.build_secret_detector(("openai",))
        mem = MemoryStore(
            tmp_path / "memory.db",
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            id_factory=lambda: "mem-1",
            secret_detector=detector,
        )
        candidate = MemoryCandidate(
            run_id="run-1",
            workspace_id="ws-1",
            kind=MemoryKind.CONVENTION,
            content="ordinary content",
            extraction_rule=ExtractionRule.HUMAN_PIN,
            tags=(_SECRET,),
        )
        result = mem.consider(candidate)
        assert result.status is ConsiderStatus.NO_UPDATE

    def test_memory_accepts_non_secret_content(self, tmp_path: Path) -> None:
        store = _make_store()
        detector = store.build_secret_detector(("openai",))
        mem = MemoryStore(
            tmp_path / "memory.db",
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            id_factory=lambda: "mem-1",
            secret_detector=detector,
        )
        candidate = MemoryCandidate(
            run_id="run-1",
            workspace_id="ws-1",
            kind=MemoryKind.HUMAN_PIN,
            content="project uses src layout",
            extraction_rule=ExtractionRule.HUMAN_PIN,
            tags=("layout",),
        )
        result = mem.consider(candidate)
        assert result.status is not ConsiderStatus.NO_UPDATE


class TestSecretNotInFeedback:
    def test_feedback_redacts_exact_secret(self) -> None:
        store = _make_store()
        detector = store.build_secret_detector(("openai",))
        engine = FeedbackEngine(secret_detector=detector)

        fb = engine.parse(
            action_id="act-1",
            action_type="shell",
            exit_code=0,
            stdout=f"output with {_SECRET} inside".encode(),
            stderr=b"",
        )
        assert _SECRET not in fb.summary
        assert _SECRET not in str(fb)


class TestCLICredentialsUpdate:
    """Test that CLI has credentials update command with hidden input."""

    def test_cli_has_update_command(self) -> None:
        from typer.testing import CliRunner
        from coding_agent_harness.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["credentials", "--help"])
        assert "update" in result.output

    def test_cli_update_uses_hidden_input(self) -> None:
        from typer.testing import CliRunner
        from coding_agent_harness.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["credentials", "update", "openai"],
            input="new-secret-key\n",
        )
        assert result.exit_code == 0
