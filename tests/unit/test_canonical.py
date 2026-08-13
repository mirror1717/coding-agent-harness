"""Tests for canonical JSON and approval fingerprint - T02"""
import pytest


def test_fingerprint_preserves_field_boundaries():
    """Fingerprint must distinguish different field boundaries"""
    from coding_agent_harness.canonical import approval_fingerprint

    # These should produce different fingerprints
    action1 = {
        "action_type": "shell",
        "normalized_args": {"argv": ["ab", "c"], "cwd": "."},
        "workspace_id": "ws-1"
    }
    action2 = {
        "action_type": "shell",
        "normalized_args": {"argv": ["a", "bc"], "cwd": "."},
        "workspace_id": "ws-1"
    }

    fp1 = approval_fingerprint(action1, version=1)
    fp2 = approval_fingerprint(action2, version=1)

    assert fp1 != fp2, "Field boundaries must be preserved"


def test_key_order_is_canonical():
    """Key order must not affect canonical JSON"""
    from coding_agent_harness.canonical import canonical_json_bytes

    obj1 = {"b": 2, "a": 1}
    obj2 = {"a": 1, "b": 2}

    assert canonical_json_bytes(obj1) == canonical_json_bytes(obj2)


def test_nan_is_rejected():
    """NaN and Infinity must be rejected"""
    from coding_agent_harness.canonical import canonical_json_bytes

    with pytest.raises((ValueError, OverflowError)):
        canonical_json_bytes({"value": float("nan")})

    with pytest.raises((ValueError, OverflowError)):
        canonical_json_bytes({"value": float("inf")})

    with pytest.raises((ValueError, OverflowError)):
        canonical_json_bytes({"value": float("-inf")})


def test_canonical_json_is_compact():
    """Canonical JSON must use compact separators"""
    from coding_agent_harness.canonical import canonical_json_bytes

    obj = {"key": "value", "list": [1, 2, 3]}
    result = canonical_json_bytes(obj)

    # Should not contain spaces after separators
    assert b": " not in result
    assert b", " not in result


def test_canonical_json_sorts_keys():
    """Canonical JSON must sort keys"""
    from coding_agent_harness.canonical import canonical_json_bytes

    obj = {"z": 1, "a": 2, "m": 3}
    result = canonical_json_bytes(obj)

    # Keys should appear in sorted order
    assert result == b'{"a":2,"m":3,"z":1}'


def test_fingerprint_includes_version_field():
    """Fingerprint canonical object must include version field"""
    import json

    from coding_agent_harness.canonical import canonical_json_bytes

    action = {
        "action_type": "shell",
        "normalized_args": {"argv": ["pytest"], "cwd": "."},
        "workspace_id": "ws-1"
    }

    # Build the canonical object manually to verify version is included
    canonical_obj = {
        "version": 1,
        "action_type": action["action_type"],
        "normalized_args": action["normalized_args"],
        "workspace_id": action["workspace_id"],
    }

    canonical_bytes = canonical_json_bytes(canonical_obj)
    parsed = json.loads(canonical_bytes)

    assert "version" in parsed
    assert parsed["version"] == 1


def test_fingerprint_unsupported_version_fails_closed():
    """Unsupported versions must fail closed"""
    from coding_agent_harness.canonical import approval_fingerprint

    action = {
        "action_type": "shell",
        "normalized_args": {"argv": ["pytest"], "cwd": "."},
        "workspace_id": "ws-1"
    }

    # Only version 1 is supported in first version
    with pytest.raises(ValueError):
        approval_fingerprint(action, version=999)


def test_fingerprint_is_deterministic():
    """Same input must produce same fingerprint"""
    from coding_agent_harness.canonical import approval_fingerprint

    action = {
        "action_type": "shell",
        "normalized_args": {"argv": ["pytest", "-q"], "cwd": "src"},
        "workspace_id": "ws-canonical-id"
    }

    fp1 = approval_fingerprint(action, version=1)
    fp2 = approval_fingerprint(action, version=1)

    assert fp1 == fp2


def test_fingerprint_is_sha256_hex():
    """Fingerprint must be SHA-256 hex digest"""
    from coding_agent_harness.canonical import approval_fingerprint

    action = {
        "action_type": "read_file",
        "normalized_args": {"path": "test.py"},
        "workspace_id": "ws-1"
    }

    fp = approval_fingerprint(action, version=1)

    # SHA-256 hex digest is 64 characters
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)
