from pathlib import Path

import pytest
from mcp import Client

from angelbot_memory_bridge.server import create_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def call(client: Client, name: str, **arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error and result.structured_content is not None, result.content
    return result.structured_content


@pytest.mark.anyio
async def test_correction_by_recalled_id_preserves_identity_scope_and_source(tmp_path: Path) -> None:
    async with Client(create_server(tmp_path / "memory.sqlite3", "weekend")) as client:
        original = await call(client, "memory_append", kind="user_statement", idempotency_key="source",
                              content="Usually make oolong tea. For the picnic, make jasmine tea.")
        common = {"kind": "preference", "evidence_ids": [original["event_id"]]}
        general = await call(client, "memory_write", key="everyday-drink", content="Make oolong tea.",
                             scope="global", idempotency_key="usual", **common)
        first = await call(client, "memory_write", key="picnic-afternoon-drink", content="Make jasmine tea.",
                           scope="task", scope_id="picnic", priority=71, expires_at="2099-01-01T00:00:00Z",
                           idempotency_key="picnic", **common)
        await call(client, "memory_write", key="meeting-drink", content="Make black tea.",
                   scope="task", scope_id="meeting", idempotency_key="meeting", **common)

        candidates = await call(client, "memory_search", query="tea", task_id="picnic")
        assert {item["record_id"] for item in candidates["items"]} == {first["record_id"], general["record_id"]}
        recalled = await call(client, "memory_get", record_id=first["record_id"])
        evidence = await call(client, "memory_read_event", event_id=recalled["evidence_ids"][0])
        assert evidence["content"] == original["content"]
        # The clarification identifies the picnic preference in ordinary words.
        raw_correction = "I meant the picnic. For our picnics, make mint tea instead of jasmine."
        correction = await call(client, "memory_append", kind="user_correction", idempotency_key="clarified",
                                content=raw_correction)
        revised = {"content": "Make mint tea.", "evidence_ids": [correction["event_id"]]}
        duplicate = await client.call_tool("memory_write", {**revised, "key": "picnic-mint-tea", "kind": "preference",
                                                              "idempotency_key": "wrong-key"})
        assert duplicate.is_error
        correct = {**revised, "record_id": recalled["record_id"], "expected_version": recalled["version"],
                   "idempotency_key": "correct-picnic"}
        assert (await client.call_tool("memory_write", {**correct, "scope": "global"})).is_error
        second = await call(client, "memory_write", **correct)
        for field in ("record_id", "key", "kind", "scope", "scope_id", "priority", "expires_at", "created_at"):
            assert second[field] == first[field]
        assert second["version"] == 2
        assert second["evidence_ids"] == [correction["event_id"]]
        assert await call(client, "memory_write", **correct) == second
        stale = await client.call_tool("memory_write", {**correct, "idempotency_key": "stale-correction"})
        assert stale.is_error

        old = await call(client, "memory_get", record_id=first["record_id"], version=1)
        assert old["content"] == "Make jasmine tea." and not old["is_current"]
        assert old["evidence_ids"] == [original["event_id"]]
        current = await call(client, "memory_get", record_id=first["record_id"])
        assert current["content"] == "Make mint tea." and current["version"] == 2
        assert (await call(client, "memory_read_event", event_id=current["evidence_ids"][0]))["content"] == raw_correction
        active = await call(client, "memory_search", query="tea", task_id="picnic")
        assert {item["content"] for item in active["items"]} == {"Make mint tea.", "Make oolong tea."}
        visible = await call(client, "memory_list", kind="preference", task_id="picnic")
        assert visible["total"] == 2 and visible["revision"] == second["revision"]


@pytest.mark.anyio
async def test_correction_rejects_inactive_records_and_evidence_outside_its_workspace(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    async with Client(create_server(database, "separate")) as other:
        foreign = await call(other, "memory_append", kind="user_correction", idempotency_key="foreign-source",
                             content="Use detailed notes instead.")
    async with Client(create_server(database, "bound")) as client:
        original = await call(client, "memory_append", kind="user_statement", idempotency_key="source",
                              content="Use terse notes.")
        first = await call(client, "memory_write", key="note-length", kind="preference", content="Use terse notes.",
                           evidence_ids=[original["event_id"]], idempotency_key="record")
        update = {"record_id": first["record_id"], "expected_version": 1, "content": "Use detailed notes.",
                  "idempotency_key": "update"}
        for source in (original, foreign):
            result = await client.call_tool("memory_write", {**update, "evidence_ids": [source["event_id"]]})
            assert result.is_error
        correction = await call(client, "memory_append", kind="user_correction", idempotency_key="correction-source",
                                content="On second thought, use detailed notes.")
        evidence_ids = [correction["event_id"]]
        assert (await client.call_tool("memory_write", {**update, "expected_version": 0,
                                                          "evidence_ids": evidence_ids})).is_error
        revoked = await call(client, "memory_revoke", record_id=first["record_id"], expected_version=1,
                             reason="The old preference was withdrawn.", evidence_ids=evidence_ids,
                             idempotency_key="withdrawal")
        result = await client.call_tool("memory_write", {**update, "expected_version": revoked["version"],
                                                          "evidence_ids": evidence_ids})
        assert result.is_error
        current = await call(client, "memory_get", record_id=first["record_id"])
        assert current["status"] == "revoked" and current["version"] == 2
