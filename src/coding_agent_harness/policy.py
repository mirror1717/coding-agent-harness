"""Declarative YAML policy engine with strict schema and deterministic rules."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Self

import yaml  # type: ignore[import-untyped]
from pydantic import Field, ValidationError, model_validator

from .domain import ActionSource, FactModel, NormalizedAction
from .errors import HarnessConfigurationError

_SUPPORTED_VERSIONS = frozenset({1})


class PolicyDecision(str, Enum):
    """Outcome of evaluating an action against the policy."""

    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class RiskLevel(str, Enum):
    """Explainable risk severity attached to a policy decision."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_DECISION_STRICTNESS: dict[PolicyDecision, int] = {
    PolicyDecision.ALLOW: 0,
    PolicyDecision.REQUIRE_APPROVAL: 1,
    PolicyDecision.DENY: 2,
}

_RISK_STRICTNESS: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class PolicyMatch(FactModel):
    """Optional criteria that narrow which actions a rule applies to."""

    source: ActionSource | None = None
    path_patterns: tuple[str, ...] = ()
    argv_patterns: tuple[str, ...] = ()
    within_workspace: bool | None = None


class PolicyDefaults(FactModel):
    """Safe fallback used when no rule matches a normalized action."""

    decision: PolicyDecision
    risk: RiskLevel


class PolicyRule(FactModel):
    """A single declarative rule with match criteria and a decision."""

    id: str = Field(min_length=1)
    action_types: tuple[str, ...] = Field(min_length=1)
    match: PolicyMatch = Field(default_factory=PolicyMatch)
    risk: RiskLevel
    decision: PolicyDecision
    reason: str = Field(min_length=1)


class PolicyDocument(FactModel):
    """The complete, strictly-validated policy document."""

    version: int
    defaults: PolicyDefaults
    rules: tuple[PolicyRule, ...] = ()

    @model_validator(mode="after")
    def _validate_document(self) -> Self:
        if self.version not in _SUPPORTED_VERSIONS:
            raise ValueError(f"unsupported policy version: {self.version}")
        if self.defaults.decision is PolicyDecision.ALLOW:
            raise ValueError("default decision must not be ALLOW")
        counts = Counter(rule.id for rule in self.rules)
        duplicates = sorted({rid for rid, count in counts.items() if count > 1})
        if duplicates:
            raise ValueError(f"duplicate rule IDs: {duplicates}")
        return self


@dataclass(frozen=True)
class PolicyResult:
    """Immutable, explainable outcome of a policy evaluation."""

    decision: PolicyDecision
    risk: RiskLevel
    rule_id: str
    reason: str
    version: int


class PolicyEngine:
    """Evaluate normalized actions against a strict YAML policy document."""

    def __init__(self, document: PolicyDocument) -> None:
        self._document = document

    @property
    def version(self) -> int:
        return self._document.version

    @classmethod
    def from_yaml(cls, text: str) -> PolicyEngine:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise HarnessConfigurationError(f"invalid YAML: {error}") from error
        if not isinstance(raw, dict):
            raise HarnessConfigurationError("policy document must be a mapping")
        try:
            document = PolicyDocument.model_validate(raw)
        except ValidationError as error:
            raise HarnessConfigurationError(
                f"invalid policy schema: {error}"
            ) from error
        return cls(document)

    @classmethod
    def from_file(cls, path: Path | str) -> PolicyEngine:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as error:
            raise HarnessConfigurationError(
                f"policy file could not be read: {error}"
            ) from error
        return cls.from_yaml(text)

    @classmethod
    def default(cls) -> PolicyEngine:
        path = Path(__file__).parents[2] / "config" / "default-policy.yaml"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = _DEFAULT_POLICY_YAML
        except OSError as error:
            raise HarnessConfigurationError(
                f"default policy file could not be read: {error}"
            ) from error
        return cls.from_yaml(text)

    def evaluate(self, action: NormalizedAction) -> PolicyResult:
        matches: list[tuple[int, PolicyRule]] = []
        for rule in self._document.rules:
            if self._rule_matches(rule, action):
                matches.append((self._specificity(rule), rule))
        if not matches:
            defaults = self._document.defaults
            return PolicyResult(
                decision=defaults.decision,
                risk=defaults.risk,
                rule_id="default",
                reason="no policy rule matched; using safe default",
                version=self._document.version,
            )
        matches.sort(
            key=lambda item: (
                -item[0],
                -_DECISION_STRICTNESS[item[1].decision],
                -_RISK_STRICTNESS[item[1].risk],
                item[1].id,
            )
        )
        rule = matches[0][1]
        return PolicyResult(
            decision=rule.decision,
            risk=rule.risk,
            rule_id=rule.id,
            reason=rule.reason,
            version=self._document.version,
        )

    def _rule_matches(self, rule: PolicyRule, action: NormalizedAction) -> bool:
        if action.type not in rule.action_types:
            return False
        match = rule.match
        if match.source is not None and action.source != match.source:
            return False
        if match.within_workspace is False:
            return False
        if match.path_patterns:
            path = action.normalized_args.get("path")
            if not isinstance(path, str):
                return False
            basename = PurePosixPath(path).name
            if not any(
                fnmatchcase(path, pattern) or fnmatchcase(basename, pattern)
                for pattern in match.path_patterns
            ):
                return False
        if match.argv_patterns:
            argv = action.normalized_args.get("argv")
            if (
                not isinstance(argv, tuple | list)
                or not argv
                or not isinstance(argv[0], str)
            ):
                return False
            program = PurePosixPath(argv[0]).name
            if not any(
                fnmatchcase(program, pattern)
                for pattern in match.argv_patterns
            ):
                return False
        return True

    @staticmethod
    def _specificity(rule: PolicyRule) -> int:
        match = rule.match
        score = 0
        if match.source is not None:
            score += 1
        if match.path_patterns:
            score += 1
        if match.argv_patterns:
            score += 1
        if match.within_workspace is not None:
            score += 1
        return score


_DEFAULT_POLICY_YAML = """\
version: 1
defaults:
  decision: REQUIRE_APPROVAL
  risk: medium
rules:
  - id: allow-safe-reads
    action_types: [read_file, list_files]
    risk: low
    decision: ALLOW
    reason: Read-only access inside the workspace
  - id: allow-required-verification-pytest
    action_types: [pytest]
    match:
      source: VERIFICATION
    risk: low
    decision: ALLOW
    reason: Required verification pytest runs without approval
  - id: require-approval-ordinary-write
    action_types: [write_file]
    risk: medium
    decision: REQUIRE_APPROVAL
    reason: Ordinary workspace writes require human approval
  - id: require-approval-ordinary-shell
    action_types: [shell]
    risk: medium
    decision: REQUIRE_APPROVAL
    reason: Ordinary shell commands require human approval
  - id: require-approval-ordinary-pytest
    action_types: [pytest]
    risk: medium
    decision: REQUIRE_APPROVAL
    reason: Non-verification pytest runs require human approval
  - id: deny-secret-path-writes
    action_types: [write_file]
    match:
      path_patterns:
        - .env
        - "*.pem"
        - "*.key"
        - id_rsa
        - id_ed25519
        - credentials
        - secrets.json
        - .netrc
        - .npmrc
        - .pypirc
    risk: high
    decision: DENY
    reason: Writes to secret-like paths are denied
  - id: deny-destructive-shell
    action_types: [shell]
    match:
      argv_patterns:
        - rm
        - rmdir
        - shred
        - mkfs
        - dd
    risk: high
    decision: DENY
    reason: Destructive shell commands are denied
"""
