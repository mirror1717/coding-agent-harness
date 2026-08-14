from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)
from pydantic import ValidationError

from coding_agent_harness.credentials import CredentialStore
from coding_agent_harness.domain import (
    ActionProposal,
    ActionSource,
    ModelDecision,
    StructuredFeedback,
    TaskRequest,
)
from coding_agent_harness.errors import HarnessConfigurationError
from coding_agent_harness.llm import (
    LLM,
    ActionSchemaEntry,
    AgentContext,
    BudgetSummary,
    LLMError,
    LLMExhaustedError,
    MemorySummary,
    MockLLM,
    OpenAICompatibleLLM,
    _WireAction,
    _WireDecision,
)


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def _store_with_key(secret: str = "sk-test-secret") -> CredentialStore:
    store = CredentialStore(_FakeKeyring(), now_utc=lambda: "2026-08-14T08:00:00Z")
    store.set(
        "openai",
        secret,
        endpoint="https://api.example.test/v1",
        model="gpt-test",
    )
    return store


def _context(prompt: str = "fix the failing test") -> AgentContext:
    return AgentContext(task=TaskRequest(prompt=prompt, workspace_id="ws-1"))


def _completion(
    parsed: _WireDecision | None, refusal: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(parsed=parsed, refusal=refusal))
        ]
    )


def _parse_returning(completion: Any) -> Callable[..., Any]:
    async def parse(**kwargs: Any) -> Any:
        return completion

    return parse


def _parse_raising(error: BaseException) -> Callable[..., Any]:
    async def parse(**kwargs: Any) -> Any:
        raise error

    return parse


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.example.test/v1/chat/completions")


def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_request())


# ---------------------------------------------------------------------------
# MockLLM
# ---------------------------------------------------------------------------


async def test_mockllm_replays_exact_sequence() -> None:
    decisions = [
        ModelDecision(
            kind="action",
            action=ActionProposal(type="read_file", raw_args={"path": "a.py"}),
        ),
        ModelDecision(kind="message", message="investigating"),
        ModelDecision(kind="finish"),
    ]
    llm = MockLLM(decisions)
    context = _context()

    first = await llm.decide(context)
    second = await llm.decide(context)
    third = await llm.decide(context)

    assert first == decisions[0]
    assert second == decisions[1]
    assert third == decisions[2]


async def test_mockllm_exhaustion_is_typed() -> None:
    llm = MockLLM([ModelDecision(kind="finish")])
    context = _context()

    await llm.decide(context)

    with pytest.raises(LLMExhaustedError) as first_exhausted:
        await llm.decide(context)
    with pytest.raises(LLMExhaustedError) as second_exhausted:
        await llm.decide(context)

    assert first_exhausted.value.code == second_exhausted.value.code == "LLM_EXHAUSTED"


async def test_mockllm_does_not_touch_network_or_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = MockLLM([ModelDecision(kind="finish")])

    decision = await llm.decide(_context())

    assert decision.kind == "finish"


# ---------------------------------------------------------------------------
# OpenAICompatibleLLM structured output mapping
# ---------------------------------------------------------------------------


async def test_provider_maps_structured_decision() -> None:
    secret = "sk-mapping-secret"
    store = _store_with_key(secret)
    wire = _WireDecision(
        kind="action",
        action=_WireAction(
            type="write_file",
            raw_args={"path": "fix.py", "content": "fixed"},
        ),
    )
    adapter = OpenAICompatibleLLM(
        credentials=store,
        provider="openai",
        model="gpt-test",
        parse_fn=_parse_returning(_completion(wire)),
    )

    decision = await adapter.decide(_context())

    assert decision.kind == "action"
    assert decision.action is not None
    assert decision.action.type == "write_file"
    assert decision.action.raw_args["path"] == "fix.py"
    assert decision.action.raw_args["content"] == "fixed"
    assert decision.action.source == ActionSource.MODEL


@pytest.mark.parametrize(
    ("wire", "expected_kind"),
    [
        (_WireDecision(kind="finish"), "finish"),
        (_WireDecision(kind="message", message="status update"), "message"),
    ],
)
async def test_provider_maps_finish_and_message(
    wire: _WireDecision, expected_kind: str
) -> None:
    store = _store_with_key()
    adapter = OpenAICompatibleLLM(
        credentials=store,
        provider="openai",
        model="gpt-test",
        parse_fn=_parse_returning(_completion(wire)),
    )

    decision = await adapter.decide(_context())

    assert decision.kind == expected_kind
    if expected_kind == "message":
        assert decision.message == "status update"


async def test_provider_refusal_is_protocol_error() -> None:
    store = _store_with_key()
    adapter = OpenAICompatibleLLM(
        credentials=store,
        provider="openai",
        model="gpt-test",
        parse_fn=_parse_returning(_completion(parsed=None, refusal="cannot help")),
    )

    with pytest.raises(LLMError) as error:
        await adapter.decide(_context())

    assert error.value.code == "LLM_PROTOCOL_ERROR"
    assert "cannot help" in error.value.message


async def test_provider_missing_parsed_output_is_protocol_error() -> None:
    store = _store_with_key()
    adapter = OpenAICompatibleLLM(
        credentials=store,
        provider="openai",
        model="gpt-test",
        parse_fn=_parse_returning(_completion(parsed=None)),
    )

    with pytest.raises(LLMError) as error:
        await adapter.decide(_context())

    assert error.value.code == "LLM_PROTOCOL_ERROR"


async def test_provider_invalid_shape_is_protocol_error() -> None:
    store = _store_with_key()
    wire = _WireDecision(kind="action", action=None)
    adapter = OpenAICompatibleLLM(
        credentials=store,
        provider="openai",
        model="gpt-test",
        parse_fn=_parse_returning(_completion(wire)),
    )

    with pytest.raises(LLMError) as error:
        await adapter.decide(_context())

    assert error.value.code == "LLM_PROTOCOL_ERROR"


# ---------------------------------------------------------------------------
# OpenAICompatibleLLM error classification + redaction
# ---------------------------------------------------------------------------


async def test_provider_error_is_redacted() -> None:
    secret = "sk-super-secret-key"
    store = _store_with_key(secret)
    error = RuntimeError(
        f"request failed; authorization header Bearer {secret} rejected"
    )
    adapter = OpenAICompatibleLLM(
        credentials=store,
        provider="openai",
        model="gpt-test",
        parse_fn=_parse_raising(error),
    )

    with pytest.raises(LLMError) as raised:
        await adapter.decide(_context())

    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert "[REDACTED]" in raised.value.message


@pytest.mark.parametrize(
    ("make_error", "expected_code"),
    [
        (
            lambda: AuthenticationError(
                "invalid key sk-test-secret",
                response=_response(401),
                body=None,
            ),
            "LLM_AUTH_ERROR",
        ),
        (
            lambda: RateLimitError(
                "rate limited sk-test-secret",
                response=_response(429),
                body=None,
            ),
            "LLM_TRANSIENT_ERROR",
        ),
        (
            lambda: APITimeoutError(request=_request()),
            "LLM_TRANSIENT_ERROR",
        ),
        (
            lambda: APIConnectionError(
                message="connection lost sk-test-secret",
                request=_request(),
            ),
            "LLM_TRANSIENT_ERROR",
        ),
        (
            lambda: BadRequestError(
                "bad request sk-test-secret",
                response=_response(400),
                body=None,
            ),
            "LLM_PROTOCOL_ERROR",
        ),
        (
            lambda: RuntimeError("unknown failure sk-test-secret"),
            "LLM_ERROR",
        ),
    ],
)
async def test_provider_classifies_error_types(
    make_error: Callable[[], BaseException], expected_code: str
) -> None:
    secret = "sk-test-secret"
    store = _store_with_key(secret)
    adapter = OpenAICompatibleLLM(
        credentials=store,
        provider="openai",
        model="gpt-test",
        parse_fn=_parse_raising(make_error()),
    )

    with pytest.raises(LLMError) as raised:
        await adapter.decide(_context())

    assert raised.value.code == expected_code
    assert secret not in str(raised.value)


async def test_provider_without_configured_key_raises_configuration_error() -> None:
    store = CredentialStore(_FakeKeyring(), now_utc=lambda: "unused")
    adapter = OpenAICompatibleLLM(
        credentials=store,
        provider="openai",
        model="gpt-test",
        parse_fn=_parse_returning(_completion(_WireDecision(kind="finish"))),
    )

    with pytest.raises(HarnessConfigurationError):
        await adapter.decide(_context())


# ---------------------------------------------------------------------------
# LLM Protocol (AC-2: LLM replaceability)
# ---------------------------------------------------------------------------


def test_mock_and_provider_implement_llm_protocol() -> None:
    mock = MockLLM([ModelDecision(kind="finish")])
    provider = OpenAICompatibleLLM(
        credentials=_store_with_key(),
        provider="openai",
        model="gpt-test",
        parse_fn=_parse_returning(_completion(_WireDecision(kind="finish"))),
    )

    assert isinstance(mock, LLM)
    assert isinstance(provider, LLM)


# ---------------------------------------------------------------------------
# AgentContext boundaries
# ---------------------------------------------------------------------------


def test_agent_context_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentContext.model_validate(
            {"task": TaskRequest(), "docker_socket": "unix:///var/run/docker.sock"}
        )


def test_agent_context_is_frozen() -> None:
    context = _context()

    with pytest.raises(ValidationError, match="Instance is frozen"):
        context.round = 99  # type: ignore[misc]


def test_agent_context_carries_bounded_summary_fields() -> None:
    context = AgentContext(
        task=TaskRequest(prompt="task"),
        memory=(
            MemorySummary(kind="convention", content="use src layout"),
            MemorySummary(kind="failure", content="import error"),
        ),
        feedback=(StructuredFeedback(category="pytest", summary="1 failed"),),
        action_schema=(ActionSchemaEntry(tool="write_file"),),
        budget=BudgetSummary(rounds_remaining=3, llm_calls_remaining=10),
        round=2,
    )

    assert len(context.memory) == 2
    assert context.feedback[0].category == "pytest"
    assert context.action_schema[0].tool == "write_file"
    assert context.budget is not None
    assert context.budget.rounds_remaining == 3
    assert context.round == 2


def test_agent_context_has_no_tool_or_credential_access() -> None:
    fields = set(AgentContext.model_fields)

    assert "credentials" not in fields
    assert "credential_store" not in fields
    assert "tools" not in fields
    assert "tool_registry" not in fields
    assert "sandbox" not in fields
    assert "docker" not in fields
    assert "filesystem" not in fields
    assert "api_key" not in fields
