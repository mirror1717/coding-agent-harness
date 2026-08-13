"""Canonical JSON serialization and versioned approval fingerprint.

Implements deterministic JSON encoding for audit and approval integrity.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize value to canonical JSON bytes.

    - UTF-8 encoding
    - Sorted keys
    - Compact separators (no spaces)
    - Rejects NaN and Infinity

    Args:
        value: JSON-serializable value

    Returns:
        Canonical JSON as UTF-8 bytes

    Raises:
        ValueError: If value contains NaN or Infinity
    """
    # Check for NaN/Infinity before serialization
    _check_finite(value)

    # Serialize with sorted keys and compact separators
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _check_finite(value: Any) -> None:
    """Recursively check that value contains no NaN or Infinity."""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"Canonical JSON does not support {value}")
    elif isinstance(value, dict):
        for v in value.values():
            _check_finite(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_finite(item)


def approval_fingerprint(action: dict[str, Any], version: int = 1) -> str:
    """Compute versioned approval fingerprint as SHA-256 hex digest.

    Fingerprint is computed over a canonical JSON object containing:
    - version: fingerprint schema version
    - action_type: type of action being approved
    - normalized_args: normalized action parameters
    - workspace_id: workspace identifier

    Args:
        action: Dict with action_type, normalized_args, workspace_id
        version: Fingerprint schema version (only 1 supported)

    Returns:
        SHA-256 hex digest (64 characters)

    Raises:
        ValueError: If version is not supported
    """
    # Only version 1 is supported in first version
    if version != 1:
        raise ValueError(f"Unsupported fingerprint version: {version}")

    # Build canonical fingerprint object
    fingerprint_obj = {
        "version": version,
        "action_type": action["action_type"],
        "normalized_args": action["normalized_args"],
        "workspace_id": action["workspace_id"],
    }

    # Serialize to canonical JSON and compute SHA-256
    canonical_bytes = canonical_json_bytes(fingerprint_obj)
    return hashlib.sha256(canonical_bytes).hexdigest()
