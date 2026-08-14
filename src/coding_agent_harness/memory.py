"""Deterministic, rule-gated long-term memory for one workspace."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, Self


class SecretDetector(Protocol):
    """Classify candidate text without coupling memory to credential storage."""

    def contains_secret(self, value: str | bytes) -> bool: ...


class MemoryKind(str, Enum):
    """The finite classes of knowledge accepted by the first release."""

    CONVENTION = "convention"
    FAILURE = "failure"
    VERIFIED_FIX = "verified_fix"
    HUMAN_PIN = "human_pin"


class ExtractionRule(str, Enum):
    """Explicit extraction decisions supplied to :meth:`MemoryStore.consider`."""

    CONFIRMED_CONVENTION = "confirmed_convention"
    REPEATED_FAILURE = "repeated_failure"
    VERIFIED_FIX = "verified_fix"
    HUMAN_PIN = "human_pin"
    ORDINARY_STDOUT = "ordinary_stdout"
    MODEL_GUESS = "model_guess"


class ConsiderStatus(str, Enum):
    NO_UPDATE = "NO_UPDATE"
    CREATED = "CREATED"
    UPDATED = "UPDATED"


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    run_id: str
    workspace_id: str
    kind: MemoryKind
    content: str
    extraction_rule: ExtractionRule
    tags: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    confidence: int = 0
    failure_signature: str | None = None
    secret_candidate: bool = False

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must be non-empty")


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    memory_id: str
    workspace_id: str
    kind: MemoryKind
    content: str
    tags: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    confidence: int
    created_at: datetime
    updated_at: datetime
    failure_signature: str | None = None


@dataclass(frozen=True, slots=True)
class ConsiderResult:
    status: ConsiderStatus
    entry: MemoryEntry | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    workspace_id: str
    kinds: tuple[MemoryKind, ...] = ()
    tokens: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_ALLOWED_RULES = {
    ExtractionRule.CONFIRMED_CONVENTION: MemoryKind.CONVENTION,
    ExtractionRule.REPEATED_FAILURE: MemoryKind.FAILURE,
    ExtractionRule.VERIFIED_FIX: MemoryKind.VERIFIED_FIX,
    ExtractionRule.HUMAN_PIN: MemoryKind.HUMAN_PIN,
}


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _normalize_terms(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted({term for value in values if (term := _normalize_text(value))})
    )


def _content_tokens(content: str) -> frozenset[str]:
    return frozenset(_TOKEN_PATTERN.findall(_normalize_text(content)))


class MemoryStore:
    """A SQLite store whose writes are controlled by deterministic rules."""

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        secret_detector: SecretDetector,
        top_k: int = 5,
        char_limit: int = 4096,
    ) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if char_limit <= 0:
            raise ValueError("char_limit must be positive")
        self._clock = clock
        self._id_factory = id_factory
        self._secret_detector = secret_detector
        self._top_k = top_k
        self._char_limit = char_limit
        self._connection = sqlite3.connect(database_path)
        self._connection.row_factory = sqlite3.Row
        self._failure_observations: dict[
            tuple[str, str, str], tuple[int, tuple[str, ...]]
        ] = {}
        self._create_schema()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self._connection.close()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_entries (
                    memory_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    confidence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    failure_signature TEXT,
                    UNIQUE(workspace_id, kind, failure_signature)
                );
                """
            )

    def consider(self, candidate: MemoryCandidate) -> ConsiderResult:
        """Persist a candidate only when its explicit extraction rule permits it."""

        if candidate.secret_candidate:
            return ConsiderResult(ConsiderStatus.NO_UPDATE, reason="secret_candidate")
        candidate_text = (
            candidate.content,
            *candidate.tags,
            *candidate.evidence_refs,
            *((candidate.failure_signature,) if candidate.failure_signature else ()),
        )
        if any(self._secret_detector.contains_secret(value) for value in candidate_text):
            return ConsiderResult(ConsiderStatus.NO_UPDATE, reason="secret_detected")
        expected_kind = _ALLOWED_RULES.get(candidate.extraction_rule)
        if expected_kind is None:
            return ConsiderResult(ConsiderStatus.NO_UPDATE, reason="rule_not_eligible")
        if expected_kind is not candidate.kind:
            return ConsiderResult(ConsiderStatus.NO_UPDATE, reason="rule_kind_mismatch")
        if not candidate.workspace_id or not candidate.content.strip():
            return ConsiderResult(ConsiderStatus.NO_UPDATE, reason="empty_candidate")
        if not 0 <= candidate.confidence <= 100:
            return ConsiderResult(ConsiderStatus.NO_UPDATE, reason="invalid_confidence")
        if (
            candidate.extraction_rule is ExtractionRule.VERIFIED_FIX
            and not candidate.evidence_refs
        ):
            return ConsiderResult(ConsiderStatus.NO_UPDATE, reason="missing_evidence")

        if candidate.extraction_rule is ExtractionRule.REPEATED_FAILURE:
            return self._consider_failure(candidate)
        return self._insert(candidate, failure_signature=None)

    def _consider_failure(self, candidate: MemoryCandidate) -> ConsiderResult:
        signature = _normalize_text(candidate.failure_signature or "")
        if not signature:
            return ConsiderResult(ConsiderStatus.NO_UPDATE, reason="missing_signature")
        if self._secret_detector.contains_secret(signature):
            return ConsiderResult(ConsiderStatus.NO_UPDATE, reason="secret_detected")

        observation_key = (candidate.run_id, candidate.workspace_id, signature)
        observation = self._failure_observations.get(observation_key)
        if observation is None:
            evidence = tuple(sorted(set(candidate.evidence_refs)))
            self._failure_observations[observation_key] = (1, evidence)
            return ConsiderResult(
                ConsiderStatus.NO_UPDATE, reason="failure_not_repeated"
            )

        occurrences, prior_evidence = observation
        evidence = tuple(sorted(set(prior_evidence) | set(candidate.evidence_refs)))
        self._failure_observations[observation_key] = (occurrences + 1, evidence)

        existing = self._find_failure(candidate.workspace_id, signature)
        if existing is not None:
            return self._update_failure(existing, candidate, evidence)
        return self._insert(
            candidate,
            failure_signature=signature,
            evidence_refs=evidence,
        )

    def _insert(
        self,
        candidate: MemoryCandidate,
        *,
        failure_signature: str | None,
        evidence_refs: tuple[str, ...] | None = None,
    ) -> ConsiderResult:
        now = self._clock()
        memory_id = self._id_factory()
        tags = _normalize_terms(candidate.tags)
        evidence = tuple(sorted(set(evidence_refs or candidate.evidence_refs)))
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO memory_entries (
                    memory_id, workspace_id, kind, content, tags_json,
                    evidence_refs_json, confidence, created_at, updated_at,
                    failure_signature
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    candidate.workspace_id,
                    candidate.kind.value,
                    candidate.content,
                    json.dumps(tags),
                    json.dumps(evidence),
                    candidate.confidence,
                    now.isoformat(),
                    now.isoformat(),
                    failure_signature,
                ),
            )
        return ConsiderResult(
            ConsiderStatus.CREATED,
            self._find_by_id(memory_id),
        )

    def _find_failure(
        self, workspace_id: str, signature: str
    ) -> MemoryEntry | None:
        row = self._connection.execute(
            """
            SELECT * FROM memory_entries
            WHERE workspace_id = ? AND kind = ? AND failure_signature = ?
            """,
            (workspace_id, MemoryKind.FAILURE.value, signature),
        ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    def _find_by_id(self, memory_id: str) -> MemoryEntry:
        row = self._connection.execute(
            "SELECT * FROM memory_entries WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("inserted memory entry is missing")
        return self._row_to_entry(row)

    def _update_failure(
        self,
        existing: MemoryEntry,
        candidate: MemoryCandidate,
        evidence_refs: tuple[str, ...],
    ) -> ConsiderResult:
        now = self._clock()
        with self._connection:
            self._connection.execute(
                """
                UPDATE memory_entries
                SET content = ?, tags_json = ?, evidence_refs_json = ?,
                    confidence = ?, updated_at = ?
                WHERE memory_id = ?
                """,
                (
                    candidate.content,
                    json.dumps(_normalize_terms(candidate.tags)),
                    json.dumps(evidence_refs),
                    candidate.confidence,
                    now.isoformat(),
                    existing.memory_id,
                ),
            )
        return ConsiderResult(
            ConsiderStatus.UPDATED,
            self._find_by_id(existing.memory_id),
        )

    def search(self, query: MemoryQuery) -> tuple[MemoryEntry, ...]:
        """Return a bounded, deterministic prefix of relevant entries."""

        parameters: list[str] = [query.workspace_id]
        statement = "SELECT * FROM memory_entries WHERE workspace_id = ?"
        if query.kinds:
            placeholders = ", ".join("?" for _ in query.kinds)
            statement += f" AND kind IN ({placeholders})"
            parameters.extend(kind.value for kind in query.kinds)
        rows = self._connection.execute(statement, parameters).fetchall()
        entries = [self._row_to_entry(row) for row in rows]

        query_tags = frozenset(_normalize_terms(query.tags))
        query_tokens = frozenset(_normalize_terms(query.tokens))

        def rank(entry: MemoryEntry) -> tuple[int, int, int, str]:
            tag_overlap = len(query_tags & frozenset(entry.tags))
            token_overlap = len(query_tokens & _content_tokens(entry.content))
            updated_at_microseconds = int(entry.updated_at.timestamp() * 1_000_000)
            return (
                -(tag_overlap + token_overlap),
                -entry.confidence,
                -updated_at_microseconds,
                entry.memory_id,
            )

        selected: list[MemoryEntry] = []
        used_characters = 0
        for entry in sorted(entries, key=rank):
            if len(selected) >= self._top_k:
                break
            next_size = used_characters + len(entry.content)
            if next_size > self._char_limit:
                break
            selected.append(entry)
            used_characters = next_size
        return tuple(selected)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            memory_id=str(row["memory_id"]),
            workspace_id=str(row["workspace_id"]),
            kind=MemoryKind(row["kind"]),
            content=str(row["content"]),
            tags=tuple(json.loads(row["tags_json"])),
            evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
            confidence=int(row["confidence"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            failure_signature=row["failure_signature"],
        )
