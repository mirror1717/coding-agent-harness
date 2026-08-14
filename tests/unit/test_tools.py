from pathlib import Path

import pytest

from coding_agent_harness.domain import (
    ActionProposal,
    NormalizedAction,
    RawExecutionResult,
)
from coding_agent_harness.errors import HarnessValidationError
from coding_agent_harness.tools import (
    ALLOWED_ENV,
    MAX_STDIN_BYTES,
    MAX_TIMEOUT_SECONDS,
    ToolDispatcher,
    ToolRegistry,
)


def proposal(action_type: str, raw_args: dict[str, object]) -> ActionProposal:
    return ActionProposal(
        action_id="action-1",
        type=action_type,
        raw_args=raw_args,
        workspace_id="workspace-1",
        round=2,
    )


def test_normalize_shell_preserves_canonical_argv_boundaries(tmp_path: Path) -> None:
    action = ToolRegistry().normalize(
        proposal(
            "shell",
            {
                "argv": ["python", "-m", "pytest", "tests/unit/test tools.py"],
                "cwd": "./tests",
                "env": {"PYTEST_ADDOPTS": "-q"},
                "timeout_seconds": 12,
                "stdin": "input\n",
            },
        ),
        tmp_path,
    )

    assert action.type == "shell"
    assert action.action_id == "action-1"
    assert dict(action.normalized_args) == {
        "argv": ("python", "-m", "pytest", "tests/unit/test tools.py"),
        "cwd": "tests",
        "env": {"PYTEST_ADDOPTS": "-q"},
        "timeout_seconds": 12,
        "stdin": "input\n",
    }


@pytest.mark.parametrize(
    ("raw_args", "expected_message"),
    [
        ({"argv": []}, "argv"),
        ({"command": "pytest -q"}, "Extra inputs"),
        ({"argv": ["bash", "-c", "pytest -q"]}, "interpreter"),
        ({"argv": ["bash", "-lc", "echo bypass"]}, "interpreter"),
        ({"argv": ["sh", "-xc", "echo bypass"]}, "interpreter"),
        ({"argv": ["env", "bash", "-c", "pytest -q"]}, "interpreter"),
        ({"argv": ["busybox", "sh", "-c", "pytest -q"]}, "interpreter"),
        ({"argv": ["env", "-S", "bash -c echo bypass"]}, "interpreter"),
        (
            {"argv": ["env", "--split-string", "bash -c echo bypass"]},
            "interpreter",
        ),
        (
            {"argv": ["busybox", "env", "bash", "-c", "echo bypass"]},
            "interpreter",
        ),
        (
            {"argv": ["env", "busybox", "sh", "-c", "echo bypass"]},
            "interpreter",
        ),
        ({"argv": ["bash", "--command=pytest -q"]}, "interpreter"),
        ({"argv": ["python", "-c", "print('unsafe')"]}, "interpreter"),
        ({"argv": ["python3", "-cpass"]}, "interpreter"),
        ({"argv": ["python3", "-Ic", "print('unsafe')"]}, "interpreter"),
        ({"argv": ["python3.12", "-c", "print('unsafe')"]}, "interpreter"),
        ({"argv": ["perl", "-eprint 1"]}, "interpreter"),
        ({"argv": ["env", "python3", "-cpass"]}, "interpreter"),
        ({"argv": ["busybox", "perl", "-eprint 1"]}, "interpreter"),
        ({"argv": ["busybox", "ash", "-c", "echo bypass"]}, "interpreter"),
        ({"argv": ["pwsh", "-Command", "pytest -q"]}, "interpreter"),
        ({"argv": ["env", "-C", ".", "bash", "-c", "true"]}, "interpreter"),
        (
            {"argv": ["env", "--chdir", ".", "bash", "-c", "true"]},
            "interpreter",
        ),
        ({"argv": ["sh", "-s"], "stdin": "echo bypass"}, "interpreter"),
        ({"argv": ["bash"], "stdin": "echo bypass"}, "interpreter"),
        ({"argv": ["pytest"], "env": {"HOME": "/tmp"}}, "environment"),
        (
            {"argv": ["pytest"], "stdin": "x" * (MAX_STDIN_BYTES + 1)},
            "stdin",
        ),
        (
            {"argv": ["pytest"], "timeout_seconds": MAX_TIMEOUT_SECONDS + 1},
            "timeout_seconds",
        ),
    ],
)
def test_normalize_shell_rejects_invalid_structured_arguments(
    tmp_path: Path, raw_args: dict[str, object], expected_message: str
) -> None:
    with pytest.raises(HarnessValidationError, match=expected_message):
        ToolRegistry().normalize(proposal("shell", raw_args), tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["python"],
        ["python", "-"],
        ["pypy3"],
        ["node"],
        ["perl"],
        ["ruby"],
        ["env", "python3.12"],
    ],
)
def test_normalize_shell_rejects_source_interpreters_reading_code_from_stdin(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter stdin"):
        ToolRegistry().normalize(
            proposal("shell", {"argv": argv, "stdin": "print('unsafe')"}),
            tmp_path,
        )


@pytest.mark.parametrize(
    ("action_type", "argv"),
    [
        ("pytest", ["python", "-m", "pytest", "-c", "pyproject.toml"]),
        ("shell", ["python", "script.py", "-c", "data"]),
        ("shell", ["bash", "script.sh", "-c", "data"]),
        ("shell", ["node", "script.js", "--eval", "data"]),
        ("shell", ["python", "--", "script.py", "-c", "data"]),
        ("shell", ["bash", "--", "script.sh", "-c", "data"]),
        ("shell", ["node", "--", "script.js", "--eval", "data"]),
        ("shell", ["perl", "script.pl", "-e", "data"]),
        ("shell", ["ruby", "script.rb", "-e", "data"]),
        ("shell", ["pwsh", "-File", "script.ps1", "-Command", "data"]),
        (
            "shell",
            [
                "pwsh",
                "-WorkingDirectory",
                ".",
                "-File",
                "script.ps1",
                "-Command",
                "data",
            ],
        ),
    ],
)
def test_normalize_allows_eval_like_data_after_interpreter_startup_boundary(
    tmp_path: Path, action_type: str, argv: list[str]
) -> None:
    action = ToolRegistry().normalize(
        proposal(action_type, {"argv": argv}),
        tmp_path,
    )

    assert tuple(action.normalized_args["argv"]) == tuple(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-W", "ignore", "-c", "print('unsafe')"],
        ["bash", "-O", "extglob", "-c", "echo unsafe"],
        ["node", "--require", "module", "--eval", "unsafe"],
        ["perl", "-I", "lib", "-e", "print 'unsafe'"],
        ["ruby", "-I", "lib", "-e", "puts 'unsafe'"],
        ["pwsh", "-InputFormat", "Text", "-Command", "unsafe"],
    ],
)
def test_startup_option_values_do_not_hide_interpreter_evaluation(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter"):
        ToolRegistry().normalize(proposal("shell", {"argv": argv}), tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["nodejs", "-e", "console.log('unsafe')"],
        ["perl5.30.0", "-e", "print 'unsafe'"],
        ["python3.13t", "-c", "print('unsafe')"],
        ["busybox.static", "sh", "-c", "echo unsafe"],
    ],
)
def test_normalize_shell_rejects_interpreter_aliases_and_versioned_names(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter"):
        ToolRegistry().normalize(proposal("shell", {"argv": argv}), tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["timeout", "5", "bash", "-c", "echo unsafe"],
        ["timeout", "5", "/bin/bash", "-c", "echo unsafe"],
        ["nice", "-n", "5", "bash", "-c", "echo unsafe"],
        ["stdbuf", "-o0", "bash", "-c", "echo unsafe"],
    ],
)
def test_normalize_shell_rejects_interpreter_evaluation_behind_wrappers(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter"):
        ToolRegistry().normalize(proposal("shell", {"argv": argv}), tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["printf", "fixtures/bash", "-c"],
        ["cp", "--", "bash", "-c"],
        ["cp", "--", "fixtures/bash", "-c"],
    ],
)
def test_normalize_shell_does_not_treat_ordinary_arguments_as_interpreters(
    tmp_path: Path, argv: list[str]
) -> None:
    action = ToolRegistry().normalize(
        proposal("shell", {"argv": argv}),
        tmp_path,
    )

    assert tuple(action.normalized_args["argv"]) == tuple(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["timeout", "--unknown", "5", "echo"],
        ["timeout", "--signal"],
        ["nice", "--unknown", "echo"],
        ["nice", "-n"],
        ["stdbuf", "--unknown", "echo"],
        ["stdbuf", "-o"],
        ["busybox", "--unknown"],
    ],
)
def test_normalize_shell_rejects_unknown_or_ambiguous_wrapper_options(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(HarnessValidationError):
        ToolRegistry().normalize(proposal("shell", {"argv": argv}), tmp_path)


def test_normalize_shell_rejects_restricted_bash_command_strings(
    tmp_path: Path,
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter"):
        ToolRegistry().normalize(
            proposal("shell", {"argv": ["rbash", "-c", "echo unsafe"]}),
            tmp_path,
        )


@pytest.mark.parametrize("print_flag", ["-p", "--print"])
def test_normalize_shell_rejects_node_print_evaluation(
    tmp_path: Path, print_flag: str
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter"):
        ToolRegistry().normalize(
            proposal("shell", {"argv": ["node", print_flag, "process.version"]}),
            tmp_path,
        )


@pytest.mark.parametrize("executable", ["pwsh", "powershell"])
def test_normalize_shell_rejects_powershell_file_from_stdin(
    tmp_path: Path, executable: str
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter stdin"):
        ToolRegistry().normalize(
            proposal(
                "shell",
                {
                    "argv": [executable, "-File", "-"],
                    "stdin": "Write-Output unsafe",
                },
            ),
            tmp_path,
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["pwsh", "-e", "unsafe"],
        ["powershell", "-EC", "unsafe"],
        ["pwsh", "-cwa", "Write-Output unsafe"],
        ["powershell", "-CommandWithArgs", "Write-Output unsafe"],
        ["fish", "-C", "echo unsafe"],
        ["fish", "--init-command", "echo unsafe"],
        ["fish", "--init-command=echo unsafe"],
    ],
)
def test_normalize_shell_rejects_additional_explicit_evaluation_forms(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter"):
        ToolRegistry().normalize(proposal("shell", {"argv": argv}), tmp_path)


@pytest.mark.parametrize("executable", ["powershell", "powershell.exe"])
def test_normalize_shell_rejects_windows_powershell_positional_command(
    tmp_path: Path, executable: str
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter"):
        ToolRegistry().normalize(
            proposal("shell", {"argv": [executable, "Get-Date"]}),
            tmp_path,
        )


@pytest.mark.parametrize("executable", ["pwsh", "pwsh.exe"])
def test_normalize_shell_allows_pwsh_positional_script(
    tmp_path: Path, executable: str
) -> None:
    action = ToolRegistry().normalize(
        proposal("shell", {"argv": [executable, "script.ps1"]}),
        tmp_path,
    )

    assert tuple(action.normalized_args["argv"]) == (executable, "script.ps1")


@pytest.mark.parametrize("executable", ["cmd", "cmd.exe"])
@pytest.mark.parametrize("command_flag", ["/C", "/K"])
def test_normalize_shell_rejects_uppercase_cmd_evaluation_flags(
    tmp_path: Path, executable: str, command_flag: str
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter"):
        ToolRegistry().normalize(
            proposal("shell", {"argv": [executable, command_flag, "echo unsafe"]}),
            tmp_path,
        )


@pytest.mark.parametrize("executable", ["pwsh", "powershell"])
def test_normalize_shell_rejects_powershell_short_file_from_stdin(
    tmp_path: Path, executable: str
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter stdin"):
        ToolRegistry().normalize(
            proposal(
                "shell",
                {
                    "argv": [executable, "-f", "-"],
                    "stdin": "Write-Output unsafe",
                },
            ),
            tmp_path,
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "script.sh"],
        ["python", "-m", "worker"],
        ["python", "--", "script.py"],
        ["node", "--", "script.js"],
        ["perl", "--", "script.pl"],
        ["ruby", "--", "script.rb"],
    ],
)
def test_normalize_shell_allows_data_stdin_after_script_boundary(
    tmp_path: Path, argv: list[str]
) -> None:
    action = ToolRegistry().normalize(
        proposal("shell", {"argv": argv, "stdin": "ordinary data"}),
        tmp_path,
    )

    assert tuple(action.normalized_args["argv"]) == tuple(argv)
    assert action.normalized_args["stdin"] == "ordinary data"


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "--"],
        ["python", "--"],
        ["node", "--"],
        ["perl", "--"],
        ["ruby", "--"],
    ],
)
def test_normalize_shell_rejects_stdin_without_script_after_boundary(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter stdin"):
        ToolRegistry().normalize(
            proposal("shell", {"argv": argv, "stdin": "unsafe code"}),
            tmp_path,
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-C", "script.sh"],
        ["ruby", "-E", "UTF-8", "script.rb"],
    ],
)
def test_normalize_shell_treats_unix_interpreter_flags_as_case_sensitive(
    tmp_path: Path, argv: list[str]
) -> None:
    action = ToolRegistry().normalize(proposal("shell", {"argv": argv}), tmp_path)

    assert tuple(action.normalized_args["argv"]) == tuple(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["perl", "-E", "say 'unsafe'"],
        ["perl", "-E42"],
        ["perl", "-eprint 'unsafe'"],
        ["perl", "-weprint 'unsafe'"],
    ],
)
def test_normalize_shell_rejects_perl_explicit_eval_and_attached_forms(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(HarnessValidationError, match="interpreter"):
        ToolRegistry().normalize(proposal("shell", {"argv": argv}), tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-Wignore::ResourceWarning", "script.py"],
        ["python", "-Xtracemalloc", "script.py"],
        ["ruby", "-E", "UTF-8", "script.rb"],
    ],
)
def test_normalize_shell_allows_interpreter_specific_value_options(
    tmp_path: Path, argv: list[str]
) -> None:
    action = ToolRegistry().normalize(proposal("shell", {"argv": argv}), tmp_path)

    assert tuple(action.normalized_args["argv"]) == tuple(argv)


def test_env_wrapper_assignments_use_the_environment_allowlist(tmp_path: Path) -> None:
    allowed = ToolRegistry().normalize(
        proposal(
            "shell",
            {
                "argv": [
                    "env",
                    "PYTHONUNBUFFERED=1",
                    "python",
                    "script.py",
                ]
            },
        ),
        tmp_path,
    )

    assert tuple(allowed.normalized_args["argv"]) == (
        "env",
        "PYTHONUNBUFFERED=1",
        "python",
        "script.py",
    )
    for assignment in ("BASH_ENV=payload.sh", "HOME=/tmp"):
        with pytest.raises(HarnessValidationError, match="environment"):
            ToolRegistry().normalize(
                proposal(
                    "shell",
                    {"argv": ["env", assignment, "bash", "script.sh"]},
                ),
                tmp_path,
            )


@pytest.mark.parametrize(
    ("action_type", "raw_args", "expected_args"),
    [
        ("list_files", {}, {"path": "."}),
        ("read_file", {"path": "./notes.txt"}, {"path": "notes.txt"}),
        (
            "write_file",
            {"path": "./notes.txt", "content": "hello"},
            {"path": "notes.txt", "content": "hello"},
        ),
        (
            "pytest",
            {"argv": ["pytest", "-q"], "cwd": "."},
            {
                "argv": ("pytest", "-q"),
                "cwd": ".",
                "env": {},
                "timeout_seconds": 30,
                "stdin": None,
            },
        ),
    ],
)
def test_normalize_registered_tool_types(
    tmp_path: Path,
    action_type: str,
    raw_args: dict[str, object],
    expected_args: dict[str, object],
) -> None:
    action = ToolRegistry().normalize(proposal(action_type, raw_args), tmp_path)

    assert dict(action.normalized_args) == expected_args


@pytest.mark.parametrize(
    ("action_type", "raw_args"),
    [
        ("read_file", {"path": "../secret.txt"}),
        ("read_file", {"path": "/etc/passwd"}),
        ("write_file", {"path": "dir/../secret.txt", "content": "no"}),
    ],
)
def test_normalize_rejects_workspace_path_escapes(
    tmp_path: Path, action_type: str, raw_args: dict[str, object]
) -> None:
    with pytest.raises(HarnessValidationError, match="workspace"):
        ToolRegistry().normalize(proposal(action_type, raw_args), tmp_path)


def test_normalize_maps_nul_path_errors_to_harness_validation_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(HarnessValidationError, match="workspace"):
        ToolRegistry().normalize(
            proposal("read_file", {"path": "bad\x00path"}), tmp_path
        )


def test_normalize_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "escaped.txt").symlink_to(outside)

    with pytest.raises(HarnessValidationError, match="workspace"):
        ToolRegistry().normalize(
            proposal("read_file", {"path": "escaped.txt"}), tmp_path
        )


@pytest.mark.parametrize(
    ("action_type", "raw_args"),
    [
        ("unknown", {}),
        ("read_file", {"path": "file.txt", "unexpected": True}),
    ],
)
def test_normalize_rejects_unknown_tools_and_extra_fields(
    tmp_path: Path, action_type: str, raw_args: dict[str, object]
) -> None:
    with pytest.raises(HarnessValidationError):
        ToolRegistry().normalize(proposal(action_type, raw_args), tmp_path)


def test_normalize_pytest_rejects_a_non_pytest_executable(tmp_path: Path) -> None:
    with pytest.raises(HarnessValidationError, match="pytest"):
        ToolRegistry().normalize(
            proposal("pytest", {"argv": ["git", "status"]}), tmp_path
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["./pytest", "-q"],
        ["tools/pytest", "-q"],
        ["./python", "-m", "pytest"],
    ],
)
def test_normalize_pytest_rejects_workspace_executable_masquerades(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(HarnessValidationError, match="pytest"):
        ToolRegistry().normalize(proposal("pytest", {"argv": argv}), tmp_path)


def test_environment_allowlist_is_nonempty() -> None:
    assert ALLOWED_ENV


@pytest.mark.parametrize(
    "argv",
    [
        {"pytest", "-q"},
        frozenset({"pytest", "-q"}),
        ["pytest", 1],
        "pytest -q",
    ],
)
def test_normalize_rejects_unordered_or_coerced_argv_before_hash_seed_order_can_matter(
    tmp_path: Path, argv: object
) -> None:
    with pytest.raises(HarnessValidationError, match="argv"):
        ToolRegistry().normalize(proposal("shell", {"argv": argv}), tmp_path)


def test_registry_registers_exactly_the_first_version_tool_types() -> None:
    assert ToolRegistry().action_types == {
        "list_files",
        "read_file",
        "write_file",
        "shell",
        "pytest",
    }


class RecordingTool:
    async def execute(self, action: NormalizedAction) -> RawExecutionResult:
        return RawExecutionResult(action_id=action.action_id, outcome="recorded")


@pytest.mark.asyncio
async def test_dispatcher_routes_only_normalized_actions_to_registered_tool() -> None:
    dispatcher = ToolDispatcher({"shell": RecordingTool()})
    action = NormalizedAction(action_id="action-1", type="shell")

    result = await dispatcher.dispatch(action)

    assert result.action_id == "action-1"
    assert result.outcome == "recorded"
    with pytest.raises(TypeError, match="NormalizedAction"):
        await dispatcher.dispatch(ActionProposal(type="shell"))  # type: ignore[arg-type]
