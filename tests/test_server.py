"""Server contracts on a synthetic, owner-selected namespace."""

from __future__ import annotations

from pathlib import Path
import os
from typing import Any

import httpx2
from mcp import Client
import pytest

from angelbot_memory_bridge.server import (
    BearerAuthMiddleware,
    create_http_app,
    create_server,
    require_loopback,
    validate_http_token,
)
from angelbot_memory_bridge.__main__ import default_db_path
from angelbot_memory_bridge.store import MemoryBridgeError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_tool_schemas_bind_namespace_and_preserve_evidence(tmp_path: Path) -> None:
    server = create_server(tmp_path / "synthetic.sqlite3", "project-a")
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "memory_append", "memory_read_event", "memory_write", "memory_get",
            "memory_revoke", "memory_search", "memory_list", "memory_context", "memory_save_communication_preference",
            "memory_shape_request",
        }
        for tool in tools:
            assert "workspace" not in tool.input_schema.get("properties", {})
            assert tool.annotations is not None
            assert tool.annotations.open_world_hint is False
        by_name = {tool.name: tool for tool in tools}
        shape = by_name["memory_shape_request"]
        assert shape.input_schema["required"] == ["wish"]
        assert set(shape.input_schema["properties"]) == {"wish", "task_id", "clarification", "lookup_terms"}
        assert shape.annotations is not None and shape.annotations.read_only_hint is True
        assert shape.annotations.destructive_hint is False and shape.annotations.idempotent_hint is True
        assert "memory_shape_request" in (by_name["memory_context"].description or "")

        original = {"speaker": "user", "text": "Keep raw evidence; 压缩视图可以重建。"}
        appended = await client.call_tool("memory_append", {
            "kind": "user_decision", "content": original, "idempotency_key": "source-1",
        })
        assert not appended.is_error
        source = appended.structured_content
        assert source is not None and source["advisory"] is True
        event_id = source["event_id"]

        saved = await client.call_tool("memory_write", {
            "key": "raw-evidence", "kind": "constraint", "content": "Do not replace original evidence with summaries.",
            "evidence_ids": [event_id], "idempotency_key": "record-1", "priority": 100,
        })
        assert not saved.is_error
        record = saved.structured_content
        assert record is not None and record["version"] == 1
        assert record["authority"] == "memory_is_not_execution_permission"

        # An unrelated query still receives every applicable active constraint.
        recalled = await client.call_tool("memory_context", {"query": "tea preferences"})
        assert not recalled.is_error
        context = recalled.structured_content
        assert context is not None
        assert context["coverage"]["complete_constraints"] is True
        assert [item["record_id"] for item in context["constraints"]] == [record["record_id"]]

        read = await client.call_tool("memory_read_event", {"event_id": event_id})
        assert read.structured_content is not None
        assert read.structured_content["content"] == original

        invalid = await client.call_tool("memory_write", {
            "key": "invalid", "kind": "constraint", "content": "Unproven",
            "evidence_ids": ["foreign-event"], "idempotency_key": "invalid-1",
        })
        assert invalid.is_error
        unchanged = await client.call_tool("memory_list", {})
        assert unchanged.structured_content is not None
        assert unchanged.structured_content["total"] == 1


@pytest.mark.anyio
async def test_shape_request_uses_current_scoped_memory_without_saving_the_conversation(tmp_path: Path) -> None:
    async with Client(create_server(tmp_path / "synthetic.sqlite3", "settings")) as client:
        async def call(name: str, **arguments: Any) -> dict[str, Any]:
            result = await client.call_tool(name, arguments)
            assert not result.is_error and result.structured_content is not None
            return result.structured_content

        source = await call("memory_append", kind="user_statement", content="The editor and settings have different defaults.",
                            idempotency_key="existing-evidence")
        evidence_ids = [source["event_id"]]
        decisions = [
            ("editor-scroll", "Restore scroll position after save.", "task", "editor"),
            ("project-scroll", "Reset scroll position on navigation.", "project", None),
            ("sidebar-scroll", "Scroll to the new sidebar row.", "task", "sidebar"),
        ]
        for key, content, scope, scope_id in decisions:
            await call("memory_write", key=key, kind="decision", content=content, scope=scope, scope_id=scope_id,
                       evidence_ids=evidence_ids, idempotency_key=key)
        habits = [
            ("project-style", "Use a short paragraph.", "project", None),
            ("editor-style", "Show a before-and-after example.", "task", "editor"),
            ("sidebar-style", "List every toggle.", "task", "sidebar"),
        ]
        for key, preference, scope, scope_id in habits:
            await call("memory_save_communication_preference", key=key, preference=preference, scope=scope,
                       scope_id=scope_id, evidence_ids=evidence_ids, idempotency_key=key)
        before = await call("memory_list")

        wish = "I keep getting lost after I change something here."
        shaped = await call("memory_shape_request", wish=wish, task_id="editor")
        assert [item["key"] for item in shaped["records"]] == ["editor-scroll", "project-scroll"]
        assert [item["key"] for item in shaped["communication_preferences"]] == ["editor-style", "project-style"]
        assert "current request" in shaped["guidance"] and "one easy question" in shaped["guidance"]

        clarification = "Only this time, jump to the edited section to show the result."
        follow_up = await call("memory_shape_request", wish=wish, task_id="editor", clarification=clarification,
                               lookup_terms="scroll position")
        assert {item["key"] for item in follow_up["records"]} == {"editor-scroll", "project-scroll"}
        assert follow_up["task_id"] == "editor"
        assert follow_up["advisory"] is True and follow_up["authority"] == "memory_is_not_execution_permission"
        assert wish not in str(follow_up) and clarification not in str(follow_up)
        assert await call("memory_list") == before


@pytest.mark.anyio
async def test_shape_request_accepts_punctuation_and_multiline_text_beyond_search_limit(tmp_path: Path) -> None:
    async with Client(create_server(tmp_path / "synthetic.sqlite3", "photos")) as client:
        source = await client.call_tool("memory_append", {
            "kind": "user_statement", "content": "Keep the lightbox open after saving a crop.",
            "idempotency_key": "lightbox-evidence",
        })
        assert source.structured_content is not None
        saved = await client.call_tool("memory_write", {
            "key": "lightbox-save", "kind": "decision", "content": "Keep the lightbox open after saving a crop.",
            "evidence_ids": [source.structured_content["event_id"]], "idempotency_key": "lightbox-save",
        })
        assert not saved.is_error

        wish = "I wish browsing were easier.\n" + "Photo editor feedback.\r\n" * 100
        shaped = await client.call_tool("memory_shape_request", {
            "wish": wish, "clarification": "The lightbox!\nSpecifically after I crop something.",
        })
        assert not shaped.is_error and shaped.structured_content is not None
        assert [item["key"] for item in shaped.structured_content["records"]] == ["lightbox-save"]
        punctuation = await client.call_tool("memory_shape_request", {"wish": "！？\n🙂", "lookup_terms": "..."})
        assert not punctuation.is_error
        for invalid in ({}, {"wish": ""}, {"wish": " \r\n\t"}, {"wish": "x" * 8001}):
            assert (await client.call_tool("memory_shape_request", invalid)).is_error


@pytest.mark.anyio
async def test_tool_cannot_override_bound_workspace(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.sqlite3"
    first = create_server(db_path, "first")
    async with Client(first) as client:
        appended = await client.call_tool("memory_append", {
            "kind": "note", "content": "private first namespace", "idempotency_key": "first-source",
        })
        assert appended.structured_content is not None
        event_id = appended.structured_content["event_id"]
    async with Client(create_server(db_path, "second")) as other:
        unavailable = await other.call_tool("memory_read_event", {"event_id": event_id})
        assert unavailable.is_error
        listing = await other.call_tool("memory_list", {})
        assert listing.structured_content is not None and listing.structured_content["total"] == 0


def test_http_configuration_fails_closed(tmp_path: Path) -> None:
    server = create_server(tmp_path / "synthetic.sqlite3", "test")
    for token in (None, "short", "x" * 31, "x" * 32 + "\n"):
        with pytest.raises(ValueError, match="MEMORY_BRIDGE_TOKEN"):
            validate_http_token(token)
    for host in ("0.0.0.0", "192.0.2.1", "example.com"):
        with pytest.raises(ValueError, match="loopback"):
            create_http_app(server, "x" * 32, host)
    for host in ("127.0.0.1", "::1", "localhost"):
        require_loopback(host)


def test_default_database_uses_dedicated_application_data_without_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    application_data = tmp_path / "synthetic-app-data"
    variable = "LOCALAPPDATA" if os.name == "nt" else "XDG_DATA_HOME"
    monkeypatch.setenv(variable, str(application_data))
    expected_name = "AngelBotMemoryBridge" if os.name == "nt" else "angelbot-memory-bridge"
    assert default_db_path() == application_data / expected_name / "memory.sqlite3"
    assert not application_data.exists()
    monkeypatch.setenv(variable, "relative-app-data")
    assert default_db_path().is_absolute()


def test_invalid_workspace_is_rejected_before_creating_storage(tmp_path: Path) -> None:
    database = tmp_path / "must-not-be-created" / "memory.sqlite3"
    for workspace in ("", " alpha ", "alpha\n", "\ud800", "x" * 129):
        with pytest.raises(MemoryBridgeError):
            create_server(database, workspace)
        assert not database.parent.exists()


@pytest.mark.anyio
async def test_protocol_rejects_boolean_and_string_numbers_without_updating_record(tmp_path: Path) -> None:
    async with Client(create_server(tmp_path / "synthetic.sqlite3", "typed-contract")) as client:
        source = await client.call_tool("memory_append", {
            "kind": "note", "content": "version must remain an integer", "idempotency_key": "typed-source",
        })
        assert source.structured_content is not None
        evidence_ids = [source.structured_content["event_id"]]
        initial = await client.call_tool("memory_write", {
            "key": "stable", "kind": "decision", "content": "original", "evidence_ids": evidence_ids,
            "idempotency_key": "typed-initial",
        })
        assert initial.structured_content is not None
        record_id = initial.structured_content["record_id"]
        for wrong_version in (True, "1"):
            update = await client.call_tool("memory_write", {
                "key": "stable", "kind": "decision", "content": "must never be saved", "evidence_ids": evidence_ids,
                "expected_version": wrong_version, "idempotency_key": f"typed-wrong-{wrong_version}",
            })
            assert update.is_error
        cases = [
            ("memory_write", {"key": "stable", "kind": "decision", "content": "wrong priority", "evidence_ids": evidence_ids,
                              "expected_version": 1, "priority": True, "idempotency_key": "wrong-priority"}),
            ("memory_get", {"record_id": record_id, "version": "1"}),
            ("memory_revoke", {"record_id": record_id, "expected_version": True, "reason": "must not revoke",
                               "evidence_ids": evidence_ids, "idempotency_key": "wrong-revoke"}),
            ("memory_list", {"limit": True}),
            ("memory_list", {"offset": "0"}),
            ("memory_list", {"expected_revision": "2"}),
            ("memory_search", {"query": "version", "limit": "1"}),
            ("memory_context", {"max_chars": "1000"}),
        ]
        for name, arguments in cases:
            rejected = await client.call_tool(name, arguments)
            assert rejected.is_error, (name, arguments)
        current = await client.call_tool("memory_get", {"record_id": record_id})
        assert current.structured_content is not None
        assert current.structured_content["version"] == 1
        assert current.structured_content["content"] == "original"
        assert current.structured_content["status"] == "active"


@pytest.mark.anyio
async def test_http_gate_authenticates_every_method_and_lifespan_is_passthrough() -> None:
    visited: list[str] = []

    async def endpoint(scope: dict[str, Any], receive: Any, send: Any) -> None:
        visited.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    token = "synthetic-http-token-" + "x" * 32
    app = BearerAuthMiddleware(endpoint, token)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://127.0.0.1") as client:
        for method in ("GET", "POST", "DELETE", "OPTIONS"):
            absent = await client.request(method, "/mcp")
            invalid = await client.request(method, "/mcp", headers={"Authorization": "Bearer wrong"})
            assert absent.status_code == invalid.status_code == 401
            assert absent.headers["www-authenticate"].startswith("Bearer")
            assert token not in absent.text and token not in invalid.text
        assert visited == []
        accepted = await client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert accepted.status_code == 204
    assert visited == ["http"]
    await app({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert visited[-1] == "lifespan"
