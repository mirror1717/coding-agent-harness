"""Tests for the declarative YAML PolicyEngine."""

import random
from pathlib import Path

import pytest
import yaml

from coding_agent_harness.domain import ActionSource, NormalizedAction
from coding_agent_harness.errors import HarnessConfigurationError
from coding_agent_harness.policy import (
    PolicyDecision,
    PolicyEngine,
    PolicyResult,
    RiskLevel,
)

_WORKSPACE = "workspace-1"


def _shell_args(argv: list[str] | None = None, **overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "argv": argv or ["pytest", "-q"],
        "cwd": ".",
        "env": {},
        "timeout_seconds": 30,
        "stdin": None,
    }
    args.update(overrides)
    return args


def _action(
    action_type: str = "shell",
    *,
    source: ActionSource = ActionSource.MODEL,
    normalized_args: dict[str, object] | None = None,
    raw_args: dict[str, object] | None = None,
) -> NormalizedAction:
    if normalized_args is None:
        normalized_args = _shell_args()
    return NormalizedAction(
        action_id="action-1",
        source=source,
        type=action_type,
        raw_args=raw_args or {},
        normalized_args=normalized_args,
        workspace_id=_WORKSPACE,
    )


def _policy_text(
    rules: list[dict[str, object]],
    *,
    defaults: dict[str, object] | None = None,
    version: int = 1,
) -> str:
    document: dict[str, object] = {
        "version": version,
        "defaults": defaults or {"decision": "REQUIRE_APPROVAL", "risk": "medium"},
        "rules": rules,
    }
    return yaml.safe_dump(document, sort_keys=False)


# --- Schema / loading: fail closed ---


def test_invalid_yaml_fails_closed() -> None:
    with pytest.raises(HarnessConfigurationError, match="invalid YAML"):
        PolicyEngine.from_yaml("version: 1\n  bad: [unbalanced")


@pytest.mark.parametrize(
    "raw_text",
    [
        pytest.param("just a string", id="scalar"),
        pytest.param("- item\n- item2", id="list"),
        pytest.param("", id="empty"),
    ],
)
def test_non_mapping_document_fails_closed(raw_text: str) -> None:
    with pytest.raises(HarnessConfigurationError):
        PolicyEngine.from_yaml(raw_text)


def test_unknown_top_level_field_fails_closed() -> None:
    document = {
        "version": 1,
        "defaults": {"decision": "REQUIRE_APPROVAL", "risk": "medium"},
        "rules": [],
        "unexpected": True,
    }
    with pytest.raises(HarnessConfigurationError):
        PolicyEngine.from_yaml(yaml.safe_dump(document))


def test_unknown_rule_field_fails_closed() -> None:
    text = _policy_text(
        [
            {
                "id": "r1",
                "action_types": ["read_file"],
                "risk": "low",
                "decision": "ALLOW",
                "reason": "ok",
                "bogus": True,
            }
        ]
    )
    with pytest.raises(HarnessConfigurationError):
        PolicyEngine.from_yaml(text)


def test_unknown_match_field_fails_closed() -> None:
    text = _policy_text(
        [
            {
                "id": "r1",
                "action_types": ["read_file"],
                "match": {"network": "none"},
                "risk": "low",
                "decision": "ALLOW",
                "reason": "ok",
            }
        ]
    )
    with pytest.raises(HarnessConfigurationError):
        PolicyEngine.from_yaml(text)


def test_missing_required_defaults_fails_closed() -> None:
    document = {"version": 1, "rules": []}
    with pytest.raises(HarnessConfigurationError):
        PolicyEngine.from_yaml(yaml.safe_dump(document))


def test_duplicate_rule_ids_fail_closed() -> None:
    text = _policy_text(
        [
            {
                "id": "dup",
                "action_types": ["read_file"],
                "risk": "low",
                "decision": "ALLOW",
                "reason": "first",
            },
            {
                "id": "dup",
                "action_types": ["write_file"],
                "risk": "medium",
                "decision": "REQUIRE_APPROVAL",
                "reason": "second",
            },
        ]
    )
    with pytest.raises(HarnessConfigurationError, match="duplicate"):
        PolicyEngine.from_yaml(text)


def test_unsupported_version_fails_closed() -> None:
    text = _policy_text([], version=2)
    with pytest.raises(HarnessConfigurationError, match="version"):
        PolicyEngine.from_yaml(text)


def test_default_decision_allow_fails_closed() -> None:
    text = _policy_text(
        [],
        defaults={"decision": "ALLOW", "risk": "low"},
    )
    with pytest.raises(HarnessConfigurationError, match="default"):
        PolicyEngine.from_yaml(text)


def test_empty_action_types_rejected() -> None:
    text = _policy_text(
        [
            {
                "id": "r1",
                "action_types": [],
                "risk": "low",
                "decision": "ALLOW",
                "reason": "ok",
            }
        ]
    )
    with pytest.raises(HarnessConfigurationError):
        PolicyEngine.from_yaml(text)


# --- Default policy file ---


def test_default_policy_file_exists_and_is_valid() -> None:
    path = Path(__file__).parents[2] / "config" / "default-policy.yaml"
    engine = PolicyEngine.from_file(path)
    assert engine.version == 1


def test_default_engine_loads() -> None:
    engine = PolicyEngine.default()
    assert engine.version == 1


# --- Default policy: safe defaults ---


@pytest.mark.parametrize("action_type", ["read_file", "list_files"])
def test_safe_reads_are_allowed(action_type: str) -> None:
    engine = PolicyEngine.default()
    result = engine.evaluate(_action(action_type, normalized_args={"path": "src/app.py"}))
    assert result.decision is PolicyDecision.ALLOW
    assert result.risk is RiskLevel.LOW
    assert result.rule_id == "allow-safe-reads"


def test_ordinary_write_requires_approval() -> None:
    engine = PolicyEngine.default()
    result = engine.evaluate(_action("write_file", normalized_args={"path": "src/app.py", "content": "x"}))
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL
    assert result.risk is RiskLevel.MEDIUM


def test_ordinary_shell_requires_approval() -> None:
    engine = PolicyEngine.default()
    result = engine.evaluate(_action("shell", normalized_args=_shell_args(["pytest", "-q"])))
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL


def test_ordinary_pytest_requires_approval() -> None:
    engine = PolicyEngine.default()
    result = engine.evaluate(
        _action("pytest", source=ActionSource.MODEL, normalized_args=_shell_args(["pytest", "-q"]))
    )
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL


def test_required_verification_pytest_is_allowed() -> None:
    engine = PolicyEngine.default()
    result = engine.evaluate(
        _action(
            "pytest",
            source=ActionSource.VERIFICATION,
            normalized_args=_shell_args(["pytest", "-q"]),
        )
    )
    assert result.decision is PolicyDecision.ALLOW
    assert result.rule_id == "allow-required-verification-pytest"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(".env", id="dotenv"),
        pytest.param("config/credentials", id="credentials"),
        pytest.param("secrets/id_rsa", id="id-rsa"),
        pytest.param("deploy/key.pem", id="pem-key"),
    ],
)
def test_secret_path_write_is_denied(path: str) -> None:
    engine = PolicyEngine.default()
    result = engine.evaluate(
        _action("write_file", normalized_args={"path": path, "content": "x"})
    )
    assert result.decision is PolicyDecision.DENY
    assert result.rule_id == "deny-secret-path-writes"


@pytest.mark.parametrize("argv", [["rm", "-rf", "build"], ["rmdir", "dist"], ["shred", "file"]])
def test_destructive_shell_is_denied(argv: list[str]) -> None:
    engine = PolicyEngine.default()
    result = engine.evaluate(_action("shell", normalized_args=_shell_args(argv)))
    assert result.decision is PolicyDecision.DENY
    assert result.rule_id == "deny-destructive-shell"


# --- No match ---


def test_no_match_uses_safe_default() -> None:
    text = _policy_text(
        [
            {
                "id": "allow-read",
                "action_types": ["read_file"],
                "risk": "low",
                "decision": "ALLOW",
                "reason": "safe read",
            }
        ]
    )
    engine = PolicyEngine.from_yaml(text)
    result = engine.evaluate(_action("write_file", normalized_args={"path": "src/app.py", "content": "x"}))
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL
    assert result.risk is RiskLevel.MEDIUM
    assert result.rule_id == "default"
    assert result.version == 1


# --- Specificity and conflict resolution ---


def test_higher_specificity_wins() -> None:
    text = _policy_text(
        [
            {
                "id": "allow-write",
                "action_types": ["write_file"],
                "risk": "low",
                "decision": "ALLOW",
                "reason": "allow",
            },
            {
                "id": "deny-env-write",
                "action_types": ["write_file"],
                "match": {"path_patterns": [".env"]},
                "risk": "high",
                "decision": "DENY",
                "reason": "deny secret",
            },
        ]
    )
    engine = PolicyEngine.from_yaml(text)
    normal = engine.evaluate(_action("write_file", normalized_args={"path": "src/app.py", "content": "x"}))
    assert normal.decision is PolicyDecision.ALLOW
    secret = engine.evaluate(_action("write_file", normalized_args={"path": ".env", "content": "x"}))
    assert secret.decision is PolicyDecision.DENY


def test_higher_specificity_allow_beats_lower_specificity_deny() -> None:
    text = _policy_text(
        [
            {
                "id": "deny-all-writes",
                "action_types": ["write_file"],
                "risk": "high",
                "decision": "DENY",
                "reason": "deny all",
            },
            {
                "id": "allow-src-writes",
                "action_types": ["write_file"],
                "match": {"path_patterns": ["src/*"]},
                "risk": "low",
                "decision": "ALLOW",
                "reason": "allow src",
            },
        ]
    )
    engine = PolicyEngine.from_yaml(text)
    inside = engine.evaluate(_action("write_file", normalized_args={"path": "src/app.py", "content": "x"}))
    assert inside.decision is PolicyDecision.ALLOW
    outside = engine.evaluate(_action("write_file", normalized_args={"path": "build/out.txt", "content": "x"}))
    assert outside.decision is PolicyDecision.DENY


def test_same_specificity_deny_beats_allow() -> None:
    text = _policy_text(
        [
            {
                "id": "allow-write",
                "action_types": ["write_file"],
                "risk": "low",
                "decision": "ALLOW",
                "reason": "allow",
            },
            {
                "id": "deny-write",
                "action_types": ["write_file"],
                "risk": "high",
                "decision": "DENY",
                "reason": "deny",
            },
        ]
    )
    engine = PolicyEngine.from_yaml(text)
    result = engine.evaluate(_action("write_file", normalized_args={"path": "src/app.py", "content": "x"}))
    assert result.decision is PolicyDecision.DENY


def test_same_specificity_require_approval_beats_allow() -> None:
    text = _policy_text(
        [
            {
                "id": "allow-write",
                "action_types": ["write_file"],
                "risk": "low",
                "decision": "ALLOW",
                "reason": "allow",
            },
            {
                "id": "review-write",
                "action_types": ["write_file"],
                "risk": "medium",
                "decision": "REQUIRE_APPROVAL",
                "reason": "review",
            },
        ]
    )
    engine = PolicyEngine.from_yaml(text)
    result = engine.evaluate(_action("write_file", normalized_args={"path": "src/app.py", "content": "x"}))
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL


# --- within_workspace criterion (SPEC example) ---


def test_spec_example_policy_with_within_workspace_loads() -> None:
    text = yaml.safe_dump(
        {
            "version": 1,
            "defaults": {"decision": "REQUIRE_APPROVAL", "risk": "medium"},
            "rules": [
                {
                    "id": "allow-project-read",
                    "action_types": ["read_file", "list_files"],
                    "match": {"within_workspace": True},
                    "risk": "low",
                    "decision": "ALLOW",
                    "reason": "Read-only access inside the selected workspace",
                }
            ],
        },
        sort_keys=False,
    )
    engine = PolicyEngine.from_yaml(text)
    result = engine.evaluate(_action("read_file", normalized_args={"path": "src/app.py"}))
    assert result.decision is PolicyDecision.ALLOW
    assert result.rule_id == "allow-project-read"


def test_within_workspace_false_never_matches() -> None:
    text = _policy_text(
        [
            {
                "id": "deny-outside",
                "action_types": ["read_file"],
                "match": {"within_workspace": False},
                "risk": "high",
                "decision": "DENY",
                "reason": "outside",
            }
        ]
    )
    engine = PolicyEngine.from_yaml(text)
    result = engine.evaluate(_action("read_file", normalized_args={"path": "src/app.py"}))
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL


# --- Result shape ---


def test_result_contains_all_fields() -> None:
    engine = PolicyEngine.default()
    result = engine.evaluate(_action("read_file", normalized_args={"path": "src/app.py"}))
    assert isinstance(result, PolicyResult)
    assert result.decision is PolicyDecision.ALLOW
    assert result.risk is RiskLevel.LOW
    assert result.rule_id == "allow-safe-reads"
    assert result.reason
    assert result.version == 1


# --- Determinism ---


def test_shuffled_rules_produce_deterministic_decisions() -> None:
    path = Path(__file__).parents[2] / "config" / "default-policy.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    original_rules = list(raw["rules"])

    actions = [
        _action("read_file", normalized_args={"path": "src/app.py"}),
        _action("list_files", normalized_args={"path": "."}),
        _action("write_file", normalized_args={"path": "src/app.py", "content": "x"}),
        _action("write_file", normalized_args={"path": ".env", "content": "x"}),
        _action("shell", normalized_args=_shell_args(["pytest", "-q"])),
        _action("shell", normalized_args=_shell_args(["rm", "-rf", "build"])),
        _action("pytest", source=ActionSource.VERIFICATION, normalized_args=_shell_args(["pytest", "-q"])),
        _action("pytest", source=ActionSource.MODEL, normalized_args=_shell_args(["pytest", "-q"])),
    ]

    reference = PolicyEngine.default()
    for seed in range(8):
        shuffled_rules = list(original_rules)
        random.Random(seed).shuffle(shuffled_rules)
        shuffled = dict(raw)
        shuffled["rules"] = shuffled_rules
        engine = PolicyEngine.from_yaml(yaml.safe_dump(shuffled, sort_keys=False))
        for action in actions:
            got = engine.evaluate(action)
            expected = reference.evaluate(action)
            assert got.decision is expected.decision
            assert got.risk is expected.risk


# --- Match only on normalized args ---


def test_match_uses_normalized_args_not_raw_args() -> None:
    engine = PolicyEngine.default()
    action = NormalizedAction(
        action_id="action-1",
        source=ActionSource.MODEL,
        type="write_file",
        raw_args={"path": "safe.txt"},
        normalized_args={"path": ".env", "content": "x"},
        workspace_id=_WORKSPACE,
    )
    result = engine.evaluate(action)
    assert result.decision is PolicyDecision.DENY
    assert result.rule_id == "deny-secret-path-writes"
