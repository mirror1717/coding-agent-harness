from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coding_agent_harness.memory import (
    ConsiderStatus,
    ExtractionRule,
    MemoryCandidate,
    MemoryKind,
    MemoryQuery,
    MemoryStore,
)


class EmptySecretDetector:
    def contains_secret(self, value: str | bytes) -> bool:
        return False


class MarkerSecretDetector:
    def __init__(self, marker: str) -> None:
        self._marker = marker

    def contains_secret(self, value: str | bytes) -> bool:
        text = value.decode() if isinstance(value, bytes) else value
        return self._marker in text


def _ids(*values: str) -> Iterator[str]:
    return iter(values)


def _clock(*values: datetime):
    iterator = iter(values)
    return lambda: next(iterator)


def _candidate(
    *,
    run_id: str = "run-a",
    workspace_id: str = "workspace-a",
    kind: MemoryKind = MemoryKind.CONVENTION,
    content: str = "Use Ruff before committing",
    rule: ExtractionRule = ExtractionRule.CONFIRMED_CONVENTION,
    tags: tuple[str, ...] = ("python",),
    evidence_refs: tuple[str, ...] = ("audit:1",),
    confidence: int = 80,
    failure_signature: str | None = None,
    secret_candidate: bool = False,
) -> MemoryCandidate:
    return MemoryCandidate(
        run_id=run_id,
        workspace_id=workspace_id,
        kind=kind,
        content=content,
        extraction_rule=rule,
        tags=tags,
        evidence_refs=evidence_refs,
        confidence=confidence,
        failure_signature=failure_signature,
        secret_candidate=secret_candidate,
    )


def test_memory_candidate_requires_run_id() -> None:
    with pytest.raises(TypeError):
        MemoryCandidate(  # type: ignore[call-arg]
            workspace_id="workspace-a",
            kind=MemoryKind.CONVENTION,
            content="Use Ruff",
            extraction_rule=ExtractionRule.CONFIRMED_CONVENTION,
        )


def test_memory_candidate_rejects_empty_run_id() -> None:
    with pytest.raises(ValueError, match="run_id"):
        _candidate(run_id="   ")


def test_secret_detector_must_be_injected(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        MemoryStore(  # type: ignore[call-arg]
            tmp_path / "memory.sqlite3",
            clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
            id_factory=lambda: "memory-1",
        )


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(content="Convention contains SECRET"),
        _candidate(tags=("python", "SECRET")),
        _candidate(evidence_refs=("AuditEvent:SECRET",)),
    ],
    ids=["content", "tag", "evidence-reference"],
)
def test_detector_rejects_secret_in_any_candidate_text(
    tmp_path: Path, candidate: MemoryCandidate
) -> None:
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: "must-not-be-used",
        secret_detector=MarkerSecretDetector("SECRET"),
    ) as store:
        result = store.consider(candidate)

        assert result.status is ConsiderStatus.NO_UPDATE
        assert result.reason == "secret_detected"
        assert store.search(MemoryQuery(workspace_id="workspace-a")) == ()


@pytest.mark.parametrize(
    "marker",
    ["Ｐ", "parsererror"],
    ids=["raw-signature", "normalized-signature"],
)
def test_detector_rejects_secret_failure_signature_before_second_observation(
    tmp_path: Path, marker: str
) -> None:
    database_path = tmp_path / "memory.sqlite3"
    candidate = _candidate(
        kind=MemoryKind.FAILURE,
        rule=ExtractionRule.REPEATED_FAILURE,
        failure_signature="ＰａｒｓｅｒＥｒｒｏｒ",
    )
    with MemoryStore(
        database_path,
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: "must-not-be-used",
        secret_detector=MarkerSecretDetector(marker),
    ) as store:
        first = store.consider(candidate)
        second = store.consider(candidate)

        with sqlite3.connect(database_path) as connection:
            persisted = connection.execute(
                "SELECT COUNT(*) FROM memory_entries"
            ).fetchone()[0]

        assert first.status is ConsiderStatus.NO_UPDATE
        assert second.status is ConsiderStatus.NO_UPDATE
        assert first.reason == second.reason == "secret_detected"
        assert persisted == 0


@pytest.mark.parametrize(
    ("kind", "rule", "content"),
    [
        (
            MemoryKind.CONVENTION,
            ExtractionRule.CONFIRMED_CONVENTION,
            "Run Ruff before committing",
        ),
        (
            MemoryKind.VERIFIED_FIX,
            ExtractionRule.VERIFIED_FIX,
            "Replacing the parser fixed AC-8",
        ),
        (
            MemoryKind.HUMAN_PIN,
            ExtractionRule.HUMAN_PIN,
            "Keep the public adapter synchronous",
        ),
    ],
)
def test_explicit_extraction_rules_create_memory(
    tmp_path: Path,
    kind: MemoryKind,
    rule: ExtractionRule,
    content: str,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: now,
        id_factory=lambda: "memory-1",
        secret_detector=EmptySecretDetector(),
    ) as store:
        result = store.consider(
            _candidate(kind=kind, rule=rule, content=content)
        )

        assert result.status is ConsiderStatus.CREATED
        assert result.entry is not None
        assert result.entry.memory_id == "memory-1"
        assert result.entry.content == content
        assert result.entry.evidence_refs == ("audit:1",)
        assert result.entry.created_at == now


def test_verified_fix_without_evidence_is_not_persisted(tmp_path: Path) -> None:
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: "must-not-be-used",
        secret_detector=EmptySecretDetector(),
    ) as store:
        result = store.consider(
            _candidate(
                kind=MemoryKind.VERIFIED_FIX,
                rule=ExtractionRule.VERIFIED_FIX,
                evidence_refs=(),
            )
        )

        assert result.status is ConsiderStatus.NO_UPDATE
        assert store.search(MemoryQuery(workspace_id="workspace-a")) == ()


def test_same_normalized_failure_signature_must_occur_twice(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    candidate = _candidate(
        kind=MemoryKind.FAILURE,
        content="test_parser failed",
        rule=ExtractionRule.REPEATED_FAILURE,
        failure_signature="  AssertionError:   Expected VALUE ",
    )
    equivalent = _candidate(
        kind=MemoryKind.FAILURE,
        content="test_parser still fails",
        rule=ExtractionRule.REPEATED_FAILURE,
        failure_signature="assertionerror: expected value",
        evidence_refs=("audit:2",),
    )
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: now,
        id_factory=lambda: "memory-failure",
        secret_detector=EmptySecretDetector(),
    ) as store:
        first = store.consider(candidate)
        second = store.consider(equivalent)

        assert first.status is ConsiderStatus.NO_UPDATE
        assert second.status is ConsiderStatus.CREATED
        assert second.entry is not None
        assert second.entry.failure_signature == "assertionerror: expected value"
        assert second.entry.evidence_refs == ("audit:1", "audit:2")


def test_failure_observations_do_not_cross_run_boundaries(tmp_path: Path) -> None:
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: "memory-run-a",
        secret_detector=EmptySecretDetector(),
    ) as store:
        run_a_first = store.consider(
            _candidate(
                run_id="run-a",
                kind=MemoryKind.FAILURE,
                rule=ExtractionRule.REPEATED_FAILURE,
                failure_signature="ParserError",
                evidence_refs=("audit:a1",),
            )
        )
        run_b_first = store.consider(
            _candidate(
                run_id="run-b",
                kind=MemoryKind.FAILURE,
                rule=ExtractionRule.REPEATED_FAILURE,
                failure_signature="ParserError",
                evidence_refs=("audit:b1",),
            )
        )
        run_a_second = store.consider(
            _candidate(
                run_id="run-a",
                kind=MemoryKind.FAILURE,
                rule=ExtractionRule.REPEATED_FAILURE,
                failure_signature="ParserError",
                evidence_refs=("audit:a2",),
            )
        )

        assert run_a_first.status is ConsiderStatus.NO_UPDATE
        assert run_b_first.status is ConsiderStatus.NO_UPDATE
        assert run_a_second.status is ConsiderStatus.CREATED
        assert run_a_second.entry is not None
        assert run_a_second.entry.evidence_refs == ("audit:a1", "audit:a2")


def test_first_failure_writes_no_rows_to_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "memory.sqlite3"
    candidate = _candidate(
        kind=MemoryKind.FAILURE,
        rule=ExtractionRule.REPEATED_FAILURE,
        failure_signature="ParserError",
    )
    with MemoryStore(
        database_path,
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: "must-not-be-used",
        secret_detector=EmptySecretDetector(),
    ) as store:
        result = store.consider(candidate)

        with sqlite3.connect(database_path) as connection:
            tables = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
            row_counts = [
                connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                for (name,) in tables
            ]

        assert result.status is ConsiderStatus.NO_UPDATE
        assert row_counts and sum(row_counts) == 0


def test_repeated_failure_preserves_evidence_reference_identity(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        kind=MemoryKind.FAILURE,
        rule=ExtractionRule.REPEATED_FAILURE,
        failure_signature="ParserError",
        evidence_refs=("AuditEvent:ABC",),
    )
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: "memory-failure",
        secret_detector=EmptySecretDetector(),
    ) as store:
        store.consider(candidate)
        result = store.consider(candidate)

        assert result.entry is not None
        assert result.entry.evidence_refs == ("AuditEvent:ABC",)


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(rule=ExtractionRule.ORDINARY_STDOUT),
        _candidate(rule=ExtractionRule.MODEL_GUESS),
        _candidate(secret_candidate=True),
    ],
    ids=["ordinary-stdout", "model-guess", "secret-candidate"],
)
def test_untrusted_candidates_are_not_persisted(
    tmp_path: Path, candidate: MemoryCandidate
) -> None:
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: "must-not-be-used",
        secret_detector=EmptySecretDetector(),
    ) as store:
        result = store.consider(candidate)

        assert result.status is ConsiderStatus.NO_UPDATE
        assert result.entry is None
        assert store.search(MemoryQuery(workspace_id="workspace-a")) == ()


def test_search_filters_workspace_and_kind(tmp_path: Path) -> None:
    ids = _ids("a-convention", "a-fix", "b-convention")
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: next(ids),
        secret_detector=EmptySecretDetector(),
    ) as store:
        store.consider(_candidate(content="Python convention"))
        store.consider(
            _candidate(
                kind=MemoryKind.VERIFIED_FIX,
                rule=ExtractionRule.VERIFIED_FIX,
                content="Python fix",
            )
        )
        store.consider(
            _candidate(workspace_id="workspace-b", content="Python convention B")
        )

        result = store.search(
            MemoryQuery(
                workspace_id="workspace-a", kinds=(MemoryKind.CONVENTION,)
            )
        )

        assert tuple(entry.memory_id for entry in result) == ("a-convention",)


def test_search_ranks_overlap_before_other_dimensions(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    ids = _ids("a-no-match", "z-match")
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: now,
        id_factory=lambda: next(ids),
        secret_detector=EmptySecretDetector(),
    ) as store:
        store.consider(_candidate(content="No match", tags=("python",)))
        store.consider(_candidate(content="Match", tags=("pytest",)))

        result = store.search(
            MemoryQuery(workspace_id="workspace-a", tags=("pytest",))
        )

        assert tuple(entry.memory_id for entry in result) == ("z-match", "a-no-match")


def test_search_ranks_confidence_when_overlap_is_equal(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    ids = _ids("a-low", "z-high")
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: now,
        id_factory=lambda: next(ids),
        secret_detector=EmptySecretDetector(),
    ) as store:
        store.consider(_candidate(content="Low", tags=("match",), confidence=10))
        store.consider(_candidate(content="High", tags=("match",), confidence=90))

        result = store.search(
            MemoryQuery(workspace_id="workspace-a", tags=("match",))
        )

        assert tuple(entry.memory_id for entry in result) == ("z-high", "a-low")


def test_search_ranks_recency_when_overlap_and_confidence_are_equal(
    tmp_path: Path,
) -> None:
    base = datetime(2026, 8, 14, tzinfo=UTC)
    times = _clock(base, base + timedelta(seconds=1))
    ids = _ids("a-old", "z-new")
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=times,
        id_factory=lambda: next(ids),
        secret_detector=EmptySecretDetector(),
    ) as store:
        store.consider(_candidate(content="Old", tags=("match",)))
        store.consider(_candidate(content="New", tags=("match",)))

        result = store.search(
            MemoryQuery(workspace_id="workspace-a", tags=("match",))
        )

        assert tuple(entry.memory_id for entry in result) == ("z-new", "a-old")


def test_search_uses_memory_id_as_final_tie_break(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    ids = _ids("z-memory", "a-memory")
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: now,
        id_factory=lambda: next(ids),
        secret_detector=EmptySecretDetector(),
    ) as store:
        store.consider(_candidate(content="Same", tags=("match",)))
        store.consider(_candidate(content="Same", tags=("match",)))

        result = store.search(
            MemoryQuery(workspace_id="workspace-a", tags=("match",))
        )

        assert tuple(entry.memory_id for entry in result) == ("a-memory", "z-memory")


def test_search_applies_top_k_independently(tmp_path: Path) -> None:
    ids = _ids("first", "second", "third")
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: next(ids),
        secret_detector=EmptySecretDetector(),
        top_k=2,
        char_limit=100,
    ) as store:
        for content in ("first", "second", "third"):
            store.consider(_candidate(content=content, tags=("match",)))

        result = store.search(
            MemoryQuery(workspace_id="workspace-a", tags=("match",))
        )

        assert tuple(entry.memory_id for entry in result) == ("first", "second")


def test_search_applies_character_limit_independently(tmp_path: Path) -> None:
    ids = _ids("first", "second", "third")
    candidate_contents = ("12345", "67890", "abcde")
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: next(ids),
        secret_detector=EmptySecretDetector(),
        top_k=5,
        char_limit=9,
    ) as store:
        for content in candidate_contents:
            store.consider(_candidate(content=content, tags=("match",)))

        result = store.search(
            MemoryQuery(workspace_id="workspace-a", tags=("match",))
        )

        assert sum(map(len, candidate_contents)) > 9
        assert tuple(entry.memory_id for entry in result) == ("first",)
        assert sum(len(entry.content) for entry in result) <= 9


def test_fixed_clock_and_ids_produce_stable_retrieval(tmp_path: Path) -> None:
    ids = _ids("memory-c", "memory-a", "memory-b")
    with MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        id_factory=lambda: next(ids),
        secret_detector=EmptySecretDetector(),
    ) as store:
        for content in ("Same C", "Same A", "Same B"):
            store.consider(_candidate(content=content, tags=("same",)))
        query = MemoryQuery(workspace_id="workspace-a", tags=("same",))

        runs = [tuple(item.memory_id for item in store.search(query)) for _ in range(3)]

        assert runs == [
            ("memory-a", "memory-b", "memory-c"),
            ("memory-a", "memory-b", "memory-c"),
            ("memory-a", "memory-b", "memory-c"),
        ]
