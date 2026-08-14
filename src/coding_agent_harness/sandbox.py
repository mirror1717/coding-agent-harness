"""Docker sandbox command building and result classification.

This module constructs controlled Docker argv from normalized actions
and classifies execution outcomes (timeout, OOM, exit code) into stable
RawExecutionResult outcomes. All process spawning uses direct argv exec
APIs; no shell-based execution is ever used.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .domain import NormalizedAction, RawExecutionResult
from .tools import Tool

SANDBOX_IMAGE = "coding-agent-harness-sandbox:latest"
POLICY_VERSION = 1
CPU_LIMIT = 1.0
MEMORY_LIMIT_BYTES = 1_073_741_824
PIDS_LIMIT = 128
TIMEOUT_SECONDS = 300
NETWORK_MODE = "none"
WORKSPACE_MOUNT_TARGET = "/workspace"
NON_ROOT_USER = "1000:1000"
TMPFS_TARGET = "/tmp"
TMPFS_SIZE = "64m"

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_OOM = "oom"
OUTCOME_PID_LIMIT = "pid_limit"
OUTCOME_ERROR = "error"

DOCKER_KILL_EXIT_CODE = 137


class Sandbox(Protocol):
    """Shared interface contract for sandbox execution."""

    async def execute(self, action: NormalizedAction) -> RawExecutionResult: ...
    async def cancel(self, execution_id: str) -> None: ...


@dataclass(frozen=True)
class SandboxConfig:
    """Frozen configuration for the Docker sandbox boundary."""

    image: str = SANDBOX_IMAGE
    cpu_limit: float = CPU_LIMIT
    memory_limit_bytes: int = MEMORY_LIMIT_BYTES
    pids_limit: int = PIDS_LIMIT
    timeout_seconds: int = TIMEOUT_SECONDS
    network_mode: str = NETWORK_MODE
    workspace_mount_target: str = WORKSPACE_MOUNT_TARGET
    non_root_user: str = NON_ROOT_USER
    tmpfs_target: str = TMPFS_TARGET
    tmpfs_size: str = TMPFS_SIZE


class DockerCommandBuilder:
    """Build controlled Docker argv from normalized actions.

    The builder enforces all hard boundary flags and never accepts
    caller-supplied mounts, network settings, or privileged mode.
    """

    REQUIRED_FLAGS: tuple[str, ...] = (
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    )

    _PROCESS_ACTIONS = frozenset({"shell", "pytest"})

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()

    @property
    def config(self) -> SandboxConfig:
        return self._config

    def build_argv(
        self,
        action: NormalizedAction,
        workspace_path: Path,
        container_name: str,
    ) -> list[str]:
        """Build the full Docker run argv for a normalized action."""
        config = self._config
        action_args = action.normalized_args

        argv: list[str] = ["docker", "run"]
        argv.extend(self.REQUIRED_FLAGS)
        argv.append(f"--user={config.non_root_user}")
        argv.append(f"--tmpfs={config.tmpfs_target}:size={config.tmpfs_size}")
        argv.append(f"--cpus={config.cpu_limit}")
        argv.append(f"--memory={config.memory_limit_bytes}")
        argv.append(f"--pids-limit={config.pids_limit}")
        argv.append(f"--name={container_name}")

        cwd = "."
        if action.type in self._PROCESS_ACTIONS:
            cwd = str(action_args.get("cwd", "."))
        workdir = config.workspace_mount_target
        if cwd and cwd != ".":
            workdir = f"{config.workspace_mount_target}/{cwd}"
        argv.append(f"--workdir={workdir}")

        env = action_args.get("env", {})
        if isinstance(env, Mapping):
            for key, value in env.items():
                argv.append(f"--env={key}={value}")

        if action_args.get("stdin") is not None:
            argv.append("-i")

        argv.extend(["-v", f"{workspace_path}:{config.workspace_mount_target}:rw"])

        argv.append(config.image)

        command = self._extract_command(action)
        argv.extend(command)

        return argv

    def _extract_command(self, action: NormalizedAction) -> list[str]:
        """Extract the command argv from a normalized action."""
        if action.type not in self._PROCESS_ACTIONS:
            raise ValueError(f"unsupported action type: {action.type!r}")
        action_args = action.normalized_args
        command = action_args.get("argv")
        if not isinstance(command, list | tuple) or not command:
            raise ValueError(f"action {action.type!r} requires non-empty argv")
        return [str(arg) for arg in command]

    def build_sandbox_meta(self, workspace_id: str) -> Mapping[str, Any]:
        """Build the sandbox metadata for boundary verification."""
        config = self._config
        return {
            "version": POLICY_VERSION,
            "cpu_limit": config.cpu_limit,
            "memory_limit_bytes": config.memory_limit_bytes,
            "pids_limit": config.pids_limit,
            "timeout_seconds": config.timeout_seconds,
            "network_mode": config.network_mode,
            "privileged": False,
            "run_as_non_root": True,
            "root_filesystem_read_only": True,
            "cap_drop_all": True,
            "no_new_privileges": True,
            "docker_socket_mounted": False,
            "mounts": [
                {
                    "type": "bind",
                    "source_workspace_id": workspace_id,
                    "target": config.workspace_mount_target,
                    "read_only": False,
                }
            ],
        }


def classify_outcome(
    exit_code: int | None,
    timed_out: bool = False,
    oom_killed: bool = False,
    pid_limit_exceeded: bool = False,
) -> str:
    """Classify a process outcome into a stable outcome string.

    Priority: timeout > oom > pid_limit > error > success/failure.
    """
    if timed_out:
        return OUTCOME_TIMEOUT
    if oom_killed:
        return OUTCOME_OOM
    if pid_limit_exceeded:
        return OUTCOME_PID_LIMIT
    if exit_code is None:
        return OUTCOME_ERROR
    if exit_code == 0:
        return OUTCOME_SUCCESS
    return OUTCOME_FAILURE


class DockerSandbox:
    """Docker-based sandbox implementing the Sandbox protocol.

    Uses asyncio direct argv exec for all process spawning; no shell
    interpretation is ever invoked.
    """

    def __init__(
        self,
        workspace_path: Path,
        workspace_id: str,
        config: SandboxConfig | None = None,
    ) -> None:
        self._builder = DockerCommandBuilder(config)
        self._workspace_path = workspace_path
        self._workspace_id = workspace_id
        self._config = self._builder.config

    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        """Execute a normalized action in a Docker container."""
        execution_id = uuid.uuid4().hex
        container_name = f"harness-{execution_id}"

        argv = self._builder.build_argv(
            action, self._workspace_path, container_name
        )

        action_args = action.normalized_args
        timeout = int(
            action_args.get("timeout_seconds", self._config.timeout_seconds)
        )

        stdin_data = action_args.get("stdin")
        stdin_bytes: bytes | None = None
        if stdin_data is not None:
            stdin_bytes = str(stdin_data).encode("utf-8")

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_bytes else None,
        )

        timed_out = False
        try:
            await asyncio.wait_for(
                process.communicate(input=stdin_bytes), timeout=timeout
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()

        exit_code = process.returncode
        oom_killed = exit_code == DOCKER_KILL_EXIT_CODE and not timed_out
        outcome = classify_outcome(exit_code, timed_out, oom_killed)

        sandbox_meta = self._builder.build_sandbox_meta(self._workspace_id)

        return RawExecutionResult(
            execution_id=execution_id,
            action_id=action.action_id,
            exit_code=exit_code,
            stdout_artifact=None,
            stderr_artifact=None,
            report_artifacts=(),
            duration_seconds=0.0,
            outcome=outcome,
            sandbox_meta=sandbox_meta,
        )

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running execution by killing its container."""
        container_name = f"harness-{execution_id}"
        kill_argv = ["docker", "rm", "-f", container_name]
        process = await asyncio.create_subprocess_exec(
            *kill_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.wait()


class CapturedDockerSandbox:
    """DockerSandbox wrapper that captures stdout/stderr bytes for tools."""

    def __init__(
        self,
        workspace_path: Path,
        workspace_id: str,
        config: SandboxConfig | None = None,
    ) -> None:
        self._sandbox = DockerSandbox(workspace_path, workspace_id, config)
        self._workspace_path = workspace_path
        self._workspace_id = workspace_id

    @property
    def config(self) -> SandboxConfig:
        return self._sandbox._config

    async def execute_with_output(
        self, action: NormalizedAction
    ) -> tuple[RawExecutionResult, bytes, bytes]:
        """Execute and return (result, stdout_bytes, stderr_bytes)."""
        execution_id = uuid.uuid4().hex
        container_name = f"harness-{execution_id}"
        argv = self._sandbox._builder.build_argv(
            action, self._workspace_path, container_name
        )
        action_args = action.normalized_args
        timeout = int(
            action_args.get("timeout_seconds", self._sandbox._config.timeout_seconds)
        )
        stdin_data = action_args.get("stdin")
        stdin_bytes: bytes | None = None
        if stdin_data is not None:
            stdin_bytes = str(stdin_data).encode("utf-8")

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_bytes else None,
        )

        timed_out = False
        stdout_bytes = b""
        stderr_bytes = b""
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=stdin_bytes), timeout=timeout
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()

        exit_code = process.returncode
        oom_killed = exit_code == DOCKER_KILL_EXIT_CODE and not timed_out
        outcome = classify_outcome(exit_code, timed_out, oom_killed)
        sandbox_meta = self._sandbox._builder.build_sandbox_meta(self._workspace_id)

        result = RawExecutionResult(
            execution_id=execution_id,
            action_id=action.action_id,
            exit_code=exit_code,
            stdout_artifact=None,
            stderr_artifact=None,
            report_artifacts=(),
            duration_seconds=0.0,
            outcome=outcome,
            sandbox_meta=sandbox_meta,
        )
        return result, stdout_bytes, stderr_bytes

    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        result, _, _ = await self.execute_with_output(action)
        return result

    async def cancel(self, execution_id: str) -> None:
        await self._sandbox.cancel(execution_id)


class ShellTool:
    """Execute shell actions through the Docker sandbox."""

    def __init__(self, sandbox: CapturedDockerSandbox) -> None:
        self._sandbox = sandbox

    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        result, stdout, stderr = await self._sandbox.execute_with_output(action)
        return result.model_copy(update={
            "stdout_artifact": stdout.decode("utf-8", errors="replace")[:4096] or None,
            "stderr_artifact": stderr.decode("utf-8", errors="replace")[:4096] or None,
        })


class PytestTool:
    """Execute pytest actions through the Docker sandbox."""

    def __init__(self, sandbox: CapturedDockerSandbox) -> None:
        self._sandbox = sandbox

    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        result, stdout, stderr = await self._sandbox.execute_with_output(action)
        return result.model_copy(update={
            "stdout_artifact": stdout.decode("utf-8", errors="replace")[:8192] or None,
            "stderr_artifact": stderr.decode("utf-8", errors="replace")[:4096] or None,
        })


class ListFilesTool:
    """List files in a workspace directory through Docker."""

    def __init__(self, sandbox: CapturedDockerSandbox) -> None:
        self._sandbox = sandbox

    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        path = action.normalized_args.get("path", ".")
        shell_action = NormalizedAction(
            action_id=action.action_id,
            source=action.source,
            parent_action_id=action.parent_action_id,
            type="shell",
            raw_args={"argv": ["ls", "-la", str(path)], "cwd": "."},
            normalized_args={"argv": ["ls", "-la", str(path)], "cwd": "."},
            workspace_id=action.workspace_id,
            round=action.round,
        )
        result, stdout, stderr = await self._sandbox.execute_with_output(shell_action)
        return result.model_copy(update={
            "stdout_artifact": stdout.decode("utf-8", errors="replace")[:4096] or None,
            "stderr_artifact": stderr.decode("utf-8", errors="replace")[:4096] or None,
        })


class ReadFileTool:
    """Read a file from the workspace through Docker."""

    def __init__(self, sandbox: CapturedDockerSandbox) -> None:
        self._sandbox = sandbox

    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        path = action.normalized_args.get("path", ".")
        shell_action = NormalizedAction(
            action_id=action.action_id,
            source=action.source,
            parent_action_id=action.parent_action_id,
            type="shell",
            raw_args={"argv": ["cat", str(path)], "cwd": "."},
            normalized_args={"argv": ["cat", str(path)], "cwd": "."},
            workspace_id=action.workspace_id,
            round=action.round,
        )
        result, stdout, stderr = await self._sandbox.execute_with_output(shell_action)
        return result.model_copy(update={
            "stdout_artifact": stdout.decode("utf-8", errors="replace")[:8192] or None,
            "stderr_artifact": stderr.decode("utf-8", errors="replace")[:4096] or None,
        })


class WriteFileTool:
    """Write a file to the workspace through Docker."""

    def __init__(self, sandbox: CapturedDockerSandbox) -> None:
        self._sandbox = sandbox

    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        path = action.normalized_args.get("path", ".")
        content = action.normalized_args.get("content", "")
        shell_action = NormalizedAction(
            action_id=action.action_id,
            source=action.source,
            parent_action_id=action.parent_action_id,
            type="shell",
            raw_args={
                "argv": ["tee", str(path)],
                "cwd": ".",
                "stdin": str(content),
            },
            normalized_args={
                "argv": ["tee", str(path)],
                "cwd": ".",
                "stdin": str(content),
            },
            workspace_id=action.workspace_id,
            round=action.round,
        )
        result, stdout, stderr = await self._sandbox.execute_with_output(shell_action)
        return result.model_copy(update={
            "stdout_artifact": stdout.decode("utf-8", errors="replace")[:4096] or None,
            "stderr_artifact": stderr.decode("utf-8", errors="replace")[:4096] or None,
        })


def create_default_tools(
    workspace_path: Path, workspace_id: str, config: SandboxConfig | None = None
) -> dict[str, Tool]:
    """Create the default set of tools backed by Docker sandbox."""
    sandbox = CapturedDockerSandbox(workspace_path, workspace_id, config)
    return {
        "list_files": ListFilesTool(sandbox),
        "read_file": ReadFileTool(sandbox),
        "write_file": WriteFileTool(sandbox),
        "shell": ShellTool(sandbox),
        "pytest": PytestTool(sandbox),
    }
