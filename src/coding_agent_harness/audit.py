"""Append-only audit facts with deterministic hash-chain verification."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from coding_agent_harness.artifacts import ArtifactRef, RunArtifactStore
from coding_agent_harness.canonical import canonical_json_bytes
from coding_agent_harness.domain import FrozenMapping
from coding_agent_harness.errors import HarnessEvidenceError, HarnessValidationError

GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One immutable fact in a run's audit chain."""

    run_id: str
    seq: int
    event_type: str
    state: str
    action_id: str | None
    source: str | None
    payload: Mapping[str, Any]
    artifact_refs: tuple[ArtifactRef, ...]
    prev_hash: str
    event_hash: str


@dataclass(frozen=True, slots=True)
class AuditVerification:
    """Separate hash-chain validity from external evidence completeness."""

    chain_valid: bool
    evidence_complete: bool
    errors: tuple[str, ...]
    events: tuple[AuditEvent, ...]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """A deterministic projection of a verified audit event stream."""

    run_id: str
    event_count: int
    event_types: tuple[str, ...]
    final_state: str | None
    terminal_state: str | None
    chain_head: str
    evidence_complete: bool


class AuditLog:
    """Append and verify canonical JSONL facts for exactly one run."""

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        artifact_store: RunArtifactStore | None = None,
    ) -> None:
        if not run_id:
            raise HarnessValidationError("audit run_id must not be empty")
        audit_path = Path(path)
        if not audit_path.name or audit_path.name in {".", ".."}:
            raise HarnessValidationError("audit path must name a file")
        try:
            parent = audit_path.parent.resolve(strict=True)
            self._parent_fd = os.open(
                parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as error:
            raise HarnessEvidenceError("audit parent directory is unavailable") from error
        self._closed = False
        self._filename = audit_path.name
        self._anchor_filename = f"{audit_path.name}.anchor"
        self.run_id = run_id
        self._artifact_store = artifact_store
        try:
            audit_exists = self._entry_is_regular(
                self._filename, label="audit log path", missing_ok=True
            )
            anchor_exists = self._entry_is_regular(
                self._anchor_filename, label="audit anchor", missing_ok=True
            )
            if not audit_exists and not anchor_exists:
                self._write_anchor(count=0, head_hash=GENESIS_HASH)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release the bound parent directory descriptor."""

        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._parent_fd)
        except OSError as error:
            raise HarnessEvidenceError("audit parent directory could not be closed") from error

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, HarnessEvidenceError, OSError):
            pass

    def append(
        self,
        *,
        event_type: str,
        state: str,
        payload: Mapping[str, Any] | None = None,
        action_id: str | None = None,
        source: str | None = None,
        artifact_refs: Sequence[ArtifactRef] = (),
    ) -> AuditEvent:
        """Append and durably flush one fact before returning it."""

        if not event_type or not state:
            raise HarnessValidationError("audit event_type and state must not be empty")
        prior = self.verify()
        if not prior.chain_valid:
            raise HarnessEvidenceError("audit chain is invalid; append denied")
        if not prior.evidence_complete:
            raise HarnessEvidenceError("audit evidence is incomplete; append denied")

        references = tuple(artifact_refs)
        if references and self._artifact_store is None:
            raise HarnessEvidenceError("artifact store is unavailable; append denied")
        if self._artifact_store is not None:
            for reference in references:
                result = self._artifact_store.verify(reference)
                if result.status != "valid":
                    raise HarnessEvidenceError(
                        f"artifact reference is not valid: {result.status}"
                    )

        previous_hash = prior.events[-1].event_hash if prior.events else GENESIS_HASH
        event_without_hash: dict[str, Any] = {
            "run_id": self.run_id,
            "seq": len(prior.events) + 1,
            "event_type": event_type,
            "state": state,
            "action_id": action_id,
            "source": source,
            "payload": dict(payload or {}),
            "artifact_refs": [asdict(reference) for reference in references],
            "prev_hash": previous_hash,
        }
        event_hash = _event_hash(previous_hash, event_without_hash)
        record = {**event_without_hash, "event_hash": event_hash}
        encoded = canonical_json_bytes(record) + b"\n"

        descriptor = self._open_regular(
            self._filename,
            label="audit log path",
            flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            mode=0o600,
        )
        try:
            with os.fdopen(descriptor, "ab") as audit_file:
                audit_file.write(encoded)
                audit_file.flush()
                os.fsync(audit_file.fileno())
        except OSError as error:
            raise HarnessEvidenceError("audit event could not be persisted") from error

        self._write_anchor(count=record["seq"], head_hash=event_hash)
        event = _event_from_record(record)
        return event

    def verify(self) -> AuditVerification:
        """Verify chain structure and all referenced evidence without reading bytes."""

        chain_errors: list[str] = []
        evidence_errors: list[str] = []
        raw = self._read_regular(
            self._filename, label="audit log path", missing_ok=True
        ) or b""

        events: list[AuditEvent] = []
        if raw and not raw.endswith(b"\n"):
            chain_errors.append("malformed_or_truncated_jsonl")
        else:
            previous_hash = GENESIS_HASH
            for expected_seq, line in enumerate(raw.splitlines(), start=1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise TypeError
                    event = _event_from_record(record)
                    without_hash = dict(record)
                    without_hash.pop("event_hash")
                    expected_hash = _event_hash(previous_hash, without_hash)
                except (KeyError, TypeError, ValueError, UnicodeDecodeError, HarnessValidationError):
                    chain_errors.append(f"invalid_event:{expected_seq}")
                    break
                if event.run_id != self.run_id:
                    chain_errors.append(f"run_id_mismatch:{expected_seq}")
                if event.seq != expected_seq:
                    chain_errors.append(f"sequence_mismatch:{expected_seq}")
                if event.prev_hash != previous_hash:
                    chain_errors.append(f"previous_hash_mismatch:{expected_seq}")
                if event.event_hash != expected_hash:
                    chain_errors.append(f"event_hash_mismatch:{expected_seq}")
                events.append(event)
                previous_hash = event.event_hash

        anchor, anchor_error = self._read_anchor()
        if anchor_error is not None:
            chain_errors.append(anchor_error)
        elif anchor is not None:
            anchored_count, anchored_head = anchor
            actual_head = events[-1].event_hash if events else GENESIS_HASH
            if len(events) != anchored_count:
                chain_errors.append("anchored_event_count_mismatch")
            if actual_head != anchored_head:
                chain_errors.append("anchored_chain_head_mismatch")

        if not chain_errors:
            for event in events:
                for reference in event.artifact_refs:
                    if self._artifact_store is None:
                        evidence_errors.append(
                            f"artifact_store_unavailable:{reference.artifact_id}"
                        )
                        continue
                    result = self._artifact_store.verify(reference)
                    if result.status != "valid":
                        evidence_errors.append(
                            f"{result.status}:{reference.artifact_id}"
                        )

        return AuditVerification(
            chain_valid=not chain_errors,
            evidence_complete=not evidence_errors,
            errors=tuple(chain_errors + evidence_errors),
            events=tuple(events),
        )

    def _read_anchor(self) -> tuple[tuple[int, str] | None, str | None]:
        raw = self._read_regular(
            self._anchor_filename, label="audit anchor", missing_ok=True
        )
        if raw is None:
            return None, "audit_anchor_missing"
        try:
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise TypeError
            if set(record) != {"version", "count", "head_hash"}:
                raise ValueError
            version = record["version"]
            count = record["count"]
            head_hash = record["head_hash"]
            if (
                type(version) is not int
                or version != 1
                or type(count) is not int
                or count < 0
                or not _is_hash(head_hash)
                or raw != canonical_json_bytes(record) + b"\n"
            ):
                raise ValueError
        except (json.JSONDecodeError, TypeError, ValueError, HarnessValidationError):
            return None, "audit_anchor_invalid"
        return (count, head_hash), None

    def _write_anchor(self, *, count: int, head_hash: str) -> None:
        record = {"version": 1, "count": count, "head_hash": head_hash}
        encoded = canonical_json_bytes(record) + b"\n"
        temporary = f".{self._anchor_filename}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=self._parent_fd,
            )
            with os.fdopen(descriptor, "wb") as anchor_file:
                anchor_file.write(encoded)
                anchor_file.flush()
                os.fsync(anchor_file.fileno())
            os.replace(
                temporary,
                self._anchor_filename,
                src_dir_fd=self._parent_fd,
                dst_dir_fd=self._parent_fd,
            )
            os.fsync(self._parent_fd)
        except OSError as error:
            raise HarnessEvidenceError("audit anchor could not be persisted") from error
        finally:
            try:
                os.unlink(temporary, dir_fd=self._parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _entry_is_regular(
        self, filename: str, *, label: str, missing_ok: bool
    ) -> bool:
        self._require_open()
        try:
            metadata = os.stat(
                filename, dir_fd=self._parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            if missing_ok:
                return False
            raise HarnessEvidenceError(f"{label} is missing") from None
        except OSError as error:
            raise HarnessEvidenceError(f"{label} is unavailable") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise HarnessEvidenceError(f"{label} is not a regular file")
        return True

    def _open_regular(
        self,
        filename: str,
        *,
        label: str,
        flags: int,
        mode: int = 0,
    ) -> int:
        self._require_open()
        self._entry_is_regular(filename, label=label, missing_ok=bool(flags & os.O_CREAT))
        try:
            descriptor = os.open(
                filename,
                flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                mode,
                dir_fd=self._parent_fd,
            )
        except OSError as error:
            raise HarnessEvidenceError(f"{label} is unavailable") from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise HarnessEvidenceError(f"{label} is not a regular file")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _read_regular(
        self, filename: str, *, label: str, missing_ok: bool
    ) -> bytes | None:
        exists = self._entry_is_regular(filename, label=label, missing_ok=missing_ok)
        if not exists:
            return None
        descriptor = self._open_regular(
            filename, label=label, flags=os.O_RDONLY
        )
        try:
            with os.fdopen(descriptor, "rb") as evidence_file:
                return evidence_file.read()
        except OSError as error:
            raise HarnessEvidenceError(f"{label} could not be read") from error

    def _require_open(self) -> None:
        if self._closed:
            raise HarnessEvidenceError("audit log is closed")


def build_run_summary(verification: AuditVerification) -> RunSummary:
    """Project only a verified event stream into a stable summary."""

    if not verification.chain_valid:
        raise HarnessEvidenceError("run summary requires a verified audit chain")
    events = verification.events
    if not events:
        return RunSummary(
            run_id="",
            event_count=0,
            event_types=(),
            final_state=None,
            terminal_state=None,
            chain_head=GENESIS_HASH,
            evidence_complete=verification.evidence_complete,
        )
    terminal_state = events[-1].payload.get("terminal_state")
    return RunSummary(
        run_id=events[0].run_id,
        event_count=len(events),
        event_types=tuple(event.event_type for event in events),
        final_state=events[-1].state,
        terminal_state=terminal_state if isinstance(terminal_state, str) else None,
        chain_head=events[-1].event_hash,
        evidence_complete=verification.evidence_complete,
    )


def _event_hash(previous_hash: str, event_without_hash: object) -> str:
    return hashlib.sha256(
        previous_hash.encode("ascii") + canonical_json_bytes(event_without_hash)
    ).hexdigest()


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _event_from_record(record: Mapping[str, Any]) -> AuditEvent:
    required = {
        "run_id",
        "seq",
        "event_type",
        "state",
        "action_id",
        "source",
        "payload",
        "artifact_refs",
        "prev_hash",
        "event_hash",
    }
    if set(record) != required:
        raise ValueError("audit event fields do not match the schema")
    payload = record["payload"]
    raw_references = record["artifact_refs"]
    if not isinstance(payload, dict) or not isinstance(raw_references, list):
        raise TypeError("audit event payload is malformed")
    references = tuple(ArtifactRef(**reference) for reference in raw_references)
    run_id = record["run_id"]
    seq = record["seq"]
    event_type = record["event_type"]
    state = record["state"]
    action_id = record["action_id"]
    source = record["source"]
    previous_hash = record["prev_hash"]
    event_hash = record["event_hash"]
    if (
        not isinstance(run_id, str)
        or type(seq) is not int
        or not isinstance(event_type, str)
        or not isinstance(state, str)
        or (action_id is not None and not isinstance(action_id, str))
        or (source is not None and not isinstance(source, str))
        or not isinstance(previous_hash, str)
        or not isinstance(event_hash, str)
    ):
        raise ValueError("audit event fields are malformed")
    return AuditEvent(
        run_id=run_id,
        seq=seq,
        event_type=event_type,
        state=state,
        action_id=action_id,
        source=source,
        payload=FrozenMapping(payload),
        artifact_refs=references,
        prev_hash=previous_hash,
        event_hash=event_hash,
    )
