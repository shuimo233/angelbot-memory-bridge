from pathlib import Path

import pytest
from mcp import Client

from angelbot_memory_bridge.server import create_server
from angelbot_memory_bridge.store import MemoryStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_client_can_save_a_habit_in_plain_words_and_use_it_after_reconnecting(tmp_path: Path):
    path = tmp_path / "client.sqlite3"
    async with Client(create_server(path, "home")) as client:
        source = await client.call_tool("memory_append", {
            "kind": "user_statement", "content": "以后先一句话总结。", "idempotency_key": "style/source"})
        arguments = dict(key="summary-first", preference="先一句话总结", evidence_ids=[source.structured_content["event_id"]],
                         idempotency_key="style/write")
        saved = await client.call_tool("memory_save_communication_preference", arguments)
        assert not saved.is_error
        replay = await client.call_tool("memory_save_communication_preference", arguments)
        assert replay.structured_content == saved.structured_content
    async with Client(create_server(path, "home")) as fresh:
        view = await fresh.call_tool("memory_context", {"query": "种薄荷"})
        [habit] = view.structured_content["communication_preferences"]
        assert habit["content"] == {"communication": "先一句话总结"}
        assert habit["evidence_ids"] == arguments["evidence_ids"]


def put(store, workspace, key, content, *, kind="preference", **options):
    event = store.append_event(workspace, "user_statement", content, idempotency_key=key + "/source")
    return store.put_record(workspace, key, kind, content, [event["event_id"]],
                            idempotency_key=key + "/save", **options)


def test_style_survives_new_session_and_correction_without_leaking_other_preferences(tmp_path: Path):
    path = tmp_path / "chat.sqlite3"
    store = MemoryStore(path)
    first = put(store, "home", "format", {"communication": "先说结论"})
    scoped = put(store, "home", "standup", {"communication": "按三行汇报"}, scope="task", scope_id="meeting")
    put(store, "home", "travel", {"communication": "用当地语言"}, scope="task", scope_id="trip")
    put(store, "home", "drink", "喜欢绿茶")
    put(store, "home", "old", {"communication": "已过期"}, expires_at="2000-01-01T00:00:00Z")
    put(store, "work", "format", {"communication": "使用英文"})
    correction = store.append_event("home", "user_correction", "以后按时间顺序讲", idempotency_key="revise/source")
    revised = store.correct_record("home", first["record_id"], 1, "以后按时间顺序讲", [correction["event_id"]],
                                   idempotency_key="revise/save")

    reopened = MemoryStore(path)
    for mode in ("search", "resume"):
        view = reopened.context("home", query="电脑电池", task_id="meeting", mode=mode)
        habits = view["communication_preferences"]
        assert [row["record_id"] for row in habits] == [scoped["record_id"], first["record_id"]]
        assert habits[1]["content"] == {"communication": "以后按时间顺序讲"}
        assert habits[1]["version"] == 2 and habits[1]["evidence_ids"] == [correction["event_id"]]
        assert view["coverage"]["revision"] == revised["revision"]
    matched = reopened.context("home", query="以后按时间顺序讲")
    assert [item["record_id"] for item in matched["communication_preferences"]] == [first["record_id"]]
    assert first["record_id"] not in {item["record_id"] for item in matched["records"]}
    reopened.revoke_record("home", first["record_id"], 2, "撤回偏好", [correction["event_id"]],
                           idempotency_key="revoke")
    assert reopened.context("home")["communication_preferences"] == []


def test_limit_and_character_budget_keep_constraints_first(tmp_path: Path):
    store = MemoryStore(tmp_path / "budget.sqlite3")
    constraint = put(store, "home", "constraint", "Keep private", kind="constraint")
    for number in range(5):
        put(store, "home", f"habit-{number}", {"communication": f"风格 {number}"}, priority=50 + number)
    view = store.context("home", query="unrelated")
    assert [row["key"] for row in view["communication_preferences"]] == ["habit-4", "habit-3", "habit-2"]
    assert view["coverage"]["communication_preferences"] == {"included": 3, "has_omissions": True}
    small = store.context("home", query="unrelated", max_chars=900)
    assert [row["record_id"] for row in small["constraints"]] == [constraint["record_id"]]
    assert small["communication_preferences"] == []
    assert small["coverage"]["communication_preferences"] == {"included": 0, "has_omissions": True}
    assert small["coverage"]["chars_used"] <= 900
