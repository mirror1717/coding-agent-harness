import math

import pytest

from coding_agent_harness.canonical import approval_fingerprint, canonical_json_bytes
from coding_agent_harness.domain import FrozenMapping, NormalizedAction
from coding_agent_harness.errors import HarnessValidationError


def test_canonical_json_uses_utf8_sorted_keys_and_compact_separators() -> None:
    value = {
        "message": "café",
        "b": ("x", 1),
        "a": FrozenMapping({"y": False, "x": None}),
    }

    assert canonical_json_bytes(value) == (
        b'{"a":{"x":null,"y":false},"b":["x",1],"message":"caf\xc3\xa9"}'
    )


@pytest.mark.parametrize("non_finite", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(non_finite: float) -> None:
    with pytest.raises(HarnessValidationError) as error:
        canonical_json_bytes({"value": non_finite})

    assert error.value.code == "VALIDATION_ERROR"


def test_canonical_json_treats_equivalent_mapping_orders_identically() -> None:
    first = FrozenMapping({"b": 2, "a": FrozenMapping({"z": 0, "y": 1})})
    second = FrozenMapping({"a": FrozenMapping({"y": 1, "z": 0}), "b": 2})

    assert canonical_json_bytes(first) == canonical_json_bytes(second)


@pytest.mark.parametrize("value", [{1: "value"}, FrozenMapping({"valid": {2: "x"}})])
def test_canonical_json_rejects_non_string_mapping_keys(value: object) -> None:
    with pytest.raises(HarnessValidationError) as error:
        canonical_json_bytes(value)

    assert error.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("value", [{"value": {"unsupported"}}, {"value": object()}])
def test_canonical_json_rejects_unsupported_values(value: object) -> None:
    with pytest.raises(HarnessValidationError) as error:
        canonical_json_bytes(value)

    assert error.value.code == "VALIDATION_ERROR"


def test_canonical_json_rejects_cyclic_values() -> None:
    value: list[object] = []
    value.append(value)

    with pytest.raises(HarnessValidationError) as error:
        canonical_json_bytes(value)

    assert error.value.code == "VALIDATION_ERROR"


def test_fingerprint_preserves_field_boundaries() -> None:
    first = NormalizedAction(
        type="write_file",
        normalized_args={"parts": ["ab", "c"]},
        workspace_id="workspace-alpha",
    )
    second = NormalizedAction(
        type="write_file",
        normalized_args={"parts": ["a", "bc"]},
        workspace_id="workspace-alpha",
    )

    assert approval_fingerprint(first) != approval_fingerprint(second)


def test_fingerprint_hashes_only_the_versioned_approval_fields() -> None:
    action = NormalizedAction(
        action_id="action-ignored",
        type="write_file",
        normalized_args={"path": "src/a.py", "mode": "overwrite"},
        workspace_id="workspace-alpha",
        raw_args={"this": "is ignored"},
        round=9,
    )

    assert approval_fingerprint(action) == (
        "394fe78e2a4bb5008d54887f277b150e4b234489aac1f2f1b1cf7eef3ac7460b"
    )


def test_fingerprint_uses_normalized_action_type() -> None:
    action = NormalizedAction(type="write_file", normalized_args={}, workspace_id="ws")

    assert approval_fingerprint(action) == (
        "7935b012bc8326133eda873c71183c00e63ccfe7345701499ed4d58061e8c9bb"
    )


@pytest.mark.parametrize("version", [2, 1.0, True])
def test_fingerprint_rejects_unsupported_versions(version: object) -> None:
    action = NormalizedAction(type="write_file", normalized_args={}, workspace_id="ws")

    with pytest.raises(HarnessValidationError) as error:
        approval_fingerprint(action, version=version)  # type: ignore[arg-type]

    assert error.value.code == "VALIDATION_ERROR"
