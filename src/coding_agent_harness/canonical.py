"""Stable JSON canonicalization and approval fingerprints."""

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from coding_agent_harness.domain import NormalizedAction
from coding_agent_harness.errors import HarnessValidationError


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a supported value as deterministic UTF-8 JSON bytes."""

    try:
        serialized = json.dumps(
            _canonical_value(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return serialized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise HarnessValidationError("canonical JSON requires valid UTF-8 strings") from error


def approval_fingerprint(action: NormalizedAction, version: int = 1) -> str:
    """Return the version-1 approval digest for a normalized action."""

    if type(version) is not int or version != 1:
        raise HarnessValidationError("unsupported approval fingerprint version")

    payload = {
        "version": version,
        "action_type": action.type,
        "normalized_args": action.normalized_args,
        "workspace_id": action.workspace_id,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _canonical_value(value: object, active_container_ids: frozenset[int] = frozenset()) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HarnessValidationError("canonical JSON does not support non-finite numbers")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise HarnessValidationError("canonical JSON requires string object keys")
        nested_container_ids = _nested_container_ids(value, active_container_ids)
        return {
            key: _canonical_value(item, nested_container_ids)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        nested_container_ids = _nested_container_ids(value, active_container_ids)
        return [
            _canonical_value(item, nested_container_ids)
            for item in value
        ]
    raise HarnessValidationError("unsupported canonical JSON value")


def _nested_container_ids(
    value: object, active_container_ids: frozenset[int]
) -> frozenset[int]:
    value_id = id(value)
    if value_id in active_container_ids:
        raise HarnessValidationError("canonical JSON does not support cyclic values")
    return active_container_ids | {value_id}
