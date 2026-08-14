import json
from pathlib import Path

import pytest

import coding_agent_harness.guardrail as guardrail_module
from coding_agent_harness.domain import NormalizedAction, RawExecutionResult
from coding_agent_harness.guardrail import (
    BoundaryIntegrityError,
    EmptySecretDetector,
    Guardrail,
    GuardrailDecision,
    GuardrailReason,
    SecretDetector,
)


class FakeSecretDetector:
    def __init__(self, *secrets: str) -> None:
        self._secrets = secrets

    def contains_secret(self, value: str | bytes) -> bool:
        candidate = value.decode() if isinstance(value, bytes) else value
        return any(secret in candidate for secret in self._secrets)


def action(
    action_type: str = "shell", normalized_args: dict[str, object] | None = None
) -> NormalizedAction:
    return NormalizedAction(
        action_id="action-1",
        type=action_type,
        raw_args={},
        normalized_args=normalized_args
        or {
            "argv": ["pytest", "-q"],
            "cwd": ".",
            "env": {},
            "timeout_seconds": 30,
            "stdin": None,
        },
        workspace_id="workspace-1",
    )


def enforcement_metadata(**overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "version": 1,
        "cpu_limit": 1.0,
        "memory_limit_bytes": 1_073_741_824,
        "pids_limit": 128,
        "timeout_seconds": 300,
        "network_mode": "none",
        "privileged": False,
        "run_as_non_root": True,
        "root_filesystem_read_only": True,
        "cap_drop_all": True,
        "no_new_privileges": True,
        "docker_socket_mounted": False,
        "mounts": [
            {
                "type": "bind",
                "source_workspace_id": "workspace-1",
                "target": "/workspace",
                "read_only": False,
            }
        ],
    }
    metadata.update(overrides)
    return metadata


def guardrail() -> Guardrail:
    return Guardrail(
        secret_detector=EmptySecretDetector(),
        credentials_present=False,
        workspace_id="workspace-1",
    )


def test_workspace_identity_is_required_and_nonempty() -> None:
    with pytest.raises(ValueError, match="workspace_id"):
        Guardrail(
            secret_detector=EmptySecretDetector(),
            credentials_present=False,
            workspace_id="",
        )


def test_action_for_another_workspace_is_denied() -> None:
    result = Guardrail(
        secret_detector=EmptySecretDetector(),
        credentials_present=False,
        workspace_id="workspace-expected",
    ).check(action())

    assert result.decision is GuardrailDecision.DENY
    assert result.reason is GuardrailReason.WORKSPACE_MISMATCH


def test_enforcement_mount_must_bind_the_expected_workspace() -> None:
    governed = Guardrail(
        secret_detector=EmptySecretDetector(),
        credentials_present=False,
        workspace_id="workspace-expected",
    )

    with pytest.raises(BoundaryIntegrityError, match="workspace"):
        governed.assert_enforcement(enforcement_metadata())


def test_secret_detector_accepts_a_structural_fake() -> None:
    assert isinstance(FakeSecretDetector("secret"), SecretDetector)


def test_package_image_policy_is_fixed_v1() -> None:
    path = Path(__file__).parents[2] / "sandbox" / "image-policy.json"

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "cpu_limit": 1.0,
        "memory_limit_bytes": 1_073_741_824,
        "pids_limit": 128,
        "timeout_seconds": 300,
        "network_mode": "none",
        "privileged": False,
        "run_as_non_root": True,
        "root_filesystem_read_only": True,
        "cap_drop_all": True,
        "no_new_privileges": True,
        "docker_socket_mounted": False,
        "workspace_mount_target": "/workspace",
        "workspace_mount_read_only": False,
        "maximum_mounts": 1,
    }


def test_missing_default_source_mirror_uses_frozen_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guardrail_module,
        "__file__",
        "/absent/site-packages/coding_agent_harness/guardrail.py",
    )
    governed = Guardrail(
        secret_detector=EmptySecretDetector(),
        credentials_present=False,
        workspace_id="workspace-1",
    )

    with pytest.raises(BoundaryIntegrityError):
        governed.assert_enforcement(enforcement_metadata(cpu_limit=1.1))


def test_missing_explicit_policy_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(BoundaryIntegrityError):
        Guardrail(
            secret_detector=EmptySecretDetector(),
            credentials_present=False,
            workspace_id="workspace-1",
            policy_path=tmp_path / "missing-policy.json",
        )


def test_existing_default_source_mirror_is_strictly_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_file = tmp_path / "src" / "coding_agent_harness" / "guardrail.py"
    mirror = tmp_path / "sandbox" / "image-policy.json"
    mirror.parent.mkdir()
    policy = json.loads(
        (Path(__file__).parents[2] / "sandbox" / "image-policy.json").read_text(
            encoding="utf-8"
        )
    )
    policy["network_mode"] = "bridge"
    mirror.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(guardrail_module, "__file__", str(source_file))

    with pytest.raises(BoundaryIntegrityError):
        guardrail()


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        pytest.param("version", 2, id="version"),
        pytest.param("version", "1", id="version-wrong-type"),
        pytest.param("cpu_limit", 1.1, id="cpu-loosened"),
        pytest.param("cpu_limit", 1, id="cpu-wrong-type"),
        pytest.param("memory_limit_bytes", 1_073_741_825, id="memory-loosened"),
        pytest.param("memory_limit_bytes", True, id="memory-wrong-type"),
        pytest.param("pids_limit", 129, id="pids-loosened"),
        pytest.param("pids_limit", 128.0, id="pids-wrong-type"),
        pytest.param("timeout_seconds", 301, id="timeout-loosened"),
        pytest.param("timeout_seconds", "300", id="timeout-wrong-type"),
        pytest.param("network_mode", "bridge", id="network"),
        pytest.param("privileged", True, id="privileged"),
        pytest.param("run_as_non_root", False, id="root-user"),
        pytest.param("root_filesystem_read_only", False, id="writable-root"),
        pytest.param("cap_drop_all", False, id="capabilities"),
        pytest.param("no_new_privileges", False, id="new-privileges"),
        pytest.param("docker_socket_mounted", True, id="docker-socket"),
        pytest.param("workspace_mount_target", "/host", id="mount-target"),
        pytest.param("workspace_mount_read_only", True, id="mount-mode"),
        pytest.param("maximum_mounts", 2, id="extra-mounts"),
    ],
)
def test_tampered_package_policy_is_rejected_at_construction(
    field: str,
    tampered_value: object,
    tmp_path: Path,
) -> None:
    policy = json.loads(
        (Path(__file__).parents[2] / "sandbox" / "image-policy.json").read_text(
            encoding="utf-8"
        )
    )
    policy[field] = tampered_value
    tampered_path = tmp_path / "image-policy.json"
    tampered_path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(BoundaryIntegrityError) as caught:
        Guardrail(
            secret_detector=EmptySecretDetector(),
            credentials_present=False,
            workspace_id="workspace-1",
            policy_path=tampered_path,
        )

    assert caught.value.code == "BOUNDARY_INTEGRITY_ERROR"


@pytest.mark.parametrize("mutation", ["missing", "unknown"])
def test_package_policy_schema_is_closed(mutation: str, tmp_path: Path) -> None:
    policy = json.loads(
        (Path(__file__).parents[2] / "sandbox" / "image-policy.json").read_text(
            encoding="utf-8"
        )
    )
    if mutation == "missing":
        del policy["network_mode"]
    else:
        policy["allow_host_network"] = True
    tampered_path = tmp_path / "image-policy.json"
    tampered_path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(BoundaryIntegrityError):
        Guardrail(
            secret_detector=EmptySecretDetector(),
            credentials_present=False,
            workspace_id="workspace-1",
            policy_path=tampered_path,
        )


def test_valid_normalized_action_passes() -> None:
    result = guardrail().check(action())

    assert result.decision is GuardrailDecision.PASS
    assert result.reason is GuardrailReason.PASS


@pytest.mark.parametrize(
    ("action_type", "args", "reason"),
    [
        ("read_file", {"path": "../outside"}, GuardrailReason.PATH_ESCAPE),
        (
            "read_file",
            {"path": "var/run/docker.sock"},
            GuardrailReason.DOCKER_SOCKET,
        ),
        (
            "shell",
            {
                "argv": ["bash", "-c", "echo unsafe"],
                "cwd": ".",
                "env": {},
                "timeout_seconds": 30,
                "stdin": None,
            },
            GuardrailReason.INTERPRETER_EVAL,
        ),
        (
            "shell",
            {
                "argv": ["pytest"],
                "cwd": ".",
                "env": {},
                "timeout_seconds": 301,
                "stdin": None,
            },
            GuardrailReason.RESOURCE_LIMIT,
        ),
    ],
)
def test_action_hard_boundaries_return_stable_denials(
    action_type: str, args: dict[str, object], reason: GuardrailReason
) -> None:
    result = guardrail().check(action(action_type, args))

    assert result.decision is GuardrailDecision.DENY
    assert result.reason is reason


@pytest.mark.parametrize(
    "reserved_key",
    [
        "network",
        "mounts",
        "privileged",
        "resources",
        "docker_socket",
        "cpu",
        "memory",
        "pids",
    ],
)
def test_reserved_sandbox_controls_are_denied_defense_in_depth(
    reserved_key: str,
) -> None:
    result = guardrail().check(
        action(
            normalized_args={
                "argv": ["pytest"],
                "cwd": ".",
                "env": {},
                "timeout_seconds": 30,
                "stdin": None,
                reserved_key: "override",
            }
        )
    )

    assert result.decision is GuardrailDecision.DENY
    assert result.reason is GuardrailReason.RESERVED_SANDBOX_CONTROL


@pytest.mark.parametrize(
    "payload",
    [
        {"env": {"PYTEST_ADDOPTS": "prefix-host-key-suffix"}, "stdin": None},
        {"env": {}, "stdin": b"prefix-host-key-suffix"},
    ],
)
def test_exact_known_secret_in_sandbox_payload_is_denied(
    payload: dict[str, object],
) -> None:
    args: dict[str, object] = {
        "argv": ["pytest"],
        "cwd": ".",
        "timeout_seconds": 30,
        **payload,
    }
    result = Guardrail(
        secret_detector=FakeSecretDetector("host-key"),
        credentials_present=True,
        workspace_id="workspace-1",
    ).check(action(normalized_args=args))

    assert result.decision is GuardrailDecision.DENY
    assert result.reason is GuardrailReason.SECRET_CONTAINMENT


def test_exact_known_secret_in_write_content_is_denied_despite_policy_override() -> (
    None
):
    result = Guardrail(
        secret_detector=FakeSecretDetector("host-key"),
        credentials_present=True,
        workspace_id="workspace-1",
    ).check(
        action(
            "write_file",
            {
                "path": "src/generated.py",
                "content": "prefix-host-key-suffix",
                "policy_decision": "ALLOW",
            },
        )
    )

    assert result.decision is GuardrailDecision.DENY
    assert result.reason is GuardrailReason.SECRET_CONTAINMENT


def test_exact_known_secret_nested_in_argv_tuple_is_denied() -> None:
    result = Guardrail(
        secret_detector=FakeSecretDetector("host-key"),
        credentials_present=True,
        workspace_id="workspace-1",
    ).check(
        action(
            normalized_args={
                "argv": ["printf", "prefix-host-key-suffix"],
                "cwd": ".",
                "env": {},
                "timeout_seconds": 30,
                "stdin": None,
            }
        )
    )

    assert result.decision is GuardrailDecision.DENY
    assert result.reason is GuardrailReason.SECRET_CONTAINMENT


def test_empty_secret_detector_allows_benign_write_content() -> None:
    result = guardrail().check(
        action(
            "write_file",
            {"path": "src/generated.py", "content": "print('safe')\n"},
        )
    )

    assert result.decision is GuardrailDecision.PASS


def test_empty_secret_detector_allows_nonempty_benign_payload() -> None:
    result = guardrail().check(
        action(
            normalized_args={
                "argv": ["pytest"],
                "cwd": ".",
                "env": {"PYTEST_ADDOPTS": "-q"},
                "timeout_seconds": 30,
                "stdin": "benign input",
            }
        )
    )

    assert result.decision is GuardrailDecision.PASS


@pytest.mark.parametrize(
    ("detector", "credentials_present"),
    [(None, True), (object(), True), (None, False), (FakeSecretDetector(), False)],
)
def test_missing_or_inconsistent_detector_fails_closed(
    detector: object | None, credentials_present: bool
) -> None:
    result = Guardrail(
        secret_detector=detector,
        credentials_present=credentials_present,
        workspace_id="workspace-1",
    ).check(action())

    assert result.decision is GuardrailDecision.DENY
    assert result.reason is GuardrailReason.SECRET_DETECTOR_UNAVAILABLE


def test_policy_like_override_is_not_accepted() -> None:
    result = guardrail().check(
        action(
            normalized_args={
                "argv": ["pytest"],
                "cwd": ".",
                "env": {},
                "timeout_seconds": 30,
                "stdin": None,
                "privileged": True,
                "policy_decision": "ALLOW",
                "approved": True,
            }
        )
    )

    assert result.decision is GuardrailDecision.DENY
    assert result.reason is GuardrailReason.RESERVED_SANDBOX_CONTROL


def test_assert_enforcement_accepts_exact_or_stricter_limits() -> None:
    metadata = enforcement_metadata(
        cpu_limit=0.5,
        memory_limit_bytes=536_870_912,
        pids_limit=64,
        timeout_seconds=120,
    )

    guardrail().assert_enforcement(metadata)


def test_assert_enforcement_accepts_frozen_domain_metadata_round_trip() -> None:
    frozen_metadata = RawExecutionResult(
        sandbox_meta=enforcement_metadata()
    ).sandbox_meta

    guardrail().assert_enforcement(frozen_metadata)


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param(
            {k: v for k, v in enforcement_metadata().items() if k != "network_mode"},
            id="missing",
        ),
        pytest.param({**enforcement_metadata(), "unknown": True}, id="unknown"),
        pytest.param(
            enforcement_metadata(memory_limit_bytes="1073741824"), id="wrong-type"
        ),
        pytest.param(enforcement_metadata(cpu_limit=True), id="cpu-bool"),
        pytest.param(enforcement_metadata(cpu_limit=float("nan")), id="cpu-nan"),
        pytest.param(enforcement_metadata(cpu_limit=float("inf")), id="cpu-inf"),
        pytest.param(enforcement_metadata(memory_limit_bytes=True), id="memory-bool"),
        pytest.param(enforcement_metadata(pids_limit=True), id="pids-bool"),
        pytest.param(enforcement_metadata(timeout_seconds=True), id="timeout-bool"),
        pytest.param(enforcement_metadata(cpu_limit=1.1), id="cpu-over-limit"),
        pytest.param(
            enforcement_metadata(memory_limit_bytes=1_073_741_825),
            id="memory-over-limit",
        ),
        pytest.param(enforcement_metadata(pids_limit=129), id="pids-over-limit"),
        pytest.param(
            enforcement_metadata(timeout_seconds=301), id="timeout-over-limit"
        ),
        pytest.param(enforcement_metadata(network_mode="bridge"), id="network"),
        pytest.param(enforcement_metadata(privileged=True), id="privileged"),
        pytest.param(
            enforcement_metadata(docker_socket_mounted=True), id="docker-socket"
        ),
        pytest.param(
            enforcement_metadata(
                mounts=[
                    {
                        "type": "bind",
                        "source_workspace_id": "workspace-1",
                        "target": "/workspace",
                        "read_only": False,
                    },
                    {
                        "type": "bind",
                        "source_workspace_id": "workspace-1",
                        "target": "/extra",
                        "read_only": False,
                    },
                ]
            ),
            id="extra-mount",
        ),
        pytest.param(
            enforcement_metadata(
                mounts=[
                    {
                        "type": "bind",
                        "source_workspace_id": "workspace-1",
                        "target": "/workspace",
                        "read_only": True,
                    }
                ]
            ),
            id="workspace-read-only",
        ),
    ],
)
def test_observed_boundary_failure_is_security_error(
    metadata: dict[str, object],
) -> None:
    with pytest.raises(BoundaryIntegrityError) as caught:
        guardrail().assert_enforcement(metadata)

    assert caught.value.code == "BOUNDARY_INTEGRITY_ERROR"
