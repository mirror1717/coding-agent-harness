"""ToolRegistry and structured Action validation.

Defines tool schemas and normalizes untrusted ActionProposals into
validated NormalizedActions. All tools only describe capabilities;
ToolDispatcher (T10) handles dispatch, DockerSandbox handles execution.
"""
from __future__ import annotations

import os
from typing import Any, Protocol

from coding_agent_harness.domain import ActionProposal, NormalizedAction
from coding_agent_harness.errors import ValidationError

# Hard limits
MAX_STDIN_BYTES = 10_000  # 10 KB
MAX_TIMEOUT_SECONDS = 300  # 5 minutes

# Allowed environment variables for shell actions
ALLOWED_ENV_VARS = frozenset({
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "TERM",
})

# Registered tool types
REGISTERED_TOOLS = frozenset({
    "list_files",
    "read_file",
    "write_file",
    "shell",
    "pytest",
})

# Shell interpreter patterns to reject
SHELL_INTERPRETER_PATTERNS = frozenset({
    "bash",
    "sh",
    "zsh",
    "fish",
    "dash",
    "ksh",
    "csh",
    "tcsh",
    "powershell",
    "pwsh",
    "cmd",
})


class Tool(Protocol):
    """Protocol for tool implementations (T10)."""

    async def execute(self, action: NormalizedAction) -> Any: ...


class ToolRegistry:
    """Registry for tool schemas and action normalization."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool_type: str, tool: Tool) -> None:
        """Register a tool implementation."""
        if tool_type not in REGISTERED_TOOLS:
            raise ValidationError(
                code="TOOL_UNKNOWN",
                message=f"Unknown tool type: {tool_type}",
            )
        self._tools[tool_type] = tool

    def normalize(
        self,
        proposal: ActionProposal,
        workspace_path: str,
    ) -> NormalizedAction:
        """Normalize and validate an ActionProposal.

        Args:
            proposal: Raw action proposal
            workspace_path: Absolute path to workspace root

        Returns:
            NormalizedAction with validated parameters

        Raises:
            ValidationError: If proposal is invalid
        """
        tool_type = proposal.type

        # Check tool is registered
        if tool_type not in REGISTERED_TOOLS:
            raise ValidationError(
                code="TOOL_UNKNOWN",
                message=f"Unknown tool type: {tool_type}",
            )

        # Normalize based on tool type
        if tool_type == "shell":
            normalized_args = self._normalize_shell(proposal.raw_args, workspace_path)
        elif tool_type in ("read_file", "write_file"):
            normalized_args = self._normalize_file(proposal.raw_args, workspace_path)
        elif tool_type == "list_files":
            normalized_args = self._normalize_list(proposal.raw_args, workspace_path)
        elif tool_type == "pytest":
            normalized_args = self._normalize_pytest(proposal.raw_args, workspace_path)
        else:
            raise ValidationError(
                code="TOOL_UNKNOWN",
                message=f"No normalizer for tool type: {tool_type}",
            )

        return NormalizedAction(
            action_id=proposal.action_id,
            source=proposal.source,
            parent_action_id=proposal.parent_action_id,
            type=tool_type,
            normalized_args=normalized_args,
            workspace_id=proposal.workspace_id,
            round=proposal.round,
        )

    def _normalize_shell(
        self,
        raw_args: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        """Normalize and validate shell action arguments."""
        # Reject shell string (must have argv)
        if "command" in raw_args:
            raise ValidationError(
                code="SHELL_STRING_FORBIDDEN",
                message="Shell action must use argv array, not command string",
            )

        # Check for extra fields
        allowed_fields = {"argv", "cwd", "env", "timeout_seconds", "stdin"}
        extra = set(raw_args.keys()) - allowed_fields
        if extra:
            raise ValidationError(
                code="EXTRA_FIELDS",
                message=f"Extra fields not allowed: {sorted(extra)}",
            )

        # Validate argv
        argv = raw_args.get("argv")
        if not isinstance(argv, list):
            raise ValidationError(
                code="ARGV_REQUIRED",
                message="argv must be a list",
            )
        if len(argv) == 0:
            raise ValidationError(
                code="ARGV_EMPTY",
                message="argv must not be empty",
            )
        if not all(isinstance(arg, str) for arg in argv):
            raise ValidationError(
                code="ARGV_TYPE",
                message="All argv elements must be strings",
            )

        # Reject shell interpreter patterns
        self._check_shell_interpreter(argv)

        # Validate cwd
        cwd = raw_args.get("cwd", ".")
        if not isinstance(cwd, str):
            raise ValidationError(
                code="CWD_TYPE",
                message="cwd must be a string",
            )
        self._validate_path_in_workspace(cwd, workspace_path)

        # Validate env
        env = raw_args.get("env", {})
        if not isinstance(env, dict):
            raise ValidationError(
                code="ENV_TYPE",
                message="env must be a dict",
            )
        self._validate_env(env)

        # Validate timeout
        timeout = raw_args.get("timeout_seconds", MAX_TIMEOUT_SECONDS)
        if not isinstance(timeout, int):
            raise ValidationError(
                code="TIMEOUT_TYPE",
                message="timeout_seconds must be an int",
            )
        if timeout > MAX_TIMEOUT_SECONDS:
            raise ValidationError(
                code="TIMEOUT_EXCEEDED",
                message=f"timeout_seconds exceeds maximum: {MAX_TIMEOUT_SECONDS}",
            )
        if timeout <= 0:
            raise ValidationError(
                code="TIMEOUT_INVALID",
                message="timeout_seconds must be positive",
            )

        # Validate stdin
        stdin = raw_args.get("stdin")
        if stdin is not None:
            if not isinstance(stdin, str):
                raise ValidationError(
                    code="STDIN_TYPE",
                    message="stdin must be a string or null",
                )
            if len(stdin.encode("utf-8")) > MAX_STDIN_BYTES:
                raise ValidationError(
                    code="STDIN_TOO_LARGE",
                    message=f"stdin exceeds maximum: {MAX_STDIN_BYTES} bytes",
                )

        result: dict[str, Any] = {"argv": argv, "cwd": cwd}
        if env:
            result["env"] = env
        if timeout != MAX_TIMEOUT_SECONDS:
            result["timeout_seconds"] = timeout
        if stdin is not None:
            result["stdin"] = stdin

        return result

    def _normalize_file(
        self,
        raw_args: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        """Normalize and validate file action arguments."""
        # Check for extra fields
        allowed_fields = {"path"}
        extra = set(raw_args.keys()) - allowed_fields
        if extra:
            raise ValidationError(
                code="EXTRA_FIELDS",
                message=f"Extra fields not allowed: {sorted(extra)}",
            )

        path = raw_args.get("path")
        if not isinstance(path, str):
            raise ValidationError(
                code="PATH_REQUIRED",
                message="path must be a string",
            )

        self._validate_path_in_workspace(path, workspace_path)

        return {"path": path}

    def _normalize_list(
        self,
        raw_args: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        """Normalize and validate list_files arguments."""
        # Check for extra fields
        allowed_fields = {"path"}
        extra = set(raw_args.keys()) - allowed_fields
        if extra:
            raise ValidationError(
                code="EXTRA_FIELDS",
                message=f"Extra fields not allowed: {sorted(extra)}",
            )

        path = raw_args.get("path", ".")
        if not isinstance(path, str):
            raise ValidationError(
                code="PATH_TYPE",
                message="path must be a string",
            )

        self._validate_path_in_workspace(path, workspace_path)

        return {"path": path}

    def _normalize_pytest(
        self,
        raw_args: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        """Normalize and validate pytest arguments."""
        # Check for extra fields
        allowed_fields = {"args", "cwd", "timeout_seconds"}
        extra = set(raw_args.keys()) - allowed_fields
        if extra:
            raise ValidationError(
                code="EXTRA_FIELDS",
                message=f"Extra fields not allowed: {sorted(extra)}",
            )

        args = raw_args.get("args", [])
        if not isinstance(args, list):
            raise ValidationError(
                code="ARGS_TYPE",
                message="args must be a list",
            )
        if not all(isinstance(arg, str) for arg in args):
            raise ValidationError(
                code="ARGS_TYPE",
                message="All args elements must be strings",
            )

        cwd = raw_args.get("cwd", ".")
        if not isinstance(cwd, str):
            raise ValidationError(
                code="CWD_TYPE",
                message="cwd must be a string",
            )
        self._validate_path_in_workspace(cwd, workspace_path)

        timeout = raw_args.get("timeout_seconds", MAX_TIMEOUT_SECONDS)
        if not isinstance(timeout, int):
            raise ValidationError(
                code="TIMEOUT_TYPE",
                message="timeout_seconds must be an int",
            )
        if timeout > MAX_TIMEOUT_SECONDS:
            raise ValidationError(
                code="TIMEOUT_EXCEEDED",
                message=f"timeout_seconds exceeds maximum: {MAX_TIMEOUT_SECONDS}",
            )

        result: dict[str, Any] = {"args": args, "cwd": cwd}
        if timeout != MAX_TIMEOUT_SECONDS:
            result["timeout_seconds"] = timeout

        return result

    def _check_shell_interpreter(self, argv: list[str]) -> None:
        """Reject shell interpreter patterns in argv."""
        if not argv:
            return

        cmd = argv[0].lower()
        cmd_base = os.path.basename(cmd)

        # Reject known shell interpreters
        if cmd_base in SHELL_INTERPRETER_PATTERNS:
            # Check for -c flag
            if len(argv) >= 2 and argv[1] == "-c":
                raise ValidationError(
                    code="SHELL_INTERPRETER_FORBIDDEN",
                    message=f"Shell interpreter with -c is forbidden: {argv[0]}",
                )
            # Even without -c, reject shell interpreters
            raise ValidationError(
                code="SHELL_INTERPRETER_FORBIDDEN",
                message=f"Shell interpreter is forbidden: {argv[0]}",
            )

    def _validate_path_in_workspace(self, path: str, workspace_path: str) -> None:
        """Validate that path is within workspace."""
        # Reject absolute paths
        if os.path.isabs(path):
            raise ValidationError(
                code="PATH_ABSOLUTE",
                message=f"Absolute paths are not allowed: {path}",
            )

        # Reject parent directory references
        if ".." in path.split(os.sep) or ".." in path.split("/"):
            raise ValidationError(
                code="PATH_ESCAPE",
                message=f"Path escapes workspace: {path}",
            )

        # Resolve path relative to workspace
        resolved = os.path.normpath(os.path.join(workspace_path, path))

        # Check if resolved path is within workspace
        workspace_real = os.path.realpath(workspace_path)
        resolved_real = os.path.realpath(resolved)

        if not resolved_real.startswith(workspace_real + os.sep) and resolved_real != workspace_real:
            raise ValidationError(
                code="PATH_ESCAPE",
                message=f"Path escapes workspace (symlink or other): {path}",
            )

    def _validate_env(self, env: dict[str, str]) -> None:
        """Validate environment variables."""
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValidationError(
                    code="ENV_TYPE",
                    message="env keys and values must be strings",
                )
            if key not in ALLOWED_ENV_VARS:
                raise ValidationError(
                    code="ENV_NOT_ALLOWED",
                    message=f"Environment variable not in allowlist: {key}",
                )
