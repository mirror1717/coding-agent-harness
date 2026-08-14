from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from coding_agent_harness.credentials import (
    CredentialStore,
    EmptySecretDetector,
    ExactSecretDetector,
)
from coding_agent_harness.errors import (
    HarnessConfigurationError,
    HarnessValidationError,
)


@dataclass
class FakeKeyring:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    locked: bool = False
    ignore_delete_usernames: set[str] = field(default_factory=set)
    fail_next_set: bool = False
    lock_after_set_failure: bool = False
    fail_next_delete: bool = False
    set_calls: int = 0
    delete_calls: int = 0

    def get_password(self, service: str, username: str) -> str | None:
        self._check_available()
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._check_available()
        self.set_calls += 1
        if self.fail_next_set:
            self.fail_next_set = False
            if self.lock_after_set_failure:
                self.locked = True
            raise RuntimeError(f"set failed while handling {password}")
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._check_available()
        self.delete_calls += 1
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("delete failed while handling known-secret")
        if (service, username) not in self.values:
            raise KeyError(username)
        if username in self.ignore_delete_usernames:
            return
        del self.values[(service, username)]

    def _check_available(self) -> None:
        if self.locked:
            raise RuntimeError("backend locked while handling sk-secret-value")


def test_fake_keyring_lifecycle_exposes_only_non_sensitive_status() -> None:
    backend = FakeKeyring()
    store = CredentialStore(backend, now_utc=lambda: "2026-08-14T08:00:00Z")

    created = store.set(
        "openai",
        "sk-first-secret",
        endpoint="https://api.example.test/v1",
        model="gpt-test",
    )

    assert created.model_dump() == {
        "provider": "openai",
        "endpoint": "https://api.example.test/v1",
        "model": "gpt-test",
        "configured": True,
        "updated_at": "2026-08-14T08:00:00Z",
    }
    assert store.get_for_client("openai") == "sk-first-secret"
    assert "sk-first-secret" not in repr(created)
    assert list(backend.values) == [("coding-agent-harness/openai", "profile")]
    assert backend.set_calls == 1

    updated = store.update("openai", "sk-second-secret", model="gpt-new")
    assert updated.endpoint == "https://api.example.test/v1"
    assert updated.model == "gpt-new"
    assert store.get_for_client("openai") == "sk-second-secret"
    assert backend.set_calls == 2

    cleared = store.clear("openai")
    assert cleared.model_dump() == {
        "provider": "openai",
        "endpoint": None,
        "model": None,
        "configured": False,
        "updated_at": None,
    }
    assert store.status("openai") == cleared
    assert backend.delete_calls == 1
    with pytest.raises(HarnessConfigurationError, match="not configured"):
        store.get_for_client("openai")


def test_status_for_unknown_provider_is_unconfigured() -> None:
    store = CredentialStore(FakeKeyring(), now_utc=lambda: "unused")

    status = store.status("openai")

    assert status.model_dump() == {
        "provider": "openai",
        "endpoint": None,
        "model": None,
        "configured": False,
        "updated_at": None,
    }


def test_backend_locked_fails_closed_without_leaking_secret() -> None:
    backend = FakeKeyring(locked=True)
    store = CredentialStore(backend, now_utc=lambda: "2026-08-14T08:00:00Z")

    with pytest.raises(HarnessConfigurationError) as error:
        store.set("openai", "sk-secret-value")

    assert error.value.code == "CONFIGURATION_ERROR"
    assert "sk-secret-value" not in str(error.value)
    assert "sk-secret-value" not in repr(error.value)


def test_store_never_creates_plaintext_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store = CredentialStore(FakeKeyring(locked=True), now_utc=lambda: "unused")

    with pytest.raises(HarnessConfigurationError):
        store.set("openai", "literal-that-must-not-be-written")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("literal", ["known-secret", "另一个秘密"])
def test_secret_detector_matches_each_known_literal_as_text_and_bytes(
    literal: str,
) -> None:
    detector = ExactSecretDetector(("known-secret", "另一个秘密"))

    assert detector.contains_secret(f"prefix-{literal}-suffix") is True
    assert detector.contains_secret(f"prefix-{literal}-suffix".encode()) is True
    assert detector.contains_secret("known-secre") is False
    assert detector.contains_secret("sk-unrecognized-token-shape") is False


def test_empty_detector_allows_nonempty_values() -> None:
    detector = EmptySecretDetector()

    assert detector.contains_secret("ordinary env value") is False
    assert detector.contains_secret(b"ordinary stdin") is False


def test_detector_built_from_store_tracks_update_and_clear() -> None:
    store = CredentialStore(FakeKeyring(), now_utc=lambda: "2026-08-14T08:00:00Z")
    store.set("openai", "old-exact-secret")
    old_detector = store.build_secret_detector(("openai",))

    store.update("openai", "new-exact-secret")
    new_detector = store.build_secret_detector(("openai",))
    store.clear("openai")
    empty_detector = store.build_secret_detector(("openai",))

    assert old_detector.contains_secret("old-exact-secret") is True
    assert old_detector.contains_secret("new-exact-secret") is False
    assert new_detector.contains_secret("old-exact-secret") is False
    assert new_detector.contains_secret("new-exact-secret") is True
    assert empty_detector.contains_secret("new-exact-secret") is False


def test_detector_fails_closed_when_configured_secret_cannot_be_read() -> None:
    backend = FakeKeyring()
    store = CredentialStore(backend, now_utc=lambda: "2026-08-14T08:00:00Z")
    store.set("openai", "known-secret")
    backend.locked = True

    with pytest.raises(HarnessConfigurationError, match="unavailable or locked"):
        store.build_secret_detector(("openai",))


def test_clear_fails_closed_when_backend_does_not_remove_record() -> None:
    backend = FakeKeyring()
    store = CredentialStore(backend, now_utc=lambda: "2026-08-14T08:00:00Z")
    store.set("openai", "known-secret")
    backend.ignore_delete_usernames.add("profile")

    with pytest.raises(HarnessConfigurationError, match="could not be cleared"):
        store.clear("openai")


@pytest.mark.parametrize("preexisting", [False, True])
def test_failed_atomic_set_preserves_complete_old_record(preexisting: bool) -> None:
    backend = FakeKeyring()
    store = CredentialStore(backend, now_utc=lambda: "2026-08-14T08:00:00Z")
    if preexisting:
        store.set("openai", "old-secret", endpoint="old-endpoint", model="old-model")
    before = dict(backend.values)
    backend.fail_next_set = True

    operation = store.update if preexisting else store.set
    with pytest.raises(HarnessConfigurationError) as error:
        operation(
            "openai",
            "new-secret",
            endpoint="new-endpoint",
            model="new-model",
        )

    assert backend.values == before
    assert "new-secret" not in str(error.value)
    assert "old-secret" not in str(error.value)


def test_failed_atomic_update_after_lock_keeps_old_record_when_unlocked() -> None:
    backend = FakeKeyring()
    store = CredentialStore(backend, now_utc=lambda: "2026-08-14T08:00:00Z")
    store.set("openai", "old-secret", endpoint="old-endpoint")
    before = dict(backend.values)
    backend.fail_next_set = True
    backend.lock_after_set_failure = True

    with pytest.raises(HarnessConfigurationError) as error:
        store.update("openai", "new-secret", endpoint="new-endpoint")

    assert error.value.code == "CONFIGURATION_ERROR"
    assert "old-secret" not in str(error.value)
    assert "new-secret" not in str(error.value)
    backend.locked = False
    assert backend.values == before
    assert store.get_for_client("openai") == "old-secret"
    assert store.status("openai").endpoint == "old-endpoint"


def test_failed_clear_keeps_complete_record_and_status_after_unlock() -> None:
    backend = FakeKeyring()
    store = CredentialStore(backend, now_utc=lambda: "2026-08-14T08:00:00Z")
    store.set("openai", "known-secret", endpoint="endpoint", model="model")
    before = dict(backend.values)
    backend.fail_next_delete = True

    with pytest.raises(HarnessConfigurationError) as error:
        store.clear("openai")

    assert "known-secret" not in str(error.value)
    assert backend.values == before
    assert store.status("openai").model_dump() == {
        "provider": "openai",
        "endpoint": "endpoint",
        "model": "model",
        "configured": True,
        "updated_at": "2026-08-14T08:00:00Z",
    }


@pytest.mark.parametrize(
    "encoded",
    [
        "not-json",
        '{"version":2,"secret":"secret","updated_at":"time"}',
        '{"version":1,"secret":"secret","updated_at":"time","extra":true}',
    ],
)
def test_invalid_or_non_strict_profile_record_fails_closed(encoded: str) -> None:
    backend = FakeKeyring(
        values={("coding-agent-harness/openai", "profile"): encoded}
    )
    store = CredentialStore(backend, now_utc=lambda: "unused")

    with pytest.raises(HarnessConfigurationError, match="invalid"):
        store.status("openai")
    with pytest.raises(HarnessConfigurationError, match="invalid"):
        store.get_for_client("openai")
    with pytest.raises(HarnessConfigurationError, match="invalid"):
        store.build_secret_detector(("openai",))


@pytest.mark.parametrize("secret", ["", "   "])
def test_empty_secret_is_rejected(secret: str) -> None:
    store = CredentialStore(FakeKeyring(), now_utc=lambda: "unused")

    with pytest.raises(HarnessValidationError):
        store.set("openai", secret)
