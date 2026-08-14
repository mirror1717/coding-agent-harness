"""Unified LLM protocol with a deterministic mock and an OpenAI-compatible adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coding_agent_harness.credentials import CredentialStore
from coding_agent_harness.domain import (
    ActionProposal,
    FactModel,
    ModelDecision,
    StructuredFeedback,
    TaskRequest,
)
from coding_agent_harness.errors import HarnessError

_REDACTED = "[REDACTED]"


class LLMError(HarnessError):
    """A typed failure raised by an LLM decision call."""


class LLMExhaustedError(LLMError):
    """Raised when a MockLLM script has no more decisions to replay."""

    ERROR_CODE: ClassVar[str] = "LLM_EXHAUSTED"

    def __init__(self, message: str = "MockLLM script exhausted") -> None:
        super().__init__(self.ERROR_CODE, message)


class MemorySummary(FactModel):
    """A bounded, model-visible summary of a single memory entry."""

    kind: str = ""
    content: str = ""
    tags: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    updated_at: str = ""


class ActionSchemaEntry(FactModel):
    """A bounded, model-visible description of one available action."""

    tool: str = ""
    description: str = ""
    required_args: tuple[str, ...] = ()
    optional_args: tuple[str, ...] = ()


class BudgetSummary(FactModel):
    """A bounded, model-visible view of remaining runtime budgets."""

    rounds_remaining: int = 0
    llm_calls_remaining: int = 0
    tool_calls_remaining: int = 0
    wall_clock_remaining_seconds: float = 0.0
    sandbox_execution_remaining_seconds: float = 0.0
    hitl_wait_remaining_seconds: float = 0.0
    exhausted: tuple[str, ...] = ()


class AgentContext(FactModel):
    """Bounded context passed to an LLM; never exposes tools, Docker, or secrets."""

    task: TaskRequest = Field(default_factory=TaskRequest)
    memory: tuple[MemorySummary, ...] = ()
    feedback: tuple[StructuredFeedback, ...] = ()
    action_schema: tuple[ActionSchemaEntry, ...] = ()
    budget: BudgetSummary | None = None
    round: int = 0


@runtime_checkable
class LLM(Protocol):
    """The decision surface owned by the harness; the model cannot execute."""

    async def decide(self, context: AgentContext) -> ModelDecision: ...


class _WireAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    raw_args: dict[str, Any] = Field(default_factory=dict)


class _WireDecision(BaseModel):
    """Structured output schema requested from the OpenAI-compatible API."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["action", "finish", "message"]
    action: _WireAction | None = None
    message: str | None = None


class MockLLM:
    """Deterministic, script-driven LLM that never touches the network or keyring."""

    def __init__(self, script: Sequence[ModelDecision]) -> None:
        self._script: tuple[ModelDecision, ...] = tuple(script)
        self._index = 0

    async def decide(self, context: AgentContext) -> ModelDecision:
        if self._index >= len(self._script):
            raise LLMExhaustedError()
        decision = self._script[self._index]
        self._index += 1
        return decision


class OpenAICompatibleLLM:
    """Adapter that reads its key from CredentialStore and maps structured output."""

    def __init__(
        self,
        *,
        credentials: CredentialStore,
        provider: str,
        model: str,
        endpoint: str | None = None,
        parse_fn: Callable[..., Any] | None = None,
    ) -> None:
        self._credentials = credentials
        self._provider = provider
        self._model = model
        self._endpoint = endpoint
        self._parse_fn = parse_fn

    async def decide(self, context: AgentContext) -> ModelDecision:
        secret = self._credentials.get_for_client(self._provider)
        parse = self._parse_fn or _build_openai_parse(self._endpoint, secret)
        try:
            completion = await parse(
                messages=_build_messages(context),
                model=self._model,
                response_format=_WireDecision,
            )
        except Exception as error:  # noqa: BLE001 - provider exceptions share no common base.
            raise _classify_and_redact(error, secret) from None
        return _map_completion(completion)


def _build_openai_parse(
    endpoint: str | None, secret: str
) -> Callable[..., Any]:
    """Construct the default structured-output callable backed by the OpenAI SDK."""

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=secret, base_url=endpoint)

    async def parse(*, messages: Any, model: str, response_format: Any) -> Any:
        return await client.beta.chat.completions.parse(
            messages=messages,
            model=model,
            response_format=response_format,
        )

    return parse


def _build_messages(context: AgentContext) -> list[Mapping[str, str]]:
    """Render a bounded, secret-free prompt for the structured-output call."""

    task = context.task
    schema_lines = [
        f"- {entry.tool}: {entry.description}".strip()
        for entry in context.action_schema
    ]
    memory_lines = [
        f"- [{m.kind}] {m.content}" for m in context.memory
    ]
    feedback_lines = [
        f"- [{f.category}] {f.summary}" for f in context.feedback
    ]
    budget = context.budget
    budget_line = ""
    if budget is not None:
        budget_line = (
            f"rounds_remaining={budget.rounds_remaining} "
            f"llm_calls_remaining={budget.llm_calls_remaining} "
            f"tool_calls_remaining={budget.tool_calls_remaining}"
        )
    system = (
        f"Task: {task.prompt}\n"
        f"Workspace: {task.workspace_id}\n"
        f"Round: {context.round}\n"
        + (f"Budget: {budget_line}\n" if budget_line else "")
        + ("Available actions:\n" + "\n".join(schema_lines) + "\n"
            if schema_lines
            else "")
        + ("Memory:\n" + "\n".join(memory_lines) + "\n"
            if memory_lines
            else "")
        + ("Recent feedback:\n" + "\n".join(feedback_lines)
            if feedback_lines
            else "")
    )
    return [{"role": "system", "content": system}]


def _map_completion(completion: Any) -> ModelDecision:
    """Map the structured-output response into a validated ModelDecision."""

    message = completion.choices[0].message
    refusal = getattr(message, "refusal", None)
    if refusal:
        raise LLMError("LLM_PROTOCOL_ERROR", f"model refused: {refusal}")
    parsed = getattr(message, "parsed", None)
    if not isinstance(parsed, _WireDecision):
        raise LLMError("LLM_PROTOCOL_ERROR", "missing structured decision output")
    action = None
    if parsed.action is not None:
        action = ActionProposal(
            type=parsed.action.type,
            raw_args=parsed.action.raw_args,
        )
    try:
        return ModelDecision(kind=parsed.kind, action=action, message=parsed.message)
    except ValidationError:
        raise LLMError("LLM_PROTOCOL_ERROR", "invalid decision shape") from None


def _classify_and_redact(error: BaseException, secret: str | None) -> LLMError:
    """Classify a provider exception and redact the exact secret literal."""

    message = _redact(str(error), secret)
    code = _classify(error)
    return LLMError(code, message)


def _classify(error: BaseException) -> str:
    """Map a provider exception to a stable error code without retrying."""

    from openai import (
        APIConnectionError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        RateLimitError,
    )

    if isinstance(error, AuthenticationError):
        return "LLM_AUTH_ERROR"
    if isinstance(
        error, APITimeoutError | APIConnectionError | RateLimitError
    ):
        return "LLM_TRANSIENT_ERROR"
    if isinstance(error, BadRequestError):
        return "LLM_PROTOCOL_ERROR"
    return "LLM_ERROR"


def _redact(text: str, secret: str | None) -> str:
    """Replace the exact secret literal with a stable placeholder."""

    if secret:
        return text.replace(secret, _REDACTED)
    return text
