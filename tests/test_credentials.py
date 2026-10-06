import sqlite3
from dataclasses import asdict

import pytest
from cryptography.fernet import Fernet

from app.credentials import (
    ConnectionMetadata,
    CredentialConfigurationError,
    CredentialDecryptionError,
    CredentialDisconnectedError,
    CredentialStore,
)


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode()


def test_requires_deployment_key_and_rejects_malformed_key(tmp_path):
    path = tmp_path / "credentials.db"
    with pytest.raises(CredentialConfigurationError, match="not configured"):
        CredentialStore(path, None)
    with pytest.raises(CredentialConfigurationError, match="invalid") as error:
        CredentialStore(path, "not-a-fernet-key")
    assert "not-a-fernet-key" not in str(error.value)


def test_plaintext_absent_from_database_and_safe_metadata(tmp_path, key):
    path = tmp_path / "credentials.db"
    store = CredentialStore(path, key)
    token = "x-access-token-very-secret-99281"
    metadata = store.put(
        "X", "demo-brand", "Demo Brand on X", {"access_token": token, "refresh_token": "also-secret"},
        required_scopes=["tweet.read", "users.read"],
        granted_scopes=["users.read", "tweet.read"],
    )

    assert metadata.status == "connected"
    assert metadata.scope_status == "least_privilege"
    assert metadata.has_credentials
    safe = repr(metadata) + repr(metadata.as_dict()) + repr(store.list()) + repr(store.get("x", "demo-brand"))
    assert token not in safe
    assert "encrypted_payload" not in metadata.as_dict()
    assert "access_token" not in safe
    assert repr(store.secret("x", "demo-brand")) == "SecretPayload(<redacted>)"
    assert store.secret("x", "demo-brand").reveal()["access_token"] == token

    with sqlite3.connect(path) as connection:
        encrypted = connection.execute("SELECT encrypted_payload FROM connector_credentials").fetchone()[0]
    assert token.encode() not in encrypted
    assert token.encode() not in path.read_bytes()


def test_wrong_key_fails_closed_without_secret_or_ciphertext(tmp_path, key):
    path = tmp_path / "credentials.db"
    store = CredentialStore(path, key)
    token = "beehiiv-secret-token"
    store.put("beehiiv", "pub_123", "Newsletter", {"token": token})

    wrong_store = CredentialStore(path, Fernet.generate_key())
    with pytest.raises(CredentialDecryptionError, match="cannot be decrypted") as error:
        wrong_store.secret("beehiiv", "pub_123")
    assert token not in repr(error.value)
    assert "gAAAA" not in repr(error.value)


def test_update_and_master_key_rotation(tmp_path, key):
    path = tmp_path / "credentials.db"
    old_store = CredentialStore(path, key)
    first = old_store.put("x", "pm", "X", {"token": "one"})
    second = old_store.put("x", "pm", "X", {"token": "two"})
    assert (first.credential_revision, second.credential_revision) == (1, 2)
    assert old_store.secret("x", "pm").reveal() == {"token": "two"}

    new_key = Fernet.generate_key()
    assert old_store.rotate_master_key(new_key, actor="operator") == 1
    assert old_store.secret("x", "pm").reveal() == {"token": "two"}
    with pytest.raises(CredentialDecryptionError):
        CredentialStore(path, key).secret("x", "pm")
    assert CredentialStore(path, new_key).secret("x", "pm").reveal() == {"token": "two"}
    assert [event.action for event in old_store.audit("x", "pm")] == [
        "connected", "credentials_updated", "master_key_rotated"
    ]


def test_scope_health_reconnect_disconnect_delete_and_audit(tmp_path, key):
    path = tmp_path / "credentials.db"
    store = CredentialStore(path, key)
    metadata = store.put(
        "beehiiv", "publication", "Newsletter", {"token": "secret"},
        required_scopes=["posts.read", "subscribers.read"],
        granted_scopes=["posts.read", "admin"],
        actor="chris",
    )
    assert metadata.missing_scopes == ("subscribers.read",)
    assert metadata.excessive_scopes == ("admin",)
    assert metadata.scope_status == "insufficient"

    unhealthy = store.set_health(
        "beehiiv", "publication", "reconnect_required", error_code="oauth.token_expired"
    )
    assert unhealthy.reconnect_required
    assert unhealthy.last_error_code == "oauth.token_expired"
    with pytest.raises(ValueError, match="machine-readable"):
        store.set_health("beehiiv", "publication", "unhealthy", error_code="Bearer secret")

    disconnected = store.disconnect("beehiiv", "publication", actor="chris")
    assert disconnected.status == "disconnected"
    assert disconnected.has_credentials is False
    assert disconnected.credential_revision == 0
    with pytest.raises(CredentialDisconnectedError):
        store.secret("beehiiv", "publication")

    store.delete("beehiiv", "publication", actor="chris")
    with pytest.raises(KeyError, match="unknown connector account"):
        store.get("beehiiv", "publication")
    assert [event.action for event in store.audit("beehiiv", "publication")] == [
        "connected", "health_reconnect_required", "disconnected", "deleted"
    ]


def test_schema_initialization_is_repeatable_and_models_have_no_secret_fields(tmp_path, key):
    path = tmp_path / "credentials.db"
    CredentialStore(path, key)
    store = CredentialStore(path, key)
    store.put("x", "one", "One", {"token": "hidden"})
    assert [row.provider for row in store.list("X")] == ["x"]
    assert "encrypted_payload" not in asdict(store.get("x", "one"))
    assert "token" not in ConnectionMetadata.__dataclass_fields__
