from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from sql_mcp_server import secure_server as server


@pytest.fixture(autouse=True)
def reset_server_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SETTINGS", replace(server.SETTINGS, expose_session_tokens=False, auto_install_drivers=False))
    monkeypatch.setattr(server, "SESSION_STORE", server.SessionStore(24))


def make_context(**permissions: bool) -> server.RequestContext:
    base_permissions = {
        "allow_read": True,
        "allow_insert": False,
        "allow_update": False,
        "allow_delete": False,
        "allow_create": False,
        "allow_drop": False,
    }
    base_permissions.update(permissions)
    return server.RequestContext(
        database_url="sqlite://",
        schema="public",
        statement_timeout_ms=1000,
        max_rows=50,
        permissions=base_permissions,
    )


def test_create_session_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_validate_database_connection", lambda context: {"database_name": "test_db", "current_user": "tester"})

    result = server.create_session(database_url="sqlite://", label="test")

    assert result["ok"] is True
    assert result["session"]["token"]
    assert result["session"]["label"] == "test"
    assert result["database_name"] == "test_db"


def test_session_store_create_get_and_revoke() -> None:
    store = server.SessionStore(1)
    entry = store.create(make_context(), label="demo")

    fetched = store.get(entry.token)
    assert fetched.label == "demo"
    assert store.revoke(entry.token) is True
    with pytest.raises(ValueError):
        store.get(entry.token)


def test_list_sessions_redacts_tokens_by_default() -> None:
    entry = server.SESSION_STORE.create(make_context(), label="demo")

    result = server.list_sessions()

    assert result["count"] == 1
    session_payload = result["sessions"][0]
    assert session_payload["label"] == "demo"
    assert session_payload["token_redacted"] is True
    assert "token" not in session_payload
    assert result["tokens_exposed"] is False
    assert entry.token not in str(result)


def test_list_sessions_can_expose_tokens_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SETTINGS", replace(server.SETTINGS, expose_session_tokens=True))
    entry = server.SESSION_STORE.create(make_context(), label="demo")

    result = server.list_sessions(include_tokens=True)

    assert result["tokens_exposed"] is True
    assert result["sessions"][0]["token"] == entry.token


def test_get_session_info_redacts_token_by_default() -> None:
    entry = server.SESSION_STORE.create(make_context(), label="demo")

    result = server.get_session_info(entry.token)

    assert result["ok"] is True
    assert result["token_exposed"] is False
    assert result["session"]["token_redacted"] is True
    assert "token" not in result["session"]


def test_metadata_requires_allow_read() -> None:
    entry = server.SESSION_STORE.create(make_context(allow_read=False), label="locked")

    result = server.list_tables(session_token=entry.token)

    assert result["ok"] is False
    assert result["error_type"] == "ValueError"
    assert "allow_read" in result["error"]


def test_query_rejects_delete_without_permission() -> None:
    entry = server.SESSION_STORE.create(make_context(allow_delete=False), label="demo")

    result = server.query("delete from users", session_token=entry.token)

    assert result["ok"] is False
    assert result["error_type"] == "ValueError"
    assert "allow_delete" in result["error"]


def test_query_rejects_cross_schema_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = server.SESSION_STORE.create(make_context(allow_read=True), label="demo")
    monkeypatch.setattr(server, "_get_engine", lambda context: SimpleNamespace(name="postgresql"))

    result = server.query("select * from other_schema.users", session_token=entry.token)

    assert result["ok"] is False
    assert result["error_type"] == "ValueError"
    assert "cross-schema" in result["error"]


def test_query_rejects_search_path_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = server.SESSION_STORE.create(make_context(allow_read=True), label="demo")
    monkeypatch.setattr(server, "_get_engine", lambda context: SimpleNamespace(name="postgresql"))

    result = server.query("set search_path to private", session_token=entry.token)

    assert result["ok"] is False
    assert result["error_type"] == "ValueError"
    assert "search_path" in result["error"]


def test_env_bool_parses_and_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_BOOL", "true")
    assert server._env_bool("TEST_BOOL", False) is True

    monkeypatch.setenv("TEST_BOOL", "off")
    assert server._env_bool("TEST_BOOL", True) is False

    monkeypatch.setenv("TEST_BOOL", "maybe")
    with pytest.raises(ValueError):
        server._env_bool("TEST_BOOL", False)
