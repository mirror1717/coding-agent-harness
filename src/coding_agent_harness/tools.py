"""Strict action schemas and pure normalization for harness tools."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
)

from .domain import ActionProposal, NormalizedAction, RawExecutionResult
from .errors import HarnessValidationError

MAX_TIMEOUT_SECONDS = 300
MAX_STDIN_BYTES = 65_536
ALLOWED_ENV = frozenset({"PYTEST_ADDOPTS", "PYTHONUNBUFFERED"})


@dataclass(frozen=True)
class _InterpreterGrammar:
    evaluation_flags: frozenset[str]
    value_options: frozenset[str] = frozenset()
    attached_evaluation_prefixes: frozenset[str] = frozenset()
    attached_value_prefixes: frozenset[str] = frozenset()
    evaluation_cluster_letters: frozenset[str] = frozenset()
    source_cluster_letters: frozenset[str] = frozenset()
    case_sensitive: bool = True
    reads_stdin_without_script: bool = True


_INTERPRETER_GRAMMARS: Mapping[str, _InterpreterGrammar] = MappingProxyType(
    {
        "bash": _InterpreterGrammar(
            frozenset({"-c", "--command"}),
            frozenset({"-O", "-o", "--init-file", "--rcfile"}),
            frozenset({"-c"}),
            frozenset({"-O", "-o"}),
            frozenset({"c"}),
            frozenset({"s"}),
        ),
        "sh": _InterpreterGrammar(
            frozenset({"-c"}),
            frozenset({"-o"}),
            frozenset({"-c"}),
            frozenset({"-o"}),
            frozenset({"c"}),
            frozenset({"s"}),
        ),
        "ash": _InterpreterGrammar(
            frozenset({"-c"}),
            frozenset({"-o"}),
            frozenset({"-c"}),
            frozenset({"-o"}),
            frozenset({"c"}),
            frozenset({"s"}),
        ),
        "dash": _InterpreterGrammar(
            frozenset({"-c"}),
            frozenset({"-o"}),
            frozenset({"-c"}),
            frozenset({"-o"}),
            frozenset({"c"}),
            frozenset({"s"}),
        ),
        "zsh": _InterpreterGrammar(
            frozenset({"-c", "--command"}),
            frozenset({"-o"}),
            frozenset({"-c"}),
            frozenset({"-o"}),
            frozenset({"c"}),
            frozenset({"s"}),
        ),
        "ksh": _InterpreterGrammar(
            frozenset({"-c"}),
            frozenset({"-o"}),
            frozenset({"-c"}),
            frozenset({"-o"}),
            frozenset({"c"}),
            frozenset({"s"}),
        ),
        "fish": _InterpreterGrammar(
            frozenset({"-c", "-C", "--command", "--init-command"}),
            attached_evaluation_prefixes=frozenset({"-c", "-C"}),
            evaluation_cluster_letters=frozenset({"c", "C"}),
            source_cluster_letters=frozenset({"s"}),
        ),
        "csh": _InterpreterGrammar(
            frozenset({"-c"}),
            attached_evaluation_prefixes=frozenset({"-c"}),
            source_cluster_letters=frozenset({"s"}),
        ),
        "tcsh": _InterpreterGrammar(
            frozenset({"-c"}),
            attached_evaluation_prefixes=frozenset({"-c"}),
            source_cluster_letters=frozenset({"s"}),
        ),
        "powershell": _InterpreterGrammar(
            frozenset(
                {
                    "-c",
                    "-command",
                    "-commandwithargs",
                    "-cwa",
                    "-e",
                    "-ec",
                    "-encodedcommand",
                    "-enc",
                }
            ),
            frozenset(
                {
                    "-configurationname",
                    "-custompipename",
                    "-executionpolicy",
                    "-inputformat",
                    "-outputformat",
                    "-settingsfile",
                    "-version",
                    "-windowstyle",
                    "-workingdirectory",
                }
            ),
            case_sensitive=False,
        ),
        "pwsh": _InterpreterGrammar(
            frozenset(
                {
                    "-c",
                    "-command",
                    "-commandwithargs",
                    "-cwa",
                    "-e",
                    "-ec",
                    "-encodedcommand",
                    "-enc",
                }
            ),
            frozenset(
                {
                    "-configurationname",
                    "-custompipename",
                    "-executionpolicy",
                    "-inputformat",
                    "-outputformat",
                    "-settingsfile",
                    "-version",
                    "-windowstyle",
                    "-workingdirectory",
                }
            ),
            case_sensitive=False,
        ),
        "cmd": _InterpreterGrammar(
            frozenset({"/c", "/k"}),
            attached_evaluation_prefixes=frozenset({"/c", "/k"}),
            case_sensitive=False,
            reads_stdin_without_script=False,
        ),
        "python": _InterpreterGrammar(
            frozenset({"-c"}),
            frozenset({"-W", "-X", "--check-hash-based-pycs"}),
            frozenset({"-c"}),
            frozenset({"-W", "-X"}),
            frozenset({"c"}),
        ),
        "pypy": _InterpreterGrammar(
            frozenset({"-c"}),
            frozenset({"-W", "-X", "--check-hash-based-pycs"}),
            frozenset({"-c"}),
            frozenset({"-W", "-X"}),
            frozenset({"c"}),
        ),
        "node": _InterpreterGrammar(
            frozenset({"-e", "--eval", "-p", "--print"}),
            frozenset({"-r", "--conditions", "--import", "--loader", "--require"}),
            frozenset({"-e", "-p"}),
            frozenset({"-r"}),
        ),
        "perl": _InterpreterGrammar(
            frozenset({"-e", "-E"}),
            frozenset({"-F", "-I", "-M", "-m"}),
            frozenset({"-e", "-E"}),
            frozenset({"-F", "-I", "-M", "-m"}),
            frozenset({"e", "E"}),
        ),
        "ruby": _InterpreterGrammar(
            frozenset({"-e"}),
            frozenset({"-C", "-E", "-I", "-r", "--directory", "--encoding"}),
            frozenset({"-e"}),
            frozenset({"-C", "-E", "-I", "-r"}),
            frozenset({"e"}),
        ),
    }
)


class ToolArgs(BaseModel):
    """Closed, strict base schema for a tool's raw arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ListFilesArgs(ToolArgs):
    path: StrictStr = "."


class ReadFileArgs(ToolArgs):
    path: StrictStr


class WriteFileArgs(ToolArgs):
    path: StrictStr
    content: StrictStr


class ShellArgs(ToolArgs):
    argv: list[StrictStr] = Field(min_length=1)
    cwd: StrictStr = "."
    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=1, le=MAX_TIMEOUT_SECONDS)
    stdin: StrictStr | None = None

    @field_validator("env")
    @classmethod
    def validate_environment(cls, value: dict[str, str]) -> dict[str, str]:
        disallowed = sorted(set(value) - ALLOWED_ENV)
        if disallowed:
            raise ValueError(f"environment variables are not allowed: {disallowed}")
        return value

    @field_validator("stdin")
    @classmethod
    def validate_stdin_size(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > MAX_STDIN_BYTES:
            raise ValueError("stdin exceeds the maximum byte length")
        return value


class PytestArgs(ShellArgs):
    """Pytest uses the same structured process contract as shell."""

    @field_validator("argv")
    @classmethod
    def validate_pytest_executable(cls, value: list[str]) -> list[str]:
        executable = value[0].casefold().removesuffix(".exe")
        invokes_pytest_module = (
            re.fullmatch(r"(?:python|pypy)\d*(?:\.\d+)*", executable) is not None
            and len(value) >= 3
            and value[1:3] == ["-m", "pytest"]
        )
        if executable != "pytest" and not invokes_pytest_module:
            raise ValueError("pytest actions must invoke pytest")
        return value


class Tool(Protocol):
    """An executable tool implementation supplied by the runtime layer."""

    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        """Execute one normalized action through its assigned boundary."""


class ToolDispatcher:
    """Route normalized actions to tools without making governance decisions."""

    def __init__(self, tools: Mapping[str, Tool]) -> None:
        self._tools = dict(tools)

    async def dispatch(self, action: NormalizedAction) -> RawExecutionResult:
        if not isinstance(action, NormalizedAction):
            raise TypeError("ToolDispatcher accepts only NormalizedAction values")
        try:
            tool = self._tools[action.type]
        except KeyError as error:
            raise HarnessValidationError(
                f"no tool is registered for {action.type!r}"
            ) from error
        return await tool.execute(action)


class ToolRegistry:
    """Normalize untrusted proposals into closed, workspace-safe action facts."""

    _schemas: Mapping[str, type[ToolArgs]] = {
        "list_files": ListFilesArgs,
        "read_file": ReadFileArgs,
        "write_file": WriteFileArgs,
        "shell": ShellArgs,
        "pytest": PytestArgs,
    }

    @property
    def action_types(self) -> frozenset[str]:
        """The exact set of first-version action types."""

        return frozenset(self._schemas)

    def normalize(
        self, proposal: ActionProposal, workspace_root: Path | str
    ) -> NormalizedAction:
        """Validate and canonicalize a proposal without executing a tool."""

        schema = self._schemas.get(proposal.type)
        if schema is None:
            raise HarnessValidationError(f"unknown tool type: {proposal.type!r}")
        raw_argv = proposal.raw_args.get("argv")
        if isinstance(raw_argv, set | frozenset):
            raise HarnessValidationError("argv must be an ordered sequence")

        try:
            parsed = schema.model_validate(self._thaw(proposal.raw_args))
        except ValidationError as error:
            raise HarnessValidationError(str(error)) from error

        normalized_args = parsed.model_dump(mode="json")
        try:
            root = Path(workspace_root).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise HarnessValidationError(
                "workspace path could not be resolved"
            ) from error
        self._normalize_paths(proposal.type, normalized_args, root)
        if isinstance(parsed, ShellArgs):
            self._reject_interpreter_evaluation(parsed.argv, parsed.stdin)

        return NormalizedAction(
            action_id=proposal.action_id,
            source=proposal.source,
            parent_action_id=proposal.parent_action_id,
            type=proposal.type,
            raw_args=proposal.raw_args,
            normalized_args=normalized_args,
            workspace_id=proposal.workspace_id,
            round=proposal.round,
        )

    @classmethod
    def _thaw(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: cls._thaw(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [cls._thaw(item) for item in value]
        if isinstance(value, frozenset):
            return [cls._thaw(item) for item in value]
        return value

    @classmethod
    def _normalize_paths(
        cls, action_type: str, args: dict[str, Any], root: Path
    ) -> None:
        if action_type in {"list_files", "read_file", "write_file"}:
            args["path"] = cls._normalize_workspace_path(args["path"], root)
        if action_type in {"shell", "pytest"}:
            args["cwd"] = cls._normalize_workspace_path(args["cwd"], root)

    @staticmethod
    def _normalize_workspace_path(value: str, root: Path) -> str:
        if "\\" in value:
            raise HarnessValidationError("workspace paths must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise HarnessValidationError("workspace path escapes are not allowed")

        try:
            resolved = (root / path).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise HarnessValidationError(
                "workspace path could not be resolved"
            ) from error
        try:
            relative = resolved.relative_to(root)
        except ValueError as error:
            raise HarnessValidationError(
                "workspace path escapes are not allowed"
            ) from error
        return relative.as_posix() or "."

    @staticmethod
    def _reject_interpreter_evaluation(argv: list[str], stdin: str | None) -> None:
        program_index = ToolRegistry._interpreter_program_index(argv)
        program = Path(argv[program_index]).name.casefold().removesuffix(".exe")
        interpreter = ToolRegistry._interpreter_name(program)
        grammar = _INTERPRETER_GRAMMARS.get(interpreter)
        if grammar is None:
            return
        arguments = argv[program_index + 1 :]
        if stdin is not None and ToolRegistry._stdin_is_interpreter_source(
            interpreter, arguments
        ):
            raise HarnessValidationError("interpreter stdin is not allowed")
        if (
            interpreter == "powershell"
            and ToolRegistry._has_positional_powershell_command(arguments)
        ):
            raise HarnessValidationError(
                "explicit interpreter evaluation is not allowed"
            )
        startup_arguments = ToolRegistry._interpreter_startup_arguments(
            interpreter, arguments
        )
        if any(
            ToolRegistry._is_evaluation_flag(
                argument,
                grammar,
            )
            for argument in startup_arguments
        ):
            raise HarnessValidationError(
                "explicit interpreter evaluation is not allowed"
            )

    @staticmethod
    def _interpreter_name(program: str) -> str:
        if program == "rbash":
            return "bash"
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*t?)?", program):
            return "python"
        if re.fullmatch(r"pypy(?:\d+(?:\.\d+)*)?", program):
            return "pypy"
        if re.fullmatch(r"node(?:js)?(?:\d+(?:\.\d+)*)?", program):
            return "node"
        if re.fullmatch(r"perl(?:\d+(?:\.\d+)*)?", program):
            return "perl"
        if re.fullmatch(r"ruby(?:\d+(?:\.\d+)*)?", program):
            return "ruby"
        return program

    @staticmethod
    def _stdin_is_interpreter_source(interpreter: str, arguments: list[str]) -> bool:
        grammar = _INTERPRETER_GRAMMARS.get(interpreter)
        if grammar is None or not grammar.reads_stdin_without_script:
            return False

        value_options = grammar.value_options
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            normalized = (
                argument.casefold()
                if interpreter in {"powershell", "pwsh"}
                else argument
            )
            if argument == "--":
                return index + 1 >= len(arguments)
            if interpreter in {"python", "pypy"} and argument == "-m":
                return index + 1 >= len(arguments) or not arguments[index + 1]
            if interpreter in {"powershell", "pwsh"} and normalized in {
                "-file",
                "-f",
            }:
                return index + 1 >= len(arguments) or arguments[index + 1] == "-"
            if (
                argument.startswith("-")
                and not argument.startswith("--")
                and any(
                    letter in argument[1:] for letter in grammar.source_cluster_letters
                )
            ):
                return True
            if argument == "-":
                return True
            if not argument.startswith("-"):
                return False
            index += 2 if normalized in value_options else 1
        return True

    @staticmethod
    def _interpreter_value_options(interpreter: str) -> frozenset[str]:
        grammar = _INTERPRETER_GRAMMARS.get(interpreter)
        return frozenset() if grammar is None else grammar.value_options

    @staticmethod
    def _has_positional_powershell_command(arguments: list[str]) -> bool:
        value_options = ToolRegistry._interpreter_value_options("powershell")
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            normalized = argument.casefold()
            if normalized in {"-file", "-f"}:
                return False
            if argument == "--":
                return index + 1 < len(arguments)
            if not argument.startswith("-"):
                return True
            index += 2 if normalized in value_options else 1
        return False

    @staticmethod
    def _interpreter_startup_arguments(
        interpreter: str, arguments: list[str]
    ) -> list[str]:
        if interpreter == "cmd":
            return arguments

        value_options = ToolRegistry._interpreter_value_options(interpreter)
        startup_arguments: list[str] = []
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            normalized = argument.casefold()
            if argument == "--":
                break
            if interpreter in {"python", "pypy"} and argument == "-m":
                break
            if interpreter in {"powershell", "pwsh"} and normalized in {
                "-file",
                "-f",
            }:
                break
            if argument == "-" or not argument.startswith("-"):
                break
            startup_arguments.append(argument)
            option = normalized if interpreter in {"powershell", "pwsh"} else argument
            index += 2 if option in value_options else 1
        return startup_arguments

    @staticmethod
    def _interpreter_program_index(argv: list[str]) -> int:
        program_index = 0
        for _ in range(8):
            program = Path(argv[program_index]).name.casefold().removesuffix(".exe")
            if program == "env":
                program_index = ToolRegistry._env_command_index(argv, program_index + 1)
                continue
            if program in {"busybox", "busybox.static"}:
                program_index = ToolRegistry._wrapper_command_index(
                    argv, program_index + 1, "busybox"
                )
                continue
            if program == "timeout":
                program_index = ToolRegistry._timeout_command_index(
                    argv, program_index + 1
                )
                continue
            if program == "nice":
                program_index = ToolRegistry._nice_command_index(
                    argv, program_index + 1
                )
                continue
            if program == "stdbuf":
                program_index = ToolRegistry._stdbuf_command_index(
                    argv, program_index + 1
                )
                continue
            return program_index
        raise HarnessValidationError(
            "ambiguous interpreter wrapper nesting is not allowed"
        )

    @staticmethod
    def _wrapper_command_index(argv: list[str], index: int, wrapper: str) -> int:
        if index >= len(argv) or argv[index].startswith("-"):
            raise HarnessValidationError(
                f"ambiguous {wrapper} wrapper invocation is not allowed"
            )
        return index

    @staticmethod
    def _timeout_command_index(argv: list[str], index: int) -> int:
        while index < len(argv):
            argument = argv[index]
            if argument == "--":
                index += 1
                break
            if argument in {"-v", "--foreground", "--preserve-status", "--verbose"}:
                index += 1
                continue
            if argument in {"-k", "--kill-after", "-s", "--signal"}:
                if index + 1 >= len(argv):
                    raise HarnessValidationError(
                        "ambiguous timeout wrapper invocation is not allowed"
                    )
                index += 2
                continue
            if argument.startswith(("--kill-after=", "--signal=")):
                if not argument.partition("=")[2]:
                    raise HarnessValidationError(
                        "ambiguous timeout wrapper invocation is not allowed"
                    )
                index += 1
                continue
            if argument.startswith("-"):
                raise HarnessValidationError(
                    "unsupported timeout option is not allowed"
                )
            break
        if index >= len(argv) or argv[index].startswith("-"):
            raise HarnessValidationError(
                "ambiguous timeout wrapper invocation is not allowed"
            )
        return ToolRegistry._wrapper_command_index(argv, index + 1, "timeout")

    @staticmethod
    def _nice_command_index(argv: list[str], index: int) -> int:
        while index < len(argv):
            argument = argv[index]
            if argument == "--":
                index += 1
                break
            if argument in {"-n", "--adjustment"}:
                if index + 1 >= len(argv):
                    raise HarnessValidationError(
                        "ambiguous nice wrapper invocation is not allowed"
                    )
                index += 2
                continue
            if argument.startswith("--adjustment="):
                if not argument.partition("=")[2]:
                    raise HarnessValidationError(
                        "ambiguous nice wrapper invocation is not allowed"
                    )
                index += 1
                continue
            if re.fullmatch(r"-\d+", argument):
                index += 1
                continue
            if argument.startswith("-"):
                raise HarnessValidationError("unsupported nice option is not allowed")
            break
        return ToolRegistry._wrapper_command_index(argv, index, "nice")

    @staticmethod
    def _stdbuf_command_index(argv: list[str], index: int) -> int:
        while index < len(argv):
            argument = argv[index]
            if argument == "--":
                index += 1
                break
            if argument in {"-i", "--input", "-o", "--output", "-e", "--error"}:
                if index + 1 >= len(argv):
                    raise HarnessValidationError(
                        "ambiguous stdbuf wrapper invocation is not allowed"
                    )
                index += 2
                continue
            if re.fullmatch(r"-[ioe].+", argument) or argument.startswith(
                ("--input=", "--output=", "--error=")
            ):
                if "=" in argument and not argument.partition("=")[2]:
                    raise HarnessValidationError(
                        "ambiguous stdbuf wrapper invocation is not allowed"
                    )
                index += 1
                continue
            if argument.startswith("-"):
                raise HarnessValidationError("unsupported stdbuf option is not allowed")
            break
        return ToolRegistry._wrapper_command_index(argv, index, "stdbuf")

    @staticmethod
    def _env_command_index(argv: list[str], index: int) -> int:
        while index < len(argv):
            argument = argv[index]
            normalized = argument.casefold()
            if normalized in {"-s", "--split-string"} or normalized.startswith(
                "--split-string="
            ):
                raise HarnessValidationError(
                    "explicit interpreter evaluation is not allowed"
                )
            if argument == "--":
                if index + 1 < len(argv):
                    return index + 1
                break
            if "=" in argument and not argument.startswith("-"):
                name = argument.partition("=")[0]
                if name not in ALLOWED_ENV:
                    raise HarnessValidationError(
                        f"environment variable is not allowed: {name!r}"
                    )
                index += 1
                continue
            if normalized in {"-i", "--ignore-environment", "-0", "--null"}:
                index += 1
                continue
            if normalized in {"-u", "--unset", "-c", "--chdir"}:
                index += 2
                continue
            if normalized.startswith(("--unset=", "--chdir=")):
                index += 1
                continue
            if argument.startswith("-"):
                raise HarnessValidationError("unsupported env option is not allowed")
            return index
        raise HarnessValidationError(
            "ambiguous interpreter wrapper invocation is not allowed"
        )

    @staticmethod
    def _is_evaluation_flag(
        argument: str,
        grammar: _InterpreterGrammar,
    ) -> bool:
        normalized = argument if grammar.case_sensitive else argument.casefold()
        if normalized in grammar.evaluation_flags:
            return True
        if any(
            flag.startswith("--") and normalized.startswith(f"{flag}=")
            for flag in grammar.evaluation_flags
        ):
            return True
        if any(
            normalized.startswith(prefix) and len(normalized) > len(prefix)
            for prefix in grammar.attached_value_prefixes
        ):
            return False
        if any(
            normalized.startswith(prefix) and len(normalized) > len(prefix)
            for prefix in grammar.attached_evaluation_prefixes
        ):
            return True
        return (
            normalized.startswith("-")
            and not normalized.startswith("--")
            and any(
                letter in normalized[1:]
                for letter in grammar.evaluation_cluster_letters
            )
        )
