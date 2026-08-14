"""Non-overridable checks for actions and observed sandbox enforcement."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from .domain import NormalizedAction
from .errors import HarnessError, HarnessSecurityError, HarnessValidationError
from .tools import ToolRegistry

_RESERVED_SANDBOX_KEYS = frozenset(
    {
        "network",
        "mounts",
        "privileged",
        "resources",
        "docker_socket",
        "cpu",
        "memory",
        "pids",
    }
)
_FILE_ACTIONS = frozenset({"list_files", "read_file", "write_file"})
_PROCESS_ACTIONS = frozenset({"shell", "pytest"})


class GuardrailDecision(str, Enum):
    PASS = "PASS"
    DENY = "DENY"


class GuardrailReason(str, Enum):
    PASS = "PASS"
    PATH_ESCAPE = "GUARDRAIL_DENY_PATH_ESCAPE"
    DOCKER_SOCKET = "GUARDRAIL_DENY_DOCKER_SOCKET"
    RESERVED_SANDBOX_CONTROL = "GUARDRAIL_DENY_RESERVED_SANDBOX_CONTROL"
    RESOURCE_LIMIT = "GUARDRAIL_DENY_RESOURCE_LIMIT"
    INTERPRETER_EVAL = "GUARDRAIL_DENY_INTERPRETER_EVAL"
    SECRET_CONTAINMENT = "GUARDRAIL_DENY_SECRET_CONTAINMENT"
    SECRET_DETECTOR_UNAVAILABLE = "GUARDRAIL_DENY_SECRET_DETECTOR_UNAVAILABLE"
    WORKSPACE_MISMATCH = "GUARDRAIL_DENY_WORKSPACE_MISMATCH"
    MALFORMED_ACTION = "GUARDRAIL_DENY_MALFORMED_ACTION"


@dataclass(frozen=True)
class GuardrailResult:
    decision: GuardrailDecision
    reason: GuardrailReason


@runtime_checkable
class SecretDetector(Protocol):
    def contains_secret(self, value: str | bytes) -> bool:
        """Return whether a known exact host secret occurs in ``value``."""


class EmptySecretDetector:
    """Explicit detector for runs that have no host credentials to protect."""

    def contains_secret(self, value: str | bytes) -> bool:
        del value
        return False


class BoundaryIntegrityError(HarnessSecurityError):
    """Observed sandbox enforcement does not satisfy the hard boundary."""

    ERROR_CODE = "BOUNDARY_INTEGRITY_ERROR"

    def __init__(self, message: str) -> None:
        HarnessError.__init__(self, self.ERROR_CODE, message)


@dataclass(frozen=True)
class _ImagePolicy:
    version: int
    cpu_limit: float
    memory_limit_bytes: int
    pids_limit: int
    timeout_seconds: int
    network_mode: str
    privileged: bool
    run_as_non_root: bool
    root_filesystem_read_only: bool
    cap_drop_all: bool
    no_new_privileges: bool
    docker_socket_mounted: bool
    workspace_mount_target: str
    workspace_mount_read_only: bool
    maximum_mounts: int


_FROZEN_IMAGE_POLICY_V1 = _ImagePolicy(
    version=1,
    cpu_limit=1.0,
    memory_limit_bytes=1_073_741_824,
    pids_limit=128,
    timeout_seconds=300,
    network_mode="none",
    privileged=False,
    run_as_non_root=True,
    root_filesystem_read_only=True,
    cap_drop_all=True,
    no_new_privileges=True,
    docker_socket_mounted=False,
    workspace_mount_target="/workspace",
    workspace_mount_read_only=False,
    maximum_mounts=1,
)


class Guardrail:
    """Apply action hard boundaries without policy or approval overrides."""

    def __init__(
        self,
        *,
        secret_detector: object | None,
        credentials_present: bool,
        workspace_id: str,
        policy_path: Path | str | None = None,
    ) -> None:
        if type(workspace_id) is not str or not workspace_id:
            raise ValueError("workspace_id must be a nonempty string")
        self._secret_detector = secret_detector
        self._credentials_present = credentials_present
        self._workspace_id = workspace_id
        if policy_path is None:
            source_mirror = Path(__file__).parents[2] / "sandbox" / "image-policy.json"
            self._image_policy = self._load_image_policy(
                source_mirror, allow_missing=True
            )
        else:
            self._image_policy = self._load_image_policy(
                Path(policy_path), allow_missing=False
            )

    def check(self, action: NormalizedAction) -> GuardrailResult:
        args = action.normalized_args
        if action.workspace_id != self._workspace_id:
            return self._deny(GuardrailReason.WORKSPACE_MISMATCH)
        if _RESERVED_SANDBOX_KEYS.intersection(args):
            return self._deny(GuardrailReason.RESERVED_SANDBOX_CONTROL)
        if not self._detector_configuration_is_usable():
            return self._deny(GuardrailReason.SECRET_DETECTOR_UNAVAILABLE)

        path_key = "path" if action.type in _FILE_ACTIONS else "cwd"
        if action.type in _FILE_ACTIONS | _PROCESS_ACTIONS:
            path = args.get(path_key)
            if not isinstance(path, str) or not self._is_canonical_relative_path(path):
                return self._deny(GuardrailReason.PATH_ESCAPE)
            if PurePosixPath(path).name == "docker.sock":
                return self._deny(GuardrailReason.DOCKER_SOCKET)

        if action.type in _PROCESS_ACTIONS:
            process_denial = self._check_process_payload(args)
            if process_denial is not None:
                return process_denial

        secret_denial = self._check_secret_payload(args)
        if secret_denial is not None:
            return secret_denial

        return GuardrailResult(GuardrailDecision.PASS, GuardrailReason.PASS)

    def assert_enforcement(self, sandbox_metadata: Mapping[str, Any]) -> None:
        """Fail with highest-priority security error on boundary drift."""

        policy = self._image_policy
        expected_keys = {
            "version",
            "cpu_limit",
            "memory_limit_bytes",
            "pids_limit",
            "timeout_seconds",
            "network_mode",
            "privileged",
            "run_as_non_root",
            "root_filesystem_read_only",
            "cap_drop_all",
            "no_new_privileges",
            "docker_socket_mounted",
            "mounts",
        }
        if set(sandbox_metadata) != expected_keys:
            raise BoundaryIntegrityError("sandbox metadata fields do not match v1")

        self._require_exact_type(sandbox_metadata, "version", int)
        self._require_exact_type(sandbox_metadata, "cpu_limit", float)
        for key in (
            "memory_limit_bytes",
            "pids_limit",
            "timeout_seconds",
        ):
            self._require_exact_type(sandbox_metadata, key, int)
        self._require_exact_type(sandbox_metadata, "network_mode", str)
        for key in (
            "privileged",
            "run_as_non_root",
            "root_filesystem_read_only",
            "cap_drop_all",
            "no_new_privileges",
            "docker_socket_mounted",
        ):
            self._require_exact_type(sandbox_metadata, key, bool)

        exact_values = {
            "version": policy.version,
            "network_mode": policy.network_mode,
            "privileged": policy.privileged,
            "run_as_non_root": policy.run_as_non_root,
            "root_filesystem_read_only": policy.root_filesystem_read_only,
            "cap_drop_all": policy.cap_drop_all,
            "no_new_privileges": policy.no_new_privileges,
            "docker_socket_mounted": policy.docker_socket_mounted,
        }
        if any(sandbox_metadata[key] != value for key, value in exact_values.items()):
            raise BoundaryIntegrityError("sandbox security setting mismatch")

        limits = {
            "cpu_limit": policy.cpu_limit,
            "memory_limit_bytes": policy.memory_limit_bytes,
            "pids_limit": policy.pids_limit,
            "timeout_seconds": policy.timeout_seconds,
        }
        if any(
            not math.isfinite(sandbox_metadata[key])
            or sandbox_metadata[key] <= 0
            or sandbox_metadata[key] > maximum
            for key, maximum in limits.items()
        ):
            raise BoundaryIntegrityError("sandbox resource limit exceeds hard maximum")
        self._assert_workspace_mount(sandbox_metadata["mounts"])

    def _check_process_payload(self, args: Mapping[str, Any]) -> GuardrailResult | None:
        argv = args.get("argv")
        timeout = args.get("timeout_seconds")
        stdin = args.get("stdin")
        env = args.get("env")
        if (
            not isinstance(argv, tuple | list)
            or not argv
            or any(not isinstance(item, str) for item in argv)
            or type(timeout) is not int
            or not isinstance(env, Mapping)
            or any(not isinstance(value, str) for value in env.values())
            or (stdin is not None and not isinstance(stdin, str | bytes))
        ):
            return self._deny(GuardrailReason.MALFORMED_ACTION)
        if timeout <= 0 or timeout > self._image_policy.timeout_seconds:
            return self._deny(GuardrailReason.RESOURCE_LIMIT)
        try:
            interpreter_stdin = stdin if isinstance(stdin, str) else None
            ToolRegistry._reject_interpreter_evaluation(list(argv), interpreter_stdin)
        except HarnessValidationError:
            return self._deny(GuardrailReason.INTERPRETER_EVAL)

        return None

    def _check_secret_payload(self, args: Mapping[str, Any]) -> GuardrailResult | None:
        detector = self._secret_detector
        assert isinstance(detector, SecretDetector)
        try:
            if any(
                detector.contains_secret(value)
                for value in self._iter_payload_values(args)
            ):
                return self._deny(GuardrailReason.SECRET_CONTAINMENT)
        except Exception:  # noqa: BLE001 - an injected detector must fail closed
            return self._deny(GuardrailReason.SECRET_DETECTOR_UNAVAILABLE)
        return None

    @classmethod
    def _iter_payload_values(cls, value: Any) -> Iterator[str | bytes]:
        if isinstance(value, str | bytes):
            yield value
        elif isinstance(value, Mapping):
            for item in value.values():
                yield from cls._iter_payload_values(item)
        elif isinstance(value, tuple | list):
            for item in value:
                yield from cls._iter_payload_values(item)

    def _detector_configuration_is_usable(self) -> bool:
        if not isinstance(self._secret_detector, SecretDetector):
            return False
        if not self._credentials_present:
            return isinstance(self._secret_detector, EmptySecretDetector)
        return not isinstance(self._secret_detector, EmptySecretDetector)

    def _assert_workspace_mount(self, mounts: Any) -> None:
        policy = self._image_policy
        if (
            not isinstance(mounts, Sequence)
            or isinstance(mounts, str | bytes)
            or len(mounts) != policy.maximum_mounts
        ):
            raise BoundaryIntegrityError("sandbox must have exactly one mount")
        mount = mounts[0]
        expected_keys = {
            "type",
            "source_workspace_id",
            "target",
            "read_only",
        }
        if not isinstance(mount, Mapping) or set(mount) != expected_keys:
            raise BoundaryIntegrityError("workspace mount fields do not match v1")
        string_fields = ("type", "source_workspace_id", "target")
        if any(type(mount[key]) is not str for key in string_fields):
            raise BoundaryIntegrityError("workspace mount string field has wrong type")
        if type(mount["read_only"]) is not bool:
            raise BoundaryIntegrityError("workspace mount read_only has wrong type")
        if (
            mount["type"] != "bind"
            or mount["source_workspace_id"] != self._workspace_id
            or mount["target"] != policy.workspace_mount_target
            or mount["read_only"] != policy.workspace_mount_read_only
        ):
            raise BoundaryIntegrityError("workspace mount violates hard boundary")

    @staticmethod
    def _require_exact_type(
        metadata: Mapping[str, Any], key: str, expected_type: type[object]
    ) -> None:
        if type(metadata[key]) is not expected_type:
            raise BoundaryIntegrityError(f"sandbox metadata {key} has wrong type")

    @staticmethod
    def _is_canonical_relative_path(value: str) -> bool:
        if not value or "\\" in value:
            return False
        path = PurePosixPath(value)
        return not path.is_absolute() and ".." not in path.parts

    @staticmethod
    def _deny(reason: GuardrailReason) -> GuardrailResult:
        return GuardrailResult(GuardrailDecision.DENY, reason)

    @staticmethod
    def _load_image_policy(path: Path, *, allow_missing: bool) -> _ImagePolicy:
        try:
            document = path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            if allow_missing:
                return _FROZEN_IMAGE_POLICY_V1
            raise BoundaryIntegrityError(
                "explicit image policy path does not exist"
            ) from error
        except OSError as error:
            raise BoundaryIntegrityError(
                "package image policy could not be read"
            ) from error

        try:
            raw = json.loads(document)
            expected = asdict(_FROZEN_IMAGE_POLICY_V1)
            if type(raw) is not dict or set(raw) != set(expected):
                raise BoundaryIntegrityError(
                    "package image policy schema does not match frozen v1"
                )
            if any(
                type(raw[key]) is not type(value) or raw[key] != value
                for key, value in expected.items()
            ):
                raise BoundaryIntegrityError(
                    "package image policy values do not match frozen v1"
                )
            return _FROZEN_IMAGE_POLICY_V1
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise BoundaryIntegrityError(
                "package image policy v1 is invalid"
            ) from error
