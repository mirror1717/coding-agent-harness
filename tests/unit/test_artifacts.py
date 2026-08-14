"""Tests for private, run-local artifact evidence storage."""

from __future__ import annotations

import os
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Self

import pytest

from coding_agent_harness.artifacts import ArtifactRef, RunArtifactStore
from coding_agent_harness.errors import HarnessEvidenceError, HarnessValidationError


class FakeIdGenerator:
    def __init__(self, value: str) -> None:
        self.value = value
        self.namespaces: list[str] = []

    def new(self, namespace: str) -> str:
        self.namespaces.append(namespace)
        return self.value

    def __bool__(self) -> bool:
        return False


def _put_one_artifact(store: RunArtifactStore, execution_id: str) -> str:
    try:
        store.put(
            execution_id=execution_id,
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )
    except HarnessValidationError:
        return "rejected"
    return "stored"


def _put_one_artifact_in_process(
    root: str, barrier: Any, outcomes: Any, execution_id: str
) -> None:
    store = RunArtifactStore(root, run_id="run-1", max_artifacts=1)
    barrier.wait()
    outcomes.put(_put_one_artifact(store, execution_id))


def _artifact_path(root: Path, reference: ArtifactRef) -> Path:
    return root / reference.run_id / reference.storage_key


def test_store_exposes_no_public_path_capability(tmp_path: Path) -> None:
    """Callers must use ArtifactRef operations, not a path vulnerable after return."""

    store = RunArtifactStore(tmp_path, run_id="run-1")

    assert not hasattr(store, "path_for")


def test_read_rejects_reference_from_another_run(tmp_path: Path) -> None:
    """A foreign run reference must not read a same-key artifact from this run."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )

    with pytest.raises(HarnessEvidenceError, match="belongs to a different run"):
        store.read(replace(reference, run_id="run-2"))


def test_read_rejects_same_size_digest_mismatch(tmp_path: Path) -> None:
    """Read must not release tampered same-size evidence to a feedback consumer."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    _artifact_path(tmp_path, reference).write_bytes(b"tampered output")

    with pytest.raises(HarnessEvidenceError, match="digest does not match"):
        store.read(reference)


def test_put_uses_injected_id_generator_for_deterministic_storage(tmp_path: Path) -> None:
    """Injected IDs must determine both the reference and safe storage key."""

    generator = FakeIdGenerator("a" * 32)
    store = RunArtifactStore(tmp_path, run_id="run-1", id_generator=generator)

    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )

    assert generator.namespaces == ["artifact"]
    assert reference.artifact_id == "a" * 32
    assert reference.storage_key == f"artifacts/{'a' * 32}.bin"


def test_put_rejects_unsafe_injected_artifact_id(tmp_path: Path) -> None:
    """Injected IDs cannot introduce a path or broaden the artifact namespace."""

    store = RunArtifactStore(
        tmp_path, run_id="run-1", id_generator=FakeIdGenerator("../unsafe")
    )

    with pytest.raises(HarnessEvidenceError, match="artifact id is unsafe"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )


def test_repeated_injected_id_does_not_overwrite_first_artifact(tmp_path: Path) -> None:
    """A deterministic ID collision must fail before it can replace stored evidence."""

    store = RunArtifactStore(
        tmp_path, run_id="run-1", id_generator=FakeIdGenerator("b" * 32)
    )
    first = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )

    with pytest.raises(HarnessEvidenceError, match="artifact id already exists"):
        store.put(
            execution_id="execution-2",
            kind="stderr",
            data=b"redacted failure",
            media_type="text/plain",
        )

    assert store.read(first) == b"redacted output"


def test_link_publication_rejects_race_sentinel_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact appearing after admission must win link publication unchanged."""

    artifact_id = "c" * 32
    store = RunArtifactStore(
        tmp_path, run_id="run-1", id_generator=FakeIdGenerator(artifact_id)
    )
    artifact_directory = tmp_path / "run-1" / "artifacts"
    sentinel = artifact_directory / "sentinel"
    sentinel.write_bytes(b"sentinel evidence")

    from coding_agent_harness import artifacts

    original_link = artifacts.os.link

    def publish_sentinel(*args: Any, **kwargs: Any) -> None:
        original_link(sentinel, args[1], dst_dir_fd=kwargs["dst_dir_fd"])
        original_link(*args, **kwargs)

    monkeypatch.setattr(artifacts.os, "link", publish_sentinel)

    with pytest.raises(HarnessEvidenceError, match="artifact id already exists"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )

    destination = artifact_directory / f"{artifact_id}.bin"
    assert destination.read_bytes() == b"sentinel evidence"


def test_read_primary_digest_error_survives_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the read FD must not mask a detected digest mismatch."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    _artifact_path(tmp_path, reference).write_bytes(b"tampered output")
    monkeypatch.setattr(store, "_require_bound_directories", lambda: None)

    from coding_agent_harness import artifacts

    monkeypatch.setattr(artifacts.os, "close", lambda _descriptor: (_ for _ in ()).throw(OSError("close blocked")))

    with pytest.raises(HarnessEvidenceError, match="digest does not match"):
        store.read(reference)


def test_verify_status_survives_close_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification must return its integrity status even when FD cleanup fails."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    _artifact_path(tmp_path, reference).write_bytes(b"tampered output")
    monkeypatch.setattr(store, "_require_bound_directories", lambda: None)

    from coding_agent_harness import artifacts

    monkeypatch.setattr(artifacts.os, "close", lambda _descriptor: (_ for _ in ()).throw(OSError("close blocked")))

    result = store.verify(reference)

    assert result.status == "digest_mismatch"


def test_constructor_primary_error_survives_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructor cleanup must not replace the original evidence failure."""

    from coding_agent_harness import artifacts

    monkeypatch.setattr(
        RunArtifactStore,
        "_ensure_directory",
        staticmethod(lambda *_args: (_ for _ in ()).throw(HarnessEvidenceError("setup failed"))),
    )
    monkeypatch.setattr(artifacts.os, "close", lambda _descriptor: (_ for _ in ()).throw(OSError("close blocked")))

    with pytest.raises(HarnessEvidenceError, match="setup failed"):
        RunArtifactStore(tmp_path, run_id="run-1")


def test_context_body_error_survives_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context cleanup must not replace an exception raised by the with body."""

    store = RunArtifactStore(tmp_path, run_id="run-1")

    from coding_agent_harness import artifacts

    monkeypatch.setattr(artifacts.os, "close", lambda _descriptor: (_ for _ in ()).throw(OSError("close blocked")))

    with pytest.raises(RuntimeError, match="body failed"), store:
        raise RuntimeError("body failed")


def test_put_cleans_final_artifact_when_directory_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed post-replace fsync must not leave evidence that has no reference."""

    store = RunArtifactStore(tmp_path, run_id="run-1")

    from coding_agent_harness import artifacts

    original_fsync = artifacts.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync blocked")
        original_fsync(descriptor)

    monkeypatch.setattr(artifacts.os, "fsync", fail_directory_fsync)

    with pytest.raises(HarnessEvidenceError, match="could not be persisted"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )

    artifact_directory = tmp_path / "run-1" / "artifacts"
    assert list(artifact_directory.glob("*.bin")) == []
    assert list(artifact_directory.glob(".artifact-*.tmp")) == []


def test_private_modes_survive_restrictive_umask(tmp_path: Path) -> None:
    """A restrictive process umask must not make private evidence directories unusable."""

    previous_umask = os.umask(0o777)
    try:
        store = RunArtifactStore(tmp_path, run_id="run-1")
        reference = store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE((tmp_path / "run-1").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "run-1" / "artifacts").stat().st_mode) == 0o700
    assert stat.S_IMODE(_artifact_path(tmp_path, reference).stat().st_mode) == 0o600


def test_private_lock_survives_restrictive_umask_across_two_puts(tmp_path: Path) -> None:
    """A lock created under umask 0777 must remain usable for later evidence writes."""

    previous_umask = os.umask(0o777)
    try:
        store = RunArtifactStore(tmp_path, run_id="run-1")
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )
        second = store.put(
            execution_id="execution-2",
            kind="stderr",
            data=b"redacted failure",
            media_type="text/plain",
        )
    finally:
        os.umask(previous_umask)

    lock_path = tmp_path / "run-1" / "artifacts" / ".lock"
    assert second.execution_id == "execution-2"
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_fdopen_failure_closes_raw_temp_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed fdopen must close the raw temporary descriptor before cleanup."""

    store = RunArtifactStore(tmp_path, run_id="run-1")

    from coding_agent_harness import artifacts

    original_open = artifacts.os.open
    original_close = artifacts.os.close
    temporary_descriptors: list[int] = []
    closed_descriptors: list[int] = []

    def record_temp_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.startswith(".artifact-"):
            temporary_descriptors.append(descriptor)
        return descriptor

    def record_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(artifacts.os, "open", record_temp_open)
    monkeypatch.setattr(artifacts.os, "close", record_close)
    monkeypatch.setattr(
        artifacts.os, "fdopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )

    with pytest.raises(HarnessEvidenceError, match="could not be persisted"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )

    assert temporary_descriptors
    assert temporary_descriptors[-1] in closed_descriptors


def test_temp_creation_failure_is_a_stable_evidence_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temporary-file exhaustion must not leak an OS error or create evidence remnants."""

    store = RunArtifactStore(tmp_path, run_id="run-1")

    from coding_agent_harness import artifacts

    original_open = artifacts.os.open

    def fail_temp_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if isinstance(path, str) and path.startswith(".artifact-"):
            raise OSError(28, "No space left on device")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts.os, "open", fail_temp_open)

    with pytest.raises(HarnessEvidenceError, match="could not be persisted"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )

    artifact_directory = tmp_path / "run-1" / "artifacts"
    assert list(artifact_directory.glob("*.bin")) == []
    assert list(artifact_directory.glob(".artifact-*.tmp")) == []


@pytest.mark.parametrize("operation", ["flock", "listdir"])
def test_put_translates_lock_and_usage_io_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Lock and quota-scan I/O failures must not escape as raw OS errors."""

    store = RunArtifactStore(tmp_path, run_id="run-1")

    from coding_agent_harness import artifacts

    if operation == "flock":
        monkeypatch.setattr(
            artifacts.fcntl,
            "flock",
            lambda *_args: (_ for _ in ()).throw(OSError("lock blocked")),
        )
    else:
        monkeypatch.setattr(
            artifacts.os,
            "listdir",
            lambda *_args: (_ for _ in ()).throw(OSError("scan blocked")),
        )

    with pytest.raises(HarnessEvidenceError):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )


def test_temp_cleanup_failure_does_not_replace_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort staging cleanup must preserve the original write failure."""

    store = RunArtifactStore(tmp_path, run_id="run-1")

    from coding_agent_harness import artifacts

    monkeypatch.setattr(
        artifacts.os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace blocked"))
    )
    original_unlink = artifacts.os.unlink

    def fail_temp_unlink(
        path: str | Path, *args: object, **kwargs: int | None
    ) -> None:
        if isinstance(path, str) and path.startswith(".artifact-"):
            raise OSError("cleanup blocked")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(artifacts.os, "unlink", fail_temp_unlink)

    with pytest.raises(HarnessEvidenceError, match="could not be persisted"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )


def test_final_cleanup_failure_does_not_replace_binding_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort final cleanup must preserve the post-write binding failure."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    original_require = store._require_bound_directories
    checks = 0

    def fail_after_write() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise HarnessEvidenceError("binding failed")
        original_require()

    monkeypatch.setattr(store, "_require_bound_directories", fail_after_write)
    monkeypatch.setattr(
        store,
        "_unlink_artifact",
        lambda _filename: (_ for _ in ()).throw(OSError("cleanup blocked")),
    )

    with pytest.raises(HarnessEvidenceError, match="binding failed"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )


def test_read_rejects_file_growth_after_initial_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read must use bounded reads and reject bytes appended after its initial stat."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )

    from coding_agent_harness import artifacts

    original_fdopen = artifacts.os.fdopen

    class GrowingFile:
        def __init__(self, artifact_file: Any) -> None:
            self._artifact_file = artifact_file
            self._reads = 0

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self._artifact_file.close()

        def read(self, size: int = -1) -> bytes:
            if size < 0:
                raise AssertionError("read used an unbounded size")
            self._reads += 1
            if self._reads == 1:
                return self._artifact_file.read(size) + b"x"
            return b""

    monkeypatch.setattr(
        artifacts.os,
        "fdopen",
        lambda descriptor, mode, **kwargs: GrowingFile(
            original_fdopen(descriptor, mode, **kwargs)
        ),
    )

    with pytest.raises(HarnessEvidenceError, match="size does not match"):
        store.read(reference)


def test_store_rejects_configured_root_path_replaced_after_initialization(
    tmp_path: Path,
) -> None:
    """A store must not keep writing through an FD after its configured root is replaced."""

    root = tmp_path / "evidence-root"
    root.mkdir()
    store = RunArtifactStore(root, run_id="run-1")
    root.rename(tmp_path / "moved-root")
    root.mkdir()

    with pytest.raises(HarnessEvidenceError, match="configured root is unavailable"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )

    assert list((tmp_path / "moved-root" / "run-1" / "artifacts").glob("*.bin")) == []


def test_put_removes_final_artifact_when_post_write_binding_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed post-write namespace check must not strand an unreferenced artifact."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    original_require = store._require_bound_directories
    checks = 0

    def fail_after_write() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise HarnessEvidenceError("artifact storage directory is unavailable")
        original_require()

    monkeypatch.setattr(store, "_require_bound_directories", fail_after_write)

    with pytest.raises(HarnessEvidenceError, match="storage directory is unavailable"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )

    assert list((tmp_path / "run-1" / "artifacts").glob("*.bin")) == []


def test_close_and_context_manager_release_store_file_descriptors(tmp_path: Path) -> None:
    """Persistent directory descriptors must be released and reject later I/O."""

    before = len(list(Path("/proc/self/fd").iterdir()))
    with RunArtifactStore(tmp_path, run_id="run-1") as store:
        during = len(list(Path("/proc/self/fd").iterdir()))
        assert during >= 3
    after = len(list(Path("/proc/self/fd").iterdir()))

    assert after <= before + 1
    with pytest.raises(HarnessEvidenceError, match="store is closed"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )


def test_verify_artifact_symlink_replacement_is_non_valid(tmp_path: Path) -> None:
    """A symlinked artifact must report integrity failure without exposing an I/O error."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    artifact_path = _artifact_path(tmp_path, reference)
    artifact_path.unlink()
    artifact_path.symlink_to(tmp_path / "outside")

    result = store.verify(reference)

    assert result.status == "evidence_missing"
    assert result.reason == "artifact file is missing"


def test_verify_checks_size_before_reading_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A size mismatch must return before reading potentially huge evidence bytes."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )

    from coding_agent_harness import artifacts

    original_fstat = artifacts.os.fstat
    original_fdopen = artifacts.os.fdopen

    def enlarged_size(descriptor: int) -> os.stat_result:
        values = list(original_fstat(descriptor))
        values[6] += 1
        return os.stat_result(values)

    class NoReadFile:
        def __init__(self, artifact_file: Any) -> None:
            self._artifact_file = artifact_file

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self._artifact_file.close()

        def fileno(self) -> int:
            return self._artifact_file.fileno()

        def read(self) -> bytes:
            raise AssertionError("verify read bytes despite the size mismatch")

    monkeypatch.setattr(artifacts.os, "fstat", enlarged_size)
    monkeypatch.setattr(
        artifacts.os,
        "fdopen",
        lambda descriptor, mode: NoReadFile(original_fdopen(descriptor, mode)),
    )
    monkeypatch.setattr(store, "_require_bound_directories", lambda: None)

    result = store.verify(reference)

    assert result.status == "digest_mismatch"
    assert result.reason == "artifact size does not match"


def test_verify_rejects_oversized_artifact_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted file over the configured limit must not be loaded for hashing."""

    store = RunArtifactStore(tmp_path, run_id="run-1", max_artifact_bytes=4)
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"1234",
        media_type="text/plain",
    )
    _artifact_path(tmp_path, reference).write_bytes(b"12345")
    oversized_reference = replace(
        reference,
        sha256="5994471abb01112afcc18159f6cc74b4f511b99806da59b3caf5a9c173cacfc5",
        size=5,
    )

    from coding_agent_harness import artifacts

    original_fdopen = artifacts.os.fdopen

    class NoReadFile:
        def __init__(self, artifact_file: Any) -> None:
            self._artifact_file = artifact_file

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self._artifact_file.close()

        def read(self) -> bytes:
            raise AssertionError("verify read bytes despite the configured size limit")

    monkeypatch.setattr(
        artifacts.os,
        "fdopen",
        lambda descriptor, mode, **kwargs: NoReadFile(
            original_fdopen(descriptor, mode, **kwargs)
        ),
    )

    result = store.verify(oversized_reference)

    assert result.status == "digest_mismatch"
    assert result.reason == "artifact size exceeds configured limit"


def test_read_rejects_oversized_artifact_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read must not materialize an artifact larger than its configured limit."""

    store = RunArtifactStore(tmp_path, run_id="run-1", max_artifact_bytes=4)
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"1234",
        media_type="text/plain",
    )
    _artifact_path(tmp_path, reference).write_bytes(b"12345")

    from coding_agent_harness import artifacts

    original_fdopen = artifacts.os.fdopen

    class NoReadFile:
        def __init__(self, artifact_file: Any) -> None:
            self._artifact_file = artifact_file

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self._artifact_file.close()

        def read(self) -> bytes:
            raise AssertionError("read bytes despite the configured size limit")

    monkeypatch.setattr(
        artifacts.os,
        "fdopen",
        lambda descriptor, mode, **kwargs: NoReadFile(
            original_fdopen(descriptor, mode, **kwargs)
        ),
    )

    with pytest.raises(HarnessEvidenceError, match="exceeds configured size limit"):
        store.read(replace(reference, size=5))


def test_bound_directory_check_closes_run_descriptor_when_artifact_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed child-directory open must not leak the already-opened run descriptor."""

    store = RunArtifactStore(tmp_path, run_id="run-1")

    from coding_agent_harness import artifacts

    original_open = artifacts.os.open
    original_close = artifacts.os.close
    opened_run_descriptors: list[int] = []
    closed_descriptors: list[int] = []

    def fail_artifact_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "artifacts":
            raise OSError("artifact directory unavailable")
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "run-1":
            opened_run_descriptors.append(descriptor)
        return descriptor

    def record_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(artifacts.os, "open", fail_artifact_open)
    monkeypatch.setattr(artifacts.os, "close", record_close)

    with pytest.raises(HarnessEvidenceError, match="storage directory is unavailable"):
        store._require_bound_directories()

    assert opened_run_descriptors
    assert opened_run_descriptors[-1] in closed_descriptors


def test_put_is_private_and_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken replace/write sequence must not leave a readable partial artifact."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    replaced: list[tuple[Path, Path]] = []

    from coding_agent_harness import artifacts

    original_link = artifacts.os.link

    def record_link(*args: Any, **_kwargs: Any) -> None:
        source, destination = args[:2]
        replaced.append((Path(source), Path(destination)))
        original_link(*args, **_kwargs)

    monkeypatch.setattr(artifacts.os, "link", record_link)

    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
        sensitive=True,
    )

    run_directory = tmp_path / "run-1"
    artifact_path = _artifact_path(tmp_path, reference)
    assert store.read(reference) == b"redacted output"
    assert store.verify(reference).status == "valid"
    assert store.verify(reference).reason == "artifact digest matches"
    assert reference.run_id == "run-1"
    assert reference.storage_key.startswith("artifacts/")
    assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert len(replaced) == 1
    temporary_path, replacement_path = replaced[0]
    assert replacement_path.name == artifact_path.name
    assert temporary_path.name.startswith(".artifact-")
    assert temporary_path.name.endswith(".tmp")


def test_put_cleans_up_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement leaves neither a visible target nor a staging file."""

    store = RunArtifactStore(tmp_path, run_id="run-1")

    from coding_agent_harness import artifacts

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("link blocked")

    monkeypatch.setattr(artifacts.os, "link", fail_link)

    with pytest.raises(HarnessEvidenceError, match="could not be persisted"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )

    assert list((tmp_path / "run-1" / "artifacts").iterdir()) == [
        tmp_path / "run-1" / "artifacts" / ".lock"
    ]


def test_digest_mismatch_detected(tmp_path: Path) -> None:
    """Changing persisted evidence after put must invalidate its digest."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stderr",
        data=b"redacted failure",
        media_type="text/plain",
    )
    _artifact_path(tmp_path, reference).write_bytes(b"modified output!")

    result = store.verify(reference)

    assert result.status == "digest_mismatch"
    assert result.reason == "artifact digest does not match"


def test_missing_artifact_reported(tmp_path: Path) -> None:
    """A deleted artifact must be distinguishable from a valid evidence chain."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="pytest_report",
        data=b"redacted report",
        media_type="application/json",
    )
    _artifact_path(tmp_path, reference).unlink()

    result = store.verify(reference)

    assert result.status == "evidence_missing"
    assert result.reason == "artifact file is missing"


def test_storage_key_cannot_escape_run(tmp_path: Path) -> None:
    """A forged relative key must never resolve outside the selected run directory."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="diff",
        data=b"redacted diff",
        media_type="text/x-diff",
    )
    escaped_reference = replace(reference, storage_key="../outside")

    result = store.verify(escaped_reference)
    assert result.status == "reference_run_mismatch"
    assert result.reason == "artifact storage key is outside the run directory"


def test_storage_key_must_belong_to_its_artifact_id(tmp_path: Path) -> None:
    """Changing an ID alone must not redirect a reference to another artifact's bytes."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    mismatched_reference = replace(reference, artifact_id="f" * 32)

    with pytest.raises(HarnessEvidenceError, match="storage key"):
        store.read(mismatched_reference)


def test_run_directory_symlink_is_rejected(tmp_path: Path) -> None:
    """A run directory symlink must not redirect evidence outside the configured root."""

    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    (tmp_path / "run-1").symlink_to(outside_directory, target_is_directory=True)

    with pytest.raises(HarnessEvidenceError, match="storage directory is unavailable"):
        RunArtifactStore(tmp_path, run_id="run-1")


def test_symlink_directory_setup_never_changes_external_permissions(tmp_path: Path) -> None:
    """A rejected directory symlink must not chmod its external target."""

    run_directory = tmp_path / "run-1"
    run_directory.mkdir()
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir(mode=0o755)
    os.chmod(outside_directory, 0o755)
    (run_directory / "artifacts").symlink_to(outside_directory, target_is_directory=True)

    with pytest.raises(HarnessEvidenceError):
        RunArtifactStore(tmp_path, run_id="run-1")

    assert stat.S_IMODE(outside_directory.stat().st_mode) == 0o755


def test_artifact_directory_symlink_is_rejected(tmp_path: Path) -> None:
    """An artifact directory symlink must not redirect evidence outside its run."""

    run_directory = tmp_path / "run-1"
    run_directory.mkdir()
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    (run_directory / "artifacts").symlink_to(
        outside_directory, target_is_directory=True
    )

    with pytest.raises(HarnessEvidenceError, match="storage directory is unavailable"):
        RunArtifactStore(tmp_path, run_id="run-1")


def test_put_does_not_follow_directory_replaced_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing a directory mid-put must not send bytes to the replacement target."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    original_usage = store._usage

    def replace_directory_after_usage() -> tuple[int, int]:
        usage = original_usage()
        run_directory = tmp_path / "run-1"
        run_directory.rename(tmp_path / "moved-run")
        run_directory.symlink_to(outside_directory, target_is_directory=True)
        return usage

    monkeypatch.setattr(store, "_usage", replace_directory_after_usage)

    with pytest.raises(HarnessEvidenceError):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"redacted output",
            media_type="text/plain",
        )

    assert list(outside_directory.iterdir()) == []


def test_run_quota_allows_only_one_concurrent_put(tmp_path: Path) -> None:
    """Without a run-wide lock, concurrent writers can both pass the count check."""

    stores = [
        RunArtifactStore(tmp_path, run_id="run-1", max_artifacts=1)
        for _ in range(16)
    ]
    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        outcomes = list(
            executor.map(
                _put_one_artifact,
                stores,
                [f"execution-{index}" for index in range(len(stores))],
            )
        )

    assert outcomes.count("stored") == 1
    assert outcomes.count("rejected") == len(stores) - 1


def test_run_quota_allows_only_one_cross_process_put(tmp_path: Path) -> None:
    """A file lock serializes quota admission for independent process stores."""

    context = get_context("fork")
    barrier = context.Barrier(4)
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_put_one_artifact_in_process,
            args=(str(tmp_path), barrier, outcomes, f"execution-{index}"),
        )
        for index in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    process_outcomes = [outcomes.get(timeout=1) for _ in processes]
    assert process_outcomes.count("stored") == 1
    assert process_outcomes.count("rejected") == 3


def test_verify_deleted_run_directory_reports_missing_evidence(tmp_path: Path) -> None:
    """Deleted run directories are missing evidence, not incidental filesystem errors."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    shutil.rmtree(tmp_path / "run-1")

    result = store.verify(reference)

    assert result.status == "evidence_missing"
    assert result.reason == "artifact file is missing"


def test_verify_replaced_artifact_directory_reports_missing_evidence(
    tmp_path: Path,
) -> None:
    """A replaced artifact directory is missing evidence, not a reference mismatch."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    artifact_directory = tmp_path / "run-1" / "artifacts"
    artifact_directory.rename(tmp_path / "moved-artifacts")
    artifact_directory.mkdir()

    result = store.verify(reference)

    assert result.status == "evidence_missing"
    assert result.reason == "artifact file is missing"


def test_verify_detects_reference_size_mismatch(tmp_path: Path) -> None:
    """Forging only the declared size must not leave evidence marked valid."""

    store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )

    result = store.verify(replace(reference, size=reference.size + 1))

    assert result.status == "digest_mismatch"
    assert result.reason == "artifact size does not match"


def test_limits_are_enforced_before_an_artifact_is_persisted(tmp_path: Path) -> None:
    """Removing limit checks must allow evidence beyond its configured run budget."""

    store = RunArtifactStore(
        tmp_path,
        run_id="run-1",
        max_artifact_bytes=4,
        max_run_bytes=5,
        max_artifacts=1,
    )

    with pytest.raises(HarnessValidationError, match="artifact exceeds"):
        store.put(
            execution_id="execution-1",
            kind="stdout",
            data=b"12345",
            media_type="text/plain",
        )

    reference = store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"1234",
        media_type="text/plain",
    )
    assert reference.size == 4

    with pytest.raises(HarnessValidationError, match="count limit"):
        store.put(
            execution_id="execution-2",
            kind="stderr",
            data=b"x",
            media_type="text/plain",
        )


def test_run_size_limit_is_enforced(tmp_path: Path) -> None:
    """Removing aggregate accounting must allow total evidence beyond the run budget."""

    store = RunArtifactStore(
        tmp_path,
        run_id="run-1",
        max_artifact_bytes=4,
        max_run_bytes=5,
        max_artifacts=2,
    )
    store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"1234",
        media_type="text/plain",
    )

    with pytest.raises(HarnessValidationError, match="run size limit"):
        store.put(
            execution_id="execution-2",
            kind="stderr",
            data=b"12",
            media_type="text/plain",
        )


def test_reference_from_another_run_is_reported_deterministically(tmp_path: Path) -> None:
    """A reference associated with another run must not be treated as missing evidence."""

    source_store = RunArtifactStore(tmp_path, run_id="run-1")
    reference = source_store.put(
        execution_id="execution-1",
        kind="stdout",
        data=b"redacted output",
        media_type="text/plain",
    )
    other_store = RunArtifactStore(tmp_path, run_id="run-2")

    result = other_store.verify(reference)

    assert result.status == "reference_run_mismatch"
    assert result.reason == "artifact reference belongs to a different run"


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_limits_are_rejected(tmp_path: Path, value: int) -> None:
    """A non-positive configured limit would make storage limits undefined."""

    with pytest.raises(HarnessValidationError, match="must be positive"):
        RunArtifactStore(tmp_path, run_id="run-1", max_artifact_bytes=value)


def test_artifact_ref_is_immutable() -> None:
    """Mutable evidence references could silently sever audit evidence from its digest."""

    reference = ArtifactRef(
        artifact_id="artifact-1",
        run_id="run-1",
        execution_id="execution-1",
        kind="stdout",
        sha256="0" * 64,
        size=0,
        media_type="text/plain",
        storage_key="artifacts/artifact-1.bin",
        sensitive=False,
        truncated=False,
    )

    with pytest.raises(AttributeError):
        reference.kind = "stderr"  # type: ignore[misc]
