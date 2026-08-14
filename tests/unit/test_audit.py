"""Tests for append-only, tamper-evident audit facts."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from coding_agent_harness.artifacts import RunArtifactStore
from coding_agent_harness.audit import AuditLog, build_run_summary
from coding_agent_harness.errors import HarnessEvidenceError


def _build_log(tmp_path: Path) -> tuple[AuditLog, Path]:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, run_id="run-1")
    log.append(event_type="RUN_STARTED", state="PREPARING", payload={"task_id": "task-1"})
    log.append(event_type="ACTION_ALLOWED", state="GOVERNING", action_id="action-1")
    log.append(event_type="RUN_FINISHED", state="TERMINAL", payload={"terminal_state": "SUCCESS"})
    return log, path


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_lines(path: Path, lines: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n" for line in lines),
        encoding="utf-8",
    )


def _anchor_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.anchor")


def _audit_bytes(path: Path) -> tuple[bytes, bytes]:
    return path.read_bytes(), _anchor_path(path).read_bytes()


def test_valid_chain_builds_deterministic_summary(tmp_path: Path) -> None:
    log, _ = _build_log(tmp_path)

    verification = log.verify()
    first = build_run_summary(verification)
    second = build_run_summary(log.verify())

    assert verification.chain_valid is True
    assert verification.evidence_complete is True
    assert verification.errors == ()
    assert first == second
    assert first.run_id == "run-1"
    assert first.event_count == 3
    assert first.final_state == "TERMINAL"
    assert first.terminal_state == "SUCCESS"
    assert first.event_types == ("RUN_STARTED", "ACTION_ALLOWED", "RUN_FINISHED")


def test_fresh_log_uses_durable_anchor_to_detect_tail_truncation(tmp_path: Path) -> None:
    _, path = _build_log(tmp_path)
    path.write_bytes(b"".join(path.read_bytes().splitlines(keepends=True)[:-1]))

    verification = AuditLog(path, run_id="run-1").verify()

    assert verification.chain_valid is False
    assert "anchored_event_count_mismatch" in verification.errors


def test_fresh_log_uses_durable_anchor_to_detect_full_log_deletion(tmp_path: Path) -> None:
    _, path = _build_log(tmp_path)
    path.unlink()

    verification = AuditLog(path, run_id="run-1").verify()

    assert verification.chain_valid is False
    assert "anchored_event_count_mismatch" in verification.errors


def test_fresh_log_rejects_missing_anchor_for_existing_events(tmp_path: Path) -> None:
    _, path = _build_log(tmp_path)
    _anchor_path(path).unlink()

    verification = AuditLog(path, run_id="run-1").verify()

    assert verification.chain_valid is False
    assert "audit_anchor_missing" in verification.errors


@pytest.mark.parametrize(
    "anchor",
    [
        b'{"count":3',
        b'{"count":3, "head_hash":"' + b"0" * 64 + b'","version":1}\n',
        b'{"count":3,"head_hash":"' + b"f" * 64 + b'","version":1}\n',
    ],
)
def test_fresh_log_rejects_malformed_or_tampered_anchor(
    tmp_path: Path, anchor: bytes
) -> None:
    _, path = _build_log(tmp_path)
    _anchor_path(path).write_bytes(anchor)

    verification = AuditLog(path, run_id="run-1").verify()

    assert verification.chain_valid is False
    assert verification.errors
    with pytest.raises(HarnessEvidenceError, match="verified audit chain"):
        build_run_summary(verification)


def test_new_log_initializes_canonical_genesis_anchor(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"

    AuditLog(path, run_id="run-1")

    assert _anchor_path(path).read_bytes() == (
        b'{"count":0,"head_hash":"' + b"0" * 64 + b'","version":1}\n'
    )


def test_genesis_anchor_fsync_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coding_agent_harness import audit

    monkeypatch.setattr(
        audit.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("blocked")),
    )

    with pytest.raises(HarnessEvidenceError, match="audit anchor could not be persisted"):
        AuditLog(tmp_path / "audit.jsonl", run_id="run-1")


def test_anchor_fsync_failure_after_event_fsync_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coding_agent_harness import audit

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, run_id="run-1")
    original_fsync = audit.os.fsync
    calls = 0

    def fail_anchor_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("blocked")
        original_fsync(descriptor)

    monkeypatch.setattr(audit.os, "fsync", fail_anchor_fsync)

    with pytest.raises(HarnessEvidenceError, match="audit anchor could not be persisted"):
        log.append(event_type="RUN_STARTED", state="PREPARING")

    assert log.verify().chain_valid is False


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_constructor_rejects_nonregular_audit_path_without_touching_target(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "audit.jsonl"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside evidence")
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()

    with pytest.raises(HarnessEvidenceError, match="audit log path is not a regular file"):
        AuditLog(path, run_id="run-1")

    assert outside.read_bytes() == b"outside evidence"


def test_verify_and_append_reject_replaced_audit_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, run_id="run-1")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside evidence")
    path.symlink_to(outside)

    with pytest.raises(HarnessEvidenceError, match="audit log path is not a regular file"):
        log.verify()
    with pytest.raises(HarnessEvidenceError, match="audit log path is not a regular file"):
        log.append(event_type="RUN_STARTED", state="PREPARING")

    assert outside.read_bytes() == b"outside evidence"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_verify_rejects_nonregular_anchor_without_touching_target(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, run_id="run-1")
    anchor = _anchor_path(path)
    anchor.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside evidence")
    if kind == "symlink":
        anchor.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(anchor)
    else:
        anchor.mkdir()

    with pytest.raises(HarnessEvidenceError, match="audit anchor is not a regular file"):
        log.verify()
    with pytest.raises(HarnessEvidenceError, match="audit anchor is not a regular file"):
        log.append(event_type="RUN_STARTED", state="PREPARING")

    assert outside.read_bytes() == b"outside evidence"


def test_io_remains_bound_to_original_parent_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    path = state / "audit.jsonl"
    log = AuditLog(path, run_id="run-1")
    original_state = tmp_path / "original-state"
    state.rename(original_state)
    state.mkdir()

    log.append(event_type="RUN_STARTED", state="PREPARING")

    assert not path.exists()
    assert (original_state / "audit.jsonl").is_file()
    assert log.verify().chain_valid is True


@pytest.mark.parametrize("tamper", ["modify", "delete", "insert", "reorder"])
def test_verify_detects_event_stream_tampering(tmp_path: Path, tamper: str) -> None:
    log, path = _build_log(tmp_path)
    lines = _lines(path)
    if tamper == "modify":
        lines[1]["state"] = "EXECUTING"
    elif tamper == "delete":
        del lines[1]
    elif tamper == "insert":
        lines.insert(1, dict(lines[0]))
    else:
        lines[0], lines[1] = lines[1], lines[0]
    _write_lines(path, lines)

    verification = log.verify()

    assert verification.chain_valid is False
    assert verification.errors
    with pytest.raises(HarnessEvidenceError, match="verified audit chain"):
        build_run_summary(verification)


def test_missing_artifact_keeps_chain_valid_but_evidence_incomplete(tmp_path: Path) -> None:
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir()
    store = RunArtifactStore(artifact_root, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    log = AuditLog(tmp_path / "audit.jsonl", run_id="run-1", artifact_store=store)
    log.append(event_type="EXECUTION_RESULT", state="PROCESSING_FEEDBACK", artifact_refs=(reference,))
    (artifact_root / reference.run_id / reference.storage_key).unlink()

    verification = log.verify()

    assert verification.chain_valid is True
    assert verification.evidence_complete is False
    assert any("evidence_missing" in error for error in verification.errors)


def test_artifact_digest_mismatch_is_reported_separately_from_chain(tmp_path: Path) -> None:
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir()
    store = RunArtifactStore(artifact_root, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    log = AuditLog(tmp_path / "audit.jsonl", run_id="run-1", artifact_store=store)
    log.append(event_type="EXECUTION_RESULT", state="PROCESSING_FEEDBACK", artifact_refs=(reference,))
    (artifact_root / reference.run_id / reference.storage_key).write_bytes(b"tampered output")

    verification = log.verify()

    assert verification.chain_valid is True
    assert verification.evidence_complete is False
    assert any("digest_mismatch" in error for error in verification.errors)


@pytest.mark.parametrize("tamper", ["delete", "digest_mismatch"])
def test_append_rejects_existing_incomplete_evidence_without_writing(
    tmp_path: Path, tamper: str
) -> None:
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir()
    store = RunArtifactStore(artifact_root, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, run_id="run-1", artifact_store=store)
    log.append(event_type="EXECUTION_RESULT", state="PROCESSING_FEEDBACK", artifact_refs=(reference,))
    artifact_path = artifact_root / reference.run_id / reference.storage_key
    if tamper == "delete":
        artifact_path.unlink()
    else:
        artifact_path.write_bytes(b"tampered output")
    before = _audit_bytes(path)

    with pytest.raises(HarnessEvidenceError, match="audit evidence is incomplete"):
        log.append(event_type="STATE_CHANGED", state="TERMINAL")

    assert _audit_bytes(path) == before


@pytest.mark.parametrize("fabrication", ["missing", "digest_mismatch", "cross_run"])
def test_append_rejects_new_invalid_artifact_reference_without_writing(
    tmp_path: Path, fabrication: str
) -> None:
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir()
    store = RunArtifactStore(artifact_root, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    if fabrication == "missing":
        reference = replace(
            reference,
            artifact_id="f" * 32,
            storage_key=f"artifacts/{'f' * 32}.bin",
        )
    elif fabrication == "digest_mismatch":
        reference = replace(reference, sha256="0" * 64)
    else:
        reference = replace(reference, run_id="run-2")
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, run_id="run-1", artifact_store=store)
    before = _anchor_path(path).read_bytes()

    with pytest.raises(HarnessEvidenceError, match="artifact reference is not valid"):
        log.append(
            event_type="EXECUTION_RESULT",
            state="PROCESSING_FEEDBACK",
            artifact_refs=(reference,),
        )

    assert not path.exists()
    assert _anchor_path(path).read_bytes() == before


def test_append_rejects_new_artifact_reference_without_store(tmp_path: Path) -> None:
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir()
    store = RunArtifactStore(artifact_root, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, run_id="run-1")
    before = _anchor_path(path).read_bytes()

    with pytest.raises(HarnessEvidenceError, match="artifact store is unavailable"):
        log.append(
            event_type="EXECUTION_RESULT",
            state="PROCESSING_FEEDBACK",
            artifact_refs=(reference,),
        )

    assert not path.exists()
    assert _anchor_path(path).read_bytes() == before


def test_append_rejects_invalid_chain_without_writing(tmp_path: Path) -> None:
    log, path = _build_log(tmp_path)
    lines = _lines(path)
    lines[1]["state"] = "EXECUTING"
    _write_lines(path, lines)
    before = _audit_bytes(path)

    with pytest.raises(HarnessEvidenceError, match="audit chain is invalid"):
        log.append(event_type="STATE_CHANGED", state="TERMINAL")

    assert _audit_bytes(path) == before


def test_append_fsync_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coding_agent_harness import audit

    log = AuditLog(tmp_path / "audit.jsonl", run_id="run-1")
    monkeypatch.setattr(audit.os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("blocked")))

    with pytest.raises(HarnessEvidenceError, match="audit event could not be persisted") as error:
        log.append(event_type="RUN_STARTED", state="PREPARING")

    assert error.value.code == "EVIDENCE_ERROR"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"run_id":"run-1"',
        b'{"run_id":"run-1"}\nnot-json\n',
    ],
)
def test_verify_rejects_malformed_or_truncated_jsonl(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_bytes(raw)

    verification = AuditLog(path, run_id="run-1").verify()

    assert verification.chain_valid is False
    assert verification.errors


def test_audit_event_contains_artifact_reference_but_not_raw_bytes(tmp_path: Path) -> None:
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir()
    store = RunArtifactStore(artifact_root, run_id="run-1")
    raw_evidence = b"redacted but deliberately unique raw evidence"
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=raw_evidence,
        media_type="text/plain",
    )
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, run_id="run-1", artifact_store=store)

    event = log.append(
        event_type="EXECUTION_RESULT",
        state="PROCESSING_FEEDBACK",
        artifact_refs=(reference,),
    )

    persisted = path.read_bytes()
    assert raw_evidence not in persisted
    assert reference.sha256.encode() in persisted
    assert event.artifact_refs == (reference,)
