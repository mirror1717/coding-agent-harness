"""Private, atomic storage for caller-redacted run evidence."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self
from uuid import uuid4

from coding_agent_harness.errors import HarnessEvidenceError, HarnessValidationError

_ARTIFACT_NAME = re.compile(r"[0-9a-f]{32}\.bin\Z")
_DEFAULT_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_RUN_BYTES = 100 * 1024 * 1024
_DEFAULT_MAX_ARTIFACTS = 1_000
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class IdGenerator(Protocol):
    """Supplies injectable artifact identifiers for deterministic harness runs."""

    def new(self, namespace: str) -> str: ...


class _UuidIdGenerator:
    def new(self, namespace: str) -> str:
        return uuid4().hex


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """An immutable pointer to evidence stored within one harness run."""

    artifact_id: str
    run_id: str
    execution_id: str
    kind: str
    sha256: str
    size: int
    media_type: str
    storage_key: str
    sensitive: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class ArtifactVerification:
    """The deterministic integrity state of an artifact reference."""

    status: Literal[
        "valid", "digest_mismatch", "evidence_missing", "reference_run_mismatch"
    ]
    reason: str


class RunArtifactStore:
    """Persist already-redacted evidence in one private, run-local directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: str,
        max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES,
        max_run_bytes: int = _DEFAULT_MAX_RUN_BYTES,
        max_artifacts: int = _DEFAULT_MAX_ARTIFACTS,
        id_generator: IdGenerator | None = None,
    ) -> None:
        self._validate_run_id(run_id)
        self._validate_limit("max_artifact_bytes", max_artifact_bytes)
        self._validate_limit("max_run_bytes", max_run_bytes)
        self._validate_limit("max_artifacts", max_artifacts)

        self.run_id = run_id
        self._max_artifact_bytes = max_artifact_bytes
        self._max_run_bytes = max_run_bytes
        self._max_artifacts = max_artifacts
        self._id_generator = _UuidIdGenerator() if id_generator is None else id_generator
        self._root = Path(root).resolve(strict=True)
        self._run_directory = self._root / run_id
        self._artifact_directory = self._run_directory / "artifacts"
        self._root_fd = self._open_directory(self._root)
        self._closed = False
        try:
            self._ensure_directory(self._root_fd, run_id)
            self._run_fd = self._open_directory(run_id, dir_fd=self._root_fd)
            self._ensure_directory(self._run_fd, "artifacts")
            self._artifact_fd = self._open_directory("artifacts", dir_fd=self._run_fd)
        except BaseException:
            if hasattr(self, "_run_fd"):
                self._close_quietly(self._run_fd)
            self._close_quietly(self._root_fd)
            raise
        os.fchmod(self._run_fd, 0o700)
        os.fchmod(self._artifact_fd, 0o700)

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, *_args: object) -> None:
        try:
            self.close()
        except HarnessEvidenceError:
            if exc_type is None:
                raise

    def close(self) -> None:
        """Release persistent directory descriptors; subsequent I/O fails closed."""

        if self._closed:
            return
        self._closed = True
        close_error: OSError | None = None
        for descriptor in (self._artifact_fd, self._run_fd, self._root_fd):
            try:
                os.close(descriptor)
            except OSError as error:
                close_error = close_error or error
        if close_error is not None:
            raise HarnessEvidenceError("artifact store close failed") from close_error

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, HarnessEvidenceError, OSError):
            pass

    def put(
        self,
        *,
        execution_id: str,
        kind: str,
        data: bytes,
        media_type: str,
        sensitive: bool = False,
        truncated: bool = False,
    ) -> ArtifactRef:
        """Atomically store caller-redacted bytes and return their immutable reference."""

        self._require_open()
        if not isinstance(data, bytes):
            raise HarnessValidationError("artifact data must be bytes")
        if len(data) > self._max_artifact_bytes:
            raise HarnessValidationError("artifact exceeds configured size limit")

        with self._run_lock():
            self._require_bound_directories()
            count, total_size = self._usage()
            if count >= self._max_artifacts:
                raise HarnessValidationError("run artifact count limit exceeded")
            if total_size + len(data) > self._max_run_bytes:
                raise HarnessValidationError("run size limit exceeded")

            artifact_id = self._id_generator.new("artifact")
            if not _ARTIFACT_NAME.fullmatch(f"{artifact_id}.bin"):
                raise HarnessEvidenceError("artifact id is unsafe")
            storage_key = f"artifacts/{artifact_id}.bin"
            filename = f"{artifact_id}.bin"
            try:
                os.stat(filename, dir_fd=self._artifact_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise HarnessEvidenceError("artifact storage is unavailable") from error
            else:
                raise HarnessEvidenceError("artifact id already exists")
            self._write_atomically(filename, data)
            try:
                self._require_bound_directories()
            except HarnessEvidenceError:
                try:
                    self._unlink_artifact(filename)
                except OSError:
                    pass
                raise

        return ArtifactRef(
            artifact_id=artifact_id,
            run_id=self.run_id,
            execution_id=execution_id,
            kind=kind,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            media_type=media_type,
            storage_key=storage_key,
            sensitive=sensitive,
            truncated=truncated,
        )

    def read(self, reference: ArtifactRef) -> bytes:
        """Read the evidence bytes referred to by a valid, local reference."""

        self._require_open()
        if reference.run_id != self.run_id:
            raise HarnessEvidenceError("artifact reference belongs to a different run")
        filename = self._artifact_filename(reference)
        self._require_bound_directories()
        try:
            descriptor = os.open(filename, _FILE_READ_FLAGS, dir_fd=self._artifact_fd)
        except FileNotFoundError as error:
            raise HarnessEvidenceError("artifact file is missing") from error
        except OSError as error:
            raise HarnessEvidenceError("artifact file could not be read") from error
        try:
            if os.fstat(descriptor).st_size != reference.size:
                raise HarnessEvidenceError("artifact size does not match")
            if os.fstat(descriptor).st_size > self._max_artifact_bytes:
                raise HarnessEvidenceError("artifact exceeds configured size limit")
            data = self._read_bounded(descriptor, reference.size)
            if hashlib.sha256(data).hexdigest() != reference.sha256:
                raise HarnessEvidenceError("artifact digest does not match")
            return data
        except HarnessEvidenceError:
            raise
        except OSError as error:
            raise HarnessEvidenceError("artifact file could not be read") from error
        finally:
            self._close_quietly(descriptor)

    def verify(self, reference: ArtifactRef) -> ArtifactVerification:
        """Return a stable integrity status without exposing raw evidence bytes."""

        self._require_open()
        if reference.run_id != self.run_id:
            return ArtifactVerification(
                status="reference_run_mismatch",
                reason="artifact reference belongs to a different run",
            )
        try:
            filename = self._artifact_filename(reference)
        except HarnessEvidenceError:
            return ArtifactVerification(
                status="reference_run_mismatch",
                reason="artifact storage key is outside the run directory",
            )

        try:
            self._require_bound_directories()
            descriptor = os.open(filename, _FILE_READ_FLAGS, dir_fd=self._artifact_fd)
        except FileNotFoundError:
            return ArtifactVerification(
                status="evidence_missing", reason="artifact file is missing"
            )
        except HarnessEvidenceError:
            return ArtifactVerification(
                status="evidence_missing", reason="artifact file is missing"
            )
        except OSError:
            return ArtifactVerification(
                status="evidence_missing", reason="artifact file is missing"
            )

        try:
            size = os.fstat(descriptor).st_size
        except OSError as error:
            self._close_quietly(descriptor)
            raise HarnessEvidenceError("artifact file could not be verified") from error

        if size != reference.size:
            self._close_quietly(descriptor)
            return ArtifactVerification(
                status="digest_mismatch", reason="artifact size does not match"
            )
        if size > self._max_artifact_bytes:
            self._close_quietly(descriptor)
            return ArtifactVerification(
                status="digest_mismatch",
                reason="artifact size exceeds configured limit",
            )
        try:
            data = self._read_bounded(descriptor, size)
        except HarnessEvidenceError:
            return ArtifactVerification(
                status="digest_mismatch", reason="artifact size does not match"
            )
        except OSError as error:
            raise HarnessEvidenceError("artifact file could not be verified") from error
        finally:
            self._close_quietly(descriptor)

        if hashlib.sha256(data).hexdigest() != reference.sha256:
            return ArtifactVerification(
                status="digest_mismatch", reason="artifact digest does not match"
            )
        return ArtifactVerification(status="valid", reason="artifact digest matches")

    @staticmethod
    def _validate_limit(name: str, value: int) -> None:
        if value <= 0:
            raise HarnessValidationError(f"{name} must be positive")

    def _require_open(self) -> None:
        if self._closed:
            raise HarnessEvidenceError("artifact store is closed")

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise HarnessValidationError("run_id must be a single path component")

    def _artifact_filename(self, reference: ArtifactRef) -> str:
        key = PurePosixPath(reference.storage_key)
        if (
            key.is_absolute()
            or key.parts[:1] != ("artifacts",)
            or len(key.parts) != 2
            or not _ARTIFACT_NAME.fullmatch(key.name)
            or key.name != f"{reference.artifact_id}.bin"
        ):
            raise HarnessEvidenceError("artifact storage key is outside the run directory")
        return key.name

    @staticmethod
    def _open_directory(path: str | Path, *, dir_fd: int | None = None) -> int:
        try:
            return os.open(path, _DIRECTORY_FLAGS, dir_fd=dir_fd)
        except OSError as error:
            raise HarnessEvidenceError("artifact storage directory is unavailable") from error

    @staticmethod
    def _ensure_directory(parent_fd: int, name: str) -> None:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise HarnessEvidenceError("artifact storage directory is unavailable") from error
        RunArtifactStore._chmod_private_directory(parent_fd, name)

    @staticmethod
    def _chmod_private_directory(parent_fd: int, name: str) -> None:
        try:
            os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
        except ValueError:
            if sys.platform != "linux" or not hasattr(os, "O_PATH"):
                raise HarnessEvidenceError("safe directory chmod is unavailable")
            try:
                descriptor = os.open(
                    name,
                    os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY,
                    dir_fd=parent_fd,
                )
            except OSError as error:
                raise HarnessEvidenceError("artifact storage directory is unavailable") from error
            try:
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise HarnessEvidenceError("artifact storage directory is unavailable")
                os.chmod(f"/proc/self/fd/{descriptor}", 0o700)
            except OSError as error:
                raise HarnessEvidenceError("artifact storage directory is unavailable") from error
            finally:
                RunArtifactStore._close_quietly(descriptor)
        except OSError as error:
            raise HarnessEvidenceError("artifact storage directory is unavailable") from error
        descriptor = RunArtifactStore._open_directory(name, dir_fd=parent_fd)
        try:
            mode = os.fstat(descriptor).st_mode
            if (mode & 0o777) != 0o700:
                raise HarnessEvidenceError("artifact storage directory is unavailable")
            os.fchmod(descriptor, 0o700)
        finally:
            RunArtifactStore._close_quietly(descriptor)

    def _require_bound_directories(self) -> None:
        run_fd: int | None = None
        root_fd: int | None = None
        artifact_fd: int | None = None
        primary_error: BaseException | None = None
        try:
            root_fd = os.open(self._root, _DIRECTORY_FLAGS)
            if not self._same_inode(root_fd, self._root_fd):
                raise HarnessEvidenceError("configured root is unavailable")
            run_fd = os.open(self.run_id, _DIRECTORY_FLAGS, dir_fd=self._root_fd)
            artifact_fd = os.open("artifacts", _DIRECTORY_FLAGS, dir_fd=run_fd)
            if not self._same_inode(run_fd, self._run_fd) or not self._same_inode(
                artifact_fd, self._artifact_fd
            ):
                raise HarnessEvidenceError("artifact storage directory is unavailable")
        except OSError as error:
            message = (
                "configured root is unavailable"
                if root_fd is None
                else "artifact storage directory is unavailable"
            )
            primary_error = HarnessEvidenceError(message)
            raise primary_error from error
        except BaseException as error:
            primary_error = error
            raise
        finally:
            for descriptor in (artifact_fd, run_fd, root_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError as error:
                        if primary_error is None:
                            raise HarnessEvidenceError(
                                "artifact storage directory is unavailable"
                            ) from error

    @staticmethod
    def _same_inode(first: int, second: int) -> bool:
        return os.fstat(first).st_dev == os.fstat(second).st_dev and os.fstat(
            first
        ).st_ino == os.fstat(second).st_ino

    def _usage(self) -> tuple[int, int]:
        try:
            files = [
                name
                for name in os.listdir(self._artifact_fd)
                if _ARTIFACT_NAME.fullmatch(name)
            ]
            return len(files), sum(
                os.stat(name, dir_fd=self._artifact_fd, follow_symlinks=False).st_size
                for name in files
            )
        except OSError as error:
            raise HarnessEvidenceError("artifact usage is unavailable") from error

    @contextmanager
    def _run_lock(self) -> Iterator[None]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._artifact_fd,
            )
            os.fchmod(descriptor, 0o600)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise HarnessEvidenceError("artifact lock is unavailable") from error
        assert descriptor is not None
        primary_error: BaseException | None = None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as error:
                raise HarnessEvidenceError("artifact lock is unavailable") from error
            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                os.close(descriptor)
            except OSError as error:
                if primary_error is None:
                    raise HarnessEvidenceError("artifact lock is unavailable") from error

    def _write_atomically(self, destination: str, data: bytes) -> None:
        temporary_name = f".artifact-{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary_name, _FILE_WRITE_FLAGS, 0o600, dir_fd=self._artifact_fd
            )
        except OSError as error:
            raise HarnessEvidenceError("artifact could not be persisted") from error
        descriptor_open = True
        replaced = False
        try:
            os.fchmod(descriptor, 0o600)
            temporary_file = os.fdopen(descriptor, "wb")
            descriptor_open = False
            with temporary_file:
                temporary_file.write(data)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.link(
                temporary_name,
                destination,
                src_dir_fd=self._artifact_fd,
                dst_dir_fd=self._artifact_fd,
                follow_symlinks=False,
            )
            replaced = True
            os.unlink(temporary_name, dir_fd=self._artifact_fd)
            os.fsync(self._artifact_fd)
        except FileExistsError as error:
            raise HarnessEvidenceError("artifact id already exists") from error
        except OSError as error:
            if replaced:
                try:
                    self._unlink_artifact(destination)
                except OSError:
                    pass
            raise HarnessEvidenceError("artifact could not be persisted") from error
        finally:
            if descriptor_open:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                os.unlink(temporary_name, dir_fd=self._artifact_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @staticmethod
    def _read_bounded(descriptor: int, expected_size: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        with os.fdopen(descriptor, "rb", closefd=False) as artifact_file:
            while total <= expected_size:
                chunk = artifact_file.read(min(64 * 1024, expected_size + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        data = b"".join(chunks)
        if len(data) != expected_size:
            raise HarnessEvidenceError("artifact size does not match")
        return data

    def _unlink_artifact(self, filename: str) -> None:
        try:
            os.unlink(filename, dir_fd=self._artifact_fd)
        except FileNotFoundError:
            pass

    @staticmethod
    def _close_quietly(descriptor: int) -> None:
        try:
            os.close(descriptor)
        except OSError:
            pass
