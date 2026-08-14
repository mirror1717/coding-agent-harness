"""Unit tests for Docker sandbox command building and result classification."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coding_agent_harness.domain import NormalizedAction
from coding_agent_harness.guardrail import EmptySecretDetector, Guardrail
from coding_agent_harness.sandbox import (
    OUTCOME_ERROR,
    OUTCOME_FAILURE,
    OUTCOME_OOM,
    OUTCOME_PID_LIMIT,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT,
    DockerCommandBuilder,
    SandboxConfig,
    classify_outcome,
)

SOURCE_FILE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "coding_agent_harness"
    / "sandbox.py"
)
DOCKERFILE = Path(__file__).resolve().parents[2] / "sandbox" / "Dockerfile"


def shell_action(
    argv: list[str] | None = None,
    cwd: str = ".",
    env: dict[str, str] | None = None,
    timeout_seconds: int = 30,
    stdin: str | None = None,
    workspace_id: str = "workspace-1",
    action_id: str = "action-1",
) -> NormalizedAction:
    return NormalizedAction(
        action_id=action_id,
        type="shell",
        raw_args={},
        normalized_args={
            "argv": tuple(argv) if argv is not None else ("pytest", "-q"),
            "cwd": cwd,
            "env": env or {},
            "timeout_seconds": timeout_seconds,
            "stdin": stdin,
        },
        workspace_id=workspace_id,
    )


def pytest_action(
    argv: list[str] | None = None,
    cwd: str = ".",
    env: dict[str, str] | None = None,
    timeout_seconds: int = 60,
    stdin: str | None = None,
    workspace_id: str = "workspace-1",
) -> NormalizedAction:
    return NormalizedAction(
        action_id="action-pytest",
        type="pytest",
        raw_args={},
        normalized_args={
            "argv": tuple(argv) if argv is not None else ("python", "-m", "pytest", "-q"),
            "cwd": cwd,
            "env": env or {},
            "timeout_seconds": timeout_seconds,
            "stdin": stdin,
        },
        workspace_id=workspace_id,
    )


class TestBuilderEnforcesAllFlags:
    """Verify the Docker argv contains all required hard boundary flags."""

    def test_builder_enforces_all_flags(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["pytest", "-q"])
        argv = builder.build_argv(action, tmp_path, "test-container-1")

        assert argv[0] == "docker"
        assert argv[1] == "run"

        for flag in (
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
        ):
            assert flag in argv, f"missing required flag: {flag}"

        user_flags = [a for a in argv if a.startswith("--user=")]
        assert len(user_flags) == 1
        user_value = user_flags[0].split("=", 1)[1]
        assert user_value != "0"
        assert user_value != "root"
        assert user_value != "0:0"

        tmpfs_flags = [a for a in argv if a.startswith("--tmpfs")]
        assert len(tmpfs_flags) >= 1
        assert any("/tmp" in f for f in tmpfs_flags)

        assert any(a.startswith("--cpus=") for a in argv)
        assert any(a.startswith("--memory=") for a in argv)
        assert any(a.startswith("--pids-limit=") for a in argv)

        assert "--name=test-container-1" in argv

        config = builder.config
        assert config.image in argv

        assert "pytest" in argv
        assert "-q" in argv

    def test_builder_enforces_flags_for_pytest_action(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = pytest_action(argv=["python", "-m", "pytest", "-q"])
        argv = builder.build_argv(action, tmp_path, "pytest-container")

        for flag in (
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
        ):
            assert flag in argv, f"missing required flag: {flag}"
        assert "--name=pytest-container" in argv

    def test_builder_sets_workdir_to_workspace(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["ls"], cwd=".")
        argv = builder.build_argv(action, tmp_path, "container-1")

        workdir_flags = [a for a in argv if a.startswith("--workdir=")]
        assert len(workdir_flags) == 1
        assert "/workspace" in workdir_flags[0]

    def test_builder_sets_workdir_for_subdirectory(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["ls"], cwd="tests")
        argv = builder.build_argv(action, tmp_path, "container-1")

        workdir_flags = [a for a in argv if a.startswith("--workdir=")]
        assert len(workdir_flags) == 1
        assert workdir_flags[0] == "--workdir=/workspace/tests"

    def test_builder_passes_env_variables(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(
            argv=["pytest"],
            env={"PYTEST_ADDOPTS": "-v", "PYTHONUNBUFFERED": "1"},
        )
        argv = builder.build_argv(action, tmp_path, "container-1")

        env_flags = [a for a in argv if a.startswith("--env=")]
        env_values = {f.split("=", 1)[1] for f in env_flags}
        assert "PYTEST_ADDOPTS=-v" in env_values
        assert "PYTHONUNBUFFERED=1" in env_values

    def test_builder_adds_interactive_flag_for_stdin(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["cat"], stdin="hello")
        argv = builder.build_argv(action, tmp_path, "container-1")

        assert "-i" in argv

    def test_builder_no_interactive_flag_without_stdin(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["ls"], stdin=None)
        argv = builder.build_argv(action, tmp_path, "container-1")

        assert "-i" not in argv

    def test_builder_preserves_action_argv_order(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["python", "-c", "print('hi')"])
        argv = builder.build_argv(action, tmp_path, "container-1")

        config = builder.config
        image_index = argv.index(config.image)
        command = argv[image_index + 1 :]
        assert command == ["python", "-c", "print('hi')"]

    def test_builder_rejects_empty_argv(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=[])
        with pytest.raises((ValueError, TypeError)):
            builder.build_argv(action, tmp_path, "container-1")


class TestOnlyWorkspaceMountExists:
    """Verify only the workspace is mounted, nothing else."""

    def test_only_workspace_mount_exists(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["pytest", "-q"])
        argv = builder.build_argv(action, tmp_path, "test-container-1")

        v_indices = [i for i, a in enumerate(argv) if a == "-v"]
        assert len(v_indices) == 1, "exactly one mount must exist"

        mount_spec = argv[v_indices[0] + 1]
        assert str(tmp_path) in mount_spec
        assert "/workspace" in mount_spec
        assert mount_spec.endswith(":rw")

        assert not any("docker.sock" in a for a in argv)
        assert not any("/var/run/docker" in a for a in argv)

        assert "--privileged" not in argv
        assert not any(a == "--privileged" for a in argv)

        assert not any(a.startswith("--mount") for a in argv)
        assert not any(a.startswith("--volume=") for a in argv)

    def test_no_docker_socket_mounted(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["ls"])
        argv = builder.build_argv(action, tmp_path, "container-1")

        for arg in argv:
            assert "docker.sock" not in arg
            assert "/var/run/docker" not in arg

    def test_no_credential_mounts(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["ls"])
        argv = builder.build_argv(action, tmp_path, "container-1")

        v_indices = [i for i, a in enumerate(argv) if a == "-v"]
        assert len(v_indices) == 1
        mount_spec = argv[v_indices[0] + 1]
        assert "/workspace" in mount_spec
        assert ".ssh" not in mount_spec
        assert ".env" not in mount_spec
        assert ".aws" not in mount_spec
        assert "keyring" not in mount_spec.lower()

    def test_builder_accepts_no_mount_parameters(self, tmp_path: Path) -> None:
        """The builder must not accept caller-supplied mount parameters."""
        builder = DockerCommandBuilder()
        action = shell_action(argv=["ls"])
        argv = builder.build_argv(action, tmp_path, "container-1")
        v_count = sum(1 for a in argv if a == "-v")
        assert v_count == 1

    def test_workspace_mount_is_read_write(self, tmp_path: Path) -> None:
        builder = DockerCommandBuilder()
        action = shell_action(argv=["ls"])
        argv = builder.build_argv(action, tmp_path, "container-1")

        v_indices = [i for i, a in enumerate(argv) if a == "-v"]
        mount_spec = argv[v_indices[0] + 1]
        assert mount_spec.endswith(":rw")
        assert ":ro" not in mount_spec


class TestShellApiNeverUsed:
    """Verify the sandbox source never uses shell execution APIs."""

    def test_shell_api_never_used(self) -> None:
        assert SOURCE_FILE.exists(), f"source file not found: {SOURCE_FILE}"
        source = SOURCE_FILE.read_text(encoding="utf-8")

        assert "create_subprocess_shell" not in source
        assert "shell=True" not in source

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "shell"
                and isinstance(node.value, ast.Constant)
                and node.value.value is True
            ):
                pytest.fail("shell=True found in sandbox source")
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name == "create_subprocess_shell":
                    pytest.fail("create_subprocess_shell found in sandbox source")


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


class TestTimeoutAndOomClassification:
    """Verify result classification for timeout, OOM, and exit codes."""

    @pytest.mark.parametrize(
        ("exit_code", "timed_out", "oom_killed", "expected"),
        [
            (0, False, False, OUTCOME_SUCCESS),
            (1, False, False, OUTCOME_FAILURE),
            (2, False, False, OUTCOME_FAILURE),
            (137, False, False, OUTCOME_FAILURE),
            (None, True, False, OUTCOME_TIMEOUT),
            (137, True, False, OUTCOME_TIMEOUT),
            (None, False, True, OUTCOME_OOM),
            (137, False, True, OUTCOME_OOM),
            (None, True, True, OUTCOME_TIMEOUT),
        ],
    )
    def test_timeout_and_oom_classification(
        self,
        exit_code: int | None,
        timed_out: bool,
        oom_killed: bool,
        expected: str,
    ) -> None:
        assert classify_outcome(exit_code, timed_out, oom_killed) == expected

    def test_success_outcome(self) -> None:
        assert classify_outcome(0, False, False) == OUTCOME_SUCCESS

    def test_failure_outcome(self) -> None:
        assert classify_outcome(1, False, False) == OUTCOME_FAILURE

    def test_timeout_outcome(self) -> None:
        assert classify_outcome(None, True, False) == OUTCOME_TIMEOUT

    def test_oom_outcome(self) -> None:
        assert classify_outcome(137, False, True) == OUTCOME_OOM

    def test_error_outcome_for_missing_exit_code(self) -> None:
        assert classify_outcome(None, False, False) == OUTCOME_ERROR

    def test_timeout_takes_priority_over_oom(self) -> None:
        assert classify_outcome(137, True, True) == OUTCOME_TIMEOUT

    def test_pid_limit_classification(self) -> None:
        assert classify_outcome(137, False, False, pid_limit_exceeded=True) == (
            OUTCOME_PID_LIMIT
        )


class TestSandboxMetadata:
    """Verify sandbox metadata is compatible with Guardrail.assert_enforcement."""

    def test_sandbox_meta_passes_guardrail_enforcement(self) -> None:
        builder = DockerCommandBuilder()
        meta = builder.build_sandbox_meta("workspace-1")

        guardrail = Guardrail(
            secret_detector=EmptySecretDetector(),
            credentials_present=False,
            workspace_id="workspace-1",
        )
        guardrail.assert_enforcement(meta)

    def test_sandbox_meta_has_correct_mount(self) -> None:
        builder = DockerCommandBuilder()
        meta = builder.build_sandbox_meta("workspace-1")

        mounts = meta["mounts"]
        assert len(mounts) == 1
        mount = mounts[0]
        assert mount["type"] == "bind"
        assert mount["source_workspace_id"] == "workspace-1"
        assert mount["target"] == "/workspace"
        assert mount["read_only"] is False

    def test_sandbox_meta_has_security_settings(self) -> None:
        builder = DockerCommandBuilder()
        meta = builder.build_sandbox_meta("workspace-1")

        assert meta["network_mode"] == "none"
        assert meta["privileged"] is False
        assert meta["run_as_non_root"] is True
        assert meta["root_filesystem_read_only"] is True
        assert meta["cap_drop_all"] is True
        assert meta["no_new_privileges"] is True
        assert meta["docker_socket_mounted"] is False

    def test_sandbox_meta_has_resource_limits(self) -> None:
        builder = DockerCommandBuilder()
        meta = builder.build_sandbox_meta("workspace-1")

        assert meta["cpu_limit"] == 1.0
        assert meta["memory_limit_bytes"] == 1_073_741_824
        assert meta["pids_limit"] == 128
        assert meta["timeout_seconds"] == 300
        assert meta["version"] == 1


class TestSandboxConfig:
    """Verify sandbox configuration defaults and immutability."""

    def test_default_config_has_security_defaults(self) -> None:
        config = SandboxConfig()
        assert config.cpu_limit == 1.0
        assert config.memory_limit_bytes == 1_073_741_824
        assert config.pids_limit == 128
        assert config.timeout_seconds == 300
        assert config.network_mode == "none"
        assert config.workspace_mount_target == "/workspace"
        assert config.non_root_user == "1000:1000"

    def test_config_is_frozen(self) -> None:
        config = SandboxConfig()
        with pytest.raises(AttributeError):
            config.cpu_limit = 2.0  # type: ignore[misc]


class TestDockerfile:
    """Verify the sandbox Dockerfile is minimal and non-root."""

    def test_dockerfile_exists(self) -> None:
        assert DOCKERFILE.exists(), f"Dockerfile not found: {DOCKERFILE}"

    def test_dockerfile_uses_python_base(self) -> None:
        content = DOCKERFILE.read_text(encoding="utf-8")
        assert "python" in content.lower()
        assert "3.12" in content

    def test_dockerfile_creates_non_root_user(self) -> None:
        content = DOCKERFILE.read_text(encoding="utf-8")
        assert "USER" in content
        assert "USER 0" not in content
        assert "USER root" not in content

    def test_dockerfile_installs_pytest(self) -> None:
        content = DOCKERFILE.read_text(encoding="utf-8")
        assert "pytest" in content.lower()

    def test_dockerfile_sets_workspace(self) -> None:
        content = DOCKERFILE.read_text(encoding="utf-8")
        assert "WORKDIR" in content
        assert "/workspace" in content
