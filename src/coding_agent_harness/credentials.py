"""Host-only credential storage backed by an injected system keyring."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from coding_agent_harness.errors import (
    HarnessConfigurationError,
    HarnessValidationError,
)

_PROVIDER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PROFILE_USERNAME = "profile"


@runtime_checkable
class KeyringBackend(Protocol):
    """Small subset of the keyring backend API used by the harness."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


@runtime_checkable
class SecretDetector(Protocol):
    """Hard-boundary adapter consumed by Guardrail through injection."""

    def contains_secret(self, value: str | bytes) -> bool: ...


class CredentialStatus(BaseModel):
    """Non-sensitive provider configuration state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    endpoint: str | None = None
    model: str | None = None
    configured: bool
    updated_at: str | None = None


class _CredentialRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    secret: str
    endpoint: str | None = None
    model: str | None = None
    updated_at: str


class ExactSecretDetector:
    """Detect containment of explicitly known secret literals only."""

    def __init__(self, secrets: Iterable[str]) -> None:
        self._text_literals = tuple(secret for secret in secrets if secret)
        self._byte_literals = tuple(secret.encode("utf-8") for secret in self._text_literals)

    def contains_secret(self, value: str | bytes) -> bool:
        if isinstance(value, bytes):
            return any(secret in value for secret in self._byte_literals)
        return any(secret in value for secret in self._text_literals)


class EmptySecretDetector:
    """Detector for a host with no configured credentials."""

    def contains_secret(self, value: str | bytes) -> bool:
        return False


class CredentialStore:
    """Manage provider credentials without exposing plaintext status data."""

    def __init__(self, backend: KeyringBackend, *, now_utc: Callable[[], str]) -> None:
        self._backend = backend
        self._now_utc = now_utc

    def set(
        self,
        provider: str,
        secret: str,
        *,
        endpoint: str | None = None,
        model: str | None = None,
    ) -> CredentialStatus:
        provider = _validate_provider(provider)
        _validate_secret(secret)
        record = _CredentialRecord(
            secret=secret,
            endpoint=endpoint,
            model=model,
            updated_at=self._now_utc(),
        )
        self._set_record(provider, record)
        return _to_status(provider, record, configured=True)

    def update(
        self,
        provider: str,
        secret: str,
        *,
        endpoint: str | None = None,
        model: str | None = None,
    ) -> CredentialStatus:
        provider = _validate_provider(provider)
        _validate_secret(secret)
        current = self._read_record(provider)
        if current is None:
            raise HarnessConfigurationError(
                f"Credential for provider '{provider}' is not configured"
            )
        record = _CredentialRecord(
            secret=secret,
            endpoint=current.endpoint if endpoint is None else endpoint,
            model=current.model if model is None else model,
            updated_at=self._now_utc(),
        )
        self._set_record(provider, record)
        return _to_status(provider, record, configured=True)

    def status(self, provider: str) -> CredentialStatus:
        provider = _validate_provider(provider)
        record = self._read_record(provider)
        if record is None:
            return CredentialStatus(provider=provider, configured=False)
        return _to_status(provider, record, configured=True)

    def get_for_client(self, provider: str) -> str:
        provider = _validate_provider(provider)
        record = self._read_record(provider)
        if record is None:
            raise HarnessConfigurationError(
                f"Credential for provider '{provider}' is not configured"
            )
        return record.secret

    def clear(self, provider: str) -> CredentialStatus:
        provider = _validate_provider(provider)
        service = _service_name(provider)
        with _translate_backend_errors(provider):
            encoded_record = self._backend.get_password(service, _PROFILE_USERNAME)
            if encoded_record is not None:
                self._backend.delete_password(service, _PROFILE_USERNAME)
            remaining = self._backend.get_password(service, _PROFILE_USERNAME)
        if remaining is not None:
            raise HarnessConfigurationError(
                f"Credential for provider '{provider}' could not be cleared"
            )
        return CredentialStatus(provider=provider, configured=False)

    def build_secret_detector(
        self, providers: Iterable[str]
    ) -> ExactSecretDetector | EmptySecretDetector:
        secrets: list[str] = []
        for provider in providers:
            provider = _validate_provider(provider)
            record = self._read_record(provider)
            if record is not None:
                secrets.append(record.secret)
        if not secrets:
            return EmptySecretDetector()
        return ExactSecretDetector(secrets)

    def _set_record(self, provider: str, record: _CredentialRecord) -> None:
        with _translate_backend_errors(provider):
            self._backend.set_password(
                _service_name(provider),
                _PROFILE_USERNAME,
                record.model_dump_json(),
            )

    def _read_record(self, provider: str) -> _CredentialRecord | None:
        with _translate_backend_errors(provider):
            encoded_record = self._backend.get_password(
                _service_name(provider), _PROFILE_USERNAME
            )
        return _decode_record(provider, encoded_record)


def _validate_provider(provider: str) -> str:
    if not _PROVIDER_PATTERN.fullmatch(provider):
        raise HarnessValidationError("Provider must be a non-empty safe identifier")
    return provider


def _validate_secret(secret: str) -> None:
    if not secret.strip():
        raise HarnessValidationError("Credential secret must not be empty")


def _service_name(provider: str) -> str:
    return f"coding-agent-harness/{provider}"


def _decode_record(
    provider: str, encoded_record: str | None
) -> _CredentialRecord | None:
    if encoded_record is None:
        return None
    try:
        return _CredentialRecord.model_validate_json(encoded_record)
    except ValidationError:
        raise HarnessConfigurationError(
            f"Credential record for provider '{provider}' is invalid"
        ) from None


def _to_status(
    provider: str, record: _CredentialRecord, *, configured: bool
) -> CredentialStatus:
    return CredentialStatus(
        provider=provider,
        endpoint=record.endpoint,
        model=record.model,
        configured=configured,
        updated_at=record.updated_at,
    )


def _backend_error(provider: str) -> HarnessConfigurationError:
    return HarnessConfigurationError(
        f"Credential backend unavailable or locked for provider '{provider}'"
    )


@contextmanager
def _translate_backend_errors(provider: str) -> Iterator[None]:
    try:
        yield
    except Exception:  # noqa: BLE001 - injected backends have no common error base.
        raise _backend_error(provider) from None
