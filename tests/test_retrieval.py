"""Keyword expansion supplied by a caller through the existing recall tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from angelbot_memory_bridge.server import create_server
from angelbot_memory_bridge.store import MemoryStore


def save(store: MemoryStore, key: str, content: str, *, workspace: str = "alpha",
         kind: str = "preference", **fields) -> dict:
    source = store.append_event(workspace, "statement", content, idempotency_key="source-" + key)
    return store.put_record(workspace, key, kind, content, [source["event_id"]],
                            idempotency_key="record-" + key, **fields)


@pytest.mark.parametrize("force_fallback", [False, True], ids=["automatic", "fallback"])
def test_complete_variant_terms_and_fusion_preserve_search_boundaries(force_fallback: bool) -> None:
    with MemoryStore(":memory:") as store:
        if force_fallback:
            store._fts = False
        target = save(store, "both", "安排回滚验证和恢复演习，使用快照。")
        one_match = save(store, "one", "快照保存手册附带恢复演习。", priority=80)
        direct = save(store, "direct", "怎么做回退：先安排维护窗口。")
        partial = save(store, "partial", "回退说明更新了。", priority=100)
        save(store, "shared-bigrams", "回滚演练通过验证；恢复演练使用快照。", priority=100)
        save(store, "missing-term", "今天的回滚验证已经完成。", priority=100)
        save(store, "other-task", target["content"], scope="task", scope_id="sibling", priority=100)
        save(store, "other-workspace", target["content"], workspace="beta", priority=100)
        save(store, "expired", target["content"], expires_at="2000-01-01T00:00:00Z", priority=100)
        revoked = save(store, "revoked", target["content"], priority=100)
        store.revoke_record("alpha", revoked["record_id"], 1, "Withdrawn", revoked["evidence_ids"],
                            idempotency_key="withdrawn")
        constraint = save(store, "constraint", "只在周末演习。", kind="constraint")

        query = "怎么做回退？"
        variants = ["恢复演习 快照", "回滚验证 快照", "快照 恢复演习"]
        legacy = store.search("alpha", query, task_id="current")
        assert direct in legacy and partial in legacy and target not in legacy
        assert store.search("alpha", query, task_id="current", query_variants=[]) == legacy
        assert store.search("alpha", query, task_id="current", match_mode="broad") == legacy

        aided = store.search("alpha", query, task_id="current", limit=4, query_variants=variants)
        assert [item["key"] for item in aided] == ["direct", "both", "one", "partial"]
        assert aided[1]["evidence_ids"] == target["evidence_ids"]
        assert aided[2]["record_id"] == one_match["record_id"]
        variant_only = store.search("alpha", "完全不同的词", task_id="current", query_variants=variants)
        assert [item["key"] for item in variant_only] == ["both", "one"]
        assert store.search("alpha", "完全不同的词", query_variants=["回滚验证 不存在的词"]) == []

        focused = store.search("alpha", query, task_id="current", query_variants=variants, match_mode="focused")
        assert [item["key"] for item in focused] == ["direct", "both", "one"]
        assert [item["recall_match"] for item in focused] == [
            {"source": "query", "terms": ["怎么做回退"]},
            {"source": "query_variant", "terms": ["恢复演习", "快照"]},
            {"source": "query_variant", "terms": ["恢复演习", "快照"]},
        ]
        assert "recall_match" not in store.get_record("alpha", target["record_id"])
        assert store.search("alpha", "回退 保单")
        assert store.search("alpha", "回退 保单", match_mode="focused") == []
        assert store.search("alpha", "回退 保单", query_variants=["回滚验证 保单"], match_mode="focused") == []
        assert store.search("alpha", "???", match_mode="focused") == []

        context = store.context("alpha", query, task_id="current", query_variants=variants)
        assert [item["key"] for item in context["records"][:4]] == ["direct", "both", "one", "partial"]
        assert [item["record_id"] for item in context["constraints"]] == [constraint["record_id"]]
        assert context["coverage"]["complete_constraints"] is True
        resumed = store.context("alpha", query, task_id="current", mode="resume", query_variants=variants)
        assert resumed == store.context("alpha", query, task_id="current", mode="resume")

        device = save(store, "device", "Inspect the Hydraulic Press at noon.")
        save(store, "vessel", "The hydraulic pressurized vessel is available.")
        assert store.search("alpha", "equipment inspection", query_variants=["hydraulic press"]) == [device]
        short_han = save(store, "tea", "晚饭后喝茶。")
        short_latin = save(store, "plot", "Use R for plotting.")
        assert [item["record_id"] for item in store.search("alpha", "茶", match_mode="focused")] == [short_han["record_id"]]
        assert [item["record_id"] for item in store.search("alpha", "R", match_mode="focused")] == [short_latin["record_id"]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_tools_accept_optional_bounded_query_variants(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    with MemoryStore(path) as store:
        target = save(store, "proof", "报账时扫描购物凭据。")
    async with Client(create_server(path, "alpha")) as client:
        query = "申领款项要交啥？"
        variants = ["报账 凭据"]
        for tool, field in (("memory_search", "items"), ("memory_context", "records")):
            bare = await client.call_tool(tool, {"query": query})
            assert not bare.is_error and bare.structured_content[field] == []
            aided = await client.call_tool(tool, {"query": query, "query_variants": variants})
            assert not aided.is_error
            assert [item["record_id"] for item in aided.structured_content[field]] == [target["record_id"]]
            for invalid in (variants * 5, ["x" * 129], ["!!!"], "报账"):
                failure = await client.call_tool(tool, {"query": query, "query_variants": invalid})
                assert failure.is_error
        broad = await client.call_tool("memory_search", {"query": "报账 交通"})
        assert not broad.is_error and [item["record_id"] for item in broad.structured_content["items"]] == [target["record_id"]]
        focused = await client.call_tool("memory_search", {"query": "报账 交通", "match_mode": "focused"})
        assert not focused.is_error and focused.structured_content["items"] == []
        assert focused.structured_content["retrieval"]["match_mode"] == "focused"
        assert "source events" in focused.structured_content["retrieval"]["guidance"]
        found = await client.call_tool("memory_search", {
            "query": query, "query_variants": variants, "match_mode": "focused",
        })
        assert not found.is_error
        assert [item["record_id"] for item in found.structured_content["items"]] == [target["record_id"]]
        assert found.structured_content["items"][0]["recall_match"] == {
            "source": "query_variant", "terms": ["报账", "凭据"],
        }
        invalid_mode = await client.call_tool("memory_search", {"query": query, "match_mode": "exact"})
        assert invalid_mode.is_error
