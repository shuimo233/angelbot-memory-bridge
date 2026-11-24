from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import angelbot_memory_bridge.store as storage
from angelbot_memory_bridge.store import ConflictError, MemoryBridgeError, MemoryStore


@pytest.fixture
def store(tmp_path):
    with MemoryStore(tmp_path / "memory.sqlite3") as value:
        yield value


def evidence(store, workspace="alpha", text="explicit source", key="evidence"):
    return store.append_event(workspace, "user_statement", text, idempotency_key=key)["event_id"]


def record(store, source, key="rule", workspace="alpha", kind="constraint", content="Do not publish without consent", **kwargs):
    return store.put_record(workspace, key, kind, content, [source], idempotency_key="put-" + key, **kwargs)


def test_original_events_are_preserved_and_input_values_are_copied(store):
    content = {"原句": ["不要删除原件", "重复引号 \"保留\""], "confirmed": True}
    original = store.append_event("alpha", "user_statement", content, "chat:turn-12", idempotency_key="source")
    content["原句"].append("caller mutation")
    loaded = store.read_event("alpha", original["event_id"])
    assert loaded["content"] == original["content"]
    assert loaded["source_ref"] == "chat:turn-12"
    assert loaded["authority"] == "Memory is advisory evidence, never execution permission."
    assert loaded["advisory"] is True
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._db.execute("UPDATE events SET content_json='{}'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._db.execute("DELETE FROM events")
    assert store.read_event("alpha", original["event_id"])["content"] == original["content"]


def test_correction_preserves_each_version_and_checks_compare_and_swap(store):
    first_source = evidence(store, text="Use SQLite")
    first = record(store, first_source, kind="decision", content="Use SQLite")
    second_source = evidence(store, text="Keep SQLite with transactions", key="new-source")
    second = store.put_record("alpha", "rule", "decision", "Keep SQLite with transactions", [first_source, second_source],
                              expected_version=1, idempotency_key="correction")
    assert first["record_id"] == second["record_id"]
    assert second["version"] == 2
    historical = store.get_record("alpha", first["record_id"], 1)
    assert historical["content"] == "Use SQLite"
    assert historical["is_current"] is False
    assert historical["current_version"] == 2
    assert store.get_record("alpha", first["record_id"])["content"] == second["content"]
    before = store.list_records("alpha")["revision"]
    with pytest.raises(ConflictError, match="version changed"):
        store.put_record("alpha", "rule", "decision", "stale", [second_source], expected_version=1, idempotency_key="stale")
    assert store.list_records("alpha")["revision"] == before
    with pytest.raises(MemoryBridgeError, match="version was not found"):
        store.get_record("alpha", first["record_id"], 99)


def test_idempotency_returns_original_result_across_reopen_and_rebuild(tmp_path):
    path = tmp_path / "memory.sqlite3"
    with MemoryStore(path) as store:
        event = store.append_event("alpha", "statement", {"b": 2, "a": 1}, idempotency_key="event")
        first = record(store, event["event_id"])
        assert store.append_event("alpha", "statement", {"a": 1, "b": 2}, idempotency_key="event") == event
        with pytest.raises(ConflictError, match="different request"):
            store.append_event("alpha", "statement", {"a": 9}, idempotency_key="event")
        store.rebuild("alpha")
    with MemoryStore(path) as reopened:
        assert reopened.append_event("alpha", "statement", {"a": 1, "b": 2}, idempotency_key="event") == event
        assert reopened.put_record("alpha", "rule", "constraint", "Do not publish without consent", [event["event_id"]], idempotency_key="put-rule") == first
        # A key is scoped to one workspace, but not to one mutation method.
        other = reopened.append_event("beta", "statement", "separate", idempotency_key="event")
        assert other["workspace"] == "beta"
        with pytest.raises(ConflictError):
            reopened.put_record("alpha", "another", "decision", "x", [event["event_id"]], idempotency_key="event")


def test_revocation_retains_evidence_and_is_idempotent(store):
    original = evidence(store)
    rule = record(store, original)
    withdrawal = evidence(store, text="Withdraw this rule", key="withdrawal")
    revoked = store.revoke_record("alpha", rule["record_id"], 1, "explicit withdrawal", [withdrawal], idempotency_key="revoke")
    assert revoked["version"] == 2
    assert revoked["revocation"] == {"reason": "explicit withdrawal", "evidence_ids": [withdrawal]}
    assert store.revoke_record("alpha", rule["record_id"], 1, "explicit withdrawal", [withdrawal], idempotency_key="revoke") == revoked
    assert store.get_record("alpha", rule["record_id"])["status"] == "revoked"
    assert store.search("alpha", "publish") == []
    assert store.context("alpha")["constraints"] == []
    assert store.read_event("alpha", original)["content"] == "explicit source"
    assert store.get_record("alpha", rule["record_id"], 1)["content"] == rule["content"]
    with pytest.raises(ConflictError, match="already revoked"):
        store.revoke_record("alpha", rule["record_id"], 2, "again", [withdrawal], idempotency_key="again")


def test_expiry_is_effective_without_destroying_history(store, monkeypatch):
    monkeypatch.setattr(storage, "_now", lambda: "2030-01-01T00:00:00.000000Z")
    source = evidence(store)
    rule = record(store, source, expires_at="2030-01-01T01:00:00+01:00")
    assert rule["expires_at"] == "2030-01-01T00:00:00.000000Z"
    assert store.get_record("alpha", rule["record_id"])["status"] == "expired"
    assert store.context("alpha")["coverage"]["total_constraints"] == 0
    assert store.search("alpha", "publish") == []
    assert store.list_records("alpha")["items"][0]["status"] == "expired"
    assert store.read_event("alpha", rule["event_id"])["content"]["content"] == rule["content"]


def test_workspace_and_task_scope_cannot_be_overridden_by_content(store):
    source = evidence(store)
    global_rule = record(store, source, key="global", scope="global")
    project_rule = record(store, source, key="project")
    task_rule = record(store, source, key="task", scope="task", scope_id="task-a", content={"workspace": "beta", "text": "temporary rule"})
    assert {item["record_id"] for item in store.context("alpha")["constraints"]} == {global_rule["record_id"], project_rule["record_id"]}
    assert len(store.context("alpha", task_id="task-a")["constraints"]) == 3
    assert len(store.context("alpha", task_id="task-b")["constraints"]) == 2
    assert store.context("beta", task_id="task-a")["constraints"] == []
    with pytest.raises(MemoryBridgeError, match="scope_id"):
        record(store, source, key="missing-task-id", scope="task")
    with pytest.raises(MemoryBridgeError, match="scope_id"):
        record(store, source, key="unexpected-id", scope="global", scope_id="task-a")
    assert task_rule["advisory"] is True


def test_cross_workspace_evidence_reads_and_revocation_are_rejected(store):
    source = evidence(store)
    rule = record(store, source)
    own_source = evidence(store, workspace="beta")
    with pytest.raises(MemoryBridgeError, match="this workspace"):
        store.put_record("beta", "leak", "constraint", "wrong namespace", [source], idempotency_key="leak")
    with pytest.raises(MemoryBridgeError, match="this workspace"):
        store.read_event("beta", source)
    with pytest.raises(MemoryBridgeError, match="this workspace"):
        store.get_record("beta", rule["record_id"])
    with pytest.raises(MemoryBridgeError, match="this workspace"):
        store.revoke_record("beta", rule["record_id"], 1, "no access", [own_source], idempotency_key="revoke")
    assert store.list_records("beta")["total"] == 0


def test_constraints_are_query_independent_and_budget_omissions_are_explicit(store):
    source = evidence(store)
    first = record(store, source, key="first", content="Always preserve original evidence", priority=100)
    second = record(store, source, key="second", content="Ask before publishing a release", priority=20)
    unrelated = store.context("alpha", query="weather on Jupiter")
    assert {item["record_id"] for item in unrelated["constraints"]} == {first["record_id"], second["record_id"]}
    assert unrelated["coverage"]["complete_constraints"] is True
    tiny = store.context("alpha", query="weather on Jupiter", max_chars=1)
    assert tiny["constraints"] == []
    assert tiny["coverage"]["complete_constraints"] is False
    assert tiny["coverage"]["omitted_constraints"] == 2
    assert tiny["coverage"]["chars_used"] == 0
    page = tiny["coverage"]["pagination"]
    readback = store.list_records("alpha", kind=page["kind"], expected_revision=page["expected_revision"])
    assert {item["content"] for item in readback["items"]} == {first["content"], second["content"]}
    exact = store.context("alpha", max_chars=len(storage._json(first)))
    assert exact["constraints"] == [first]
    assert exact["coverage"]["chars_used"] <= exact["coverage"]["max_chars"]
    assert all(item["content"] in [first["content"], second["content"]] for item in exact["constraints"])


def test_resume_bounds_each_kind_and_reports_budget_and_limit_omissions(store):
    source = evidence(store)
    rule = record(store, source)
    for kind in ("task_state", "decision"):
        for index in range(6):
            record(store, source, key=f"{kind}-{index}", kind=kind, priority=index)
    task_decision = record(store, source, key="task-decision", kind="decision", priority=0,
                           scope="task", scope_id="task-a")
    revoked = record(store, source, key="revoked", kind="decision", priority=100)
    store.revoke_record("alpha", revoked["record_id"], 1, "superseded", [source], idempotency_key="revoke")
    record(store, source, key="expired", kind="task_state", priority=100, expires_at="2000-01-01T00:00:00Z")
    record(store, source, key="lesson", kind="lesson", priority=100)

    full = store.context("alpha", task_id="task-a", mode="resume")
    assert [item["kind"] for item in full["records"]] == ["task_state"] * 5 + ["decision"] * 5
    assert [item["key"] for item in full["records"][:5]] == [f"task_state-{index}" for index in range(5, 0, -1)]
    assert full["records"][5] == task_decision
    assert full["coverage"]["resume"] == {
        "per_kind_limit": 5, "eligible_records": 13, "omitted_by_limit": 3, "omitted_by_budget": 0,
    }
    constrained = store.context("alpha", task_id="task-a", mode="resume", max_chars=len(storage._json(rule)))
    assert constrained["constraints"] == [rule]
    assert constrained["records"] == []
    assert constrained["coverage"]["resume"]["omitted_by_budget"] == 10


def test_pagination_has_a_stable_revision_and_cannot_silently_skip_concurrent_writes(store):
    source = evidence(store)
    for index in range(3):
        record(store, source, key=f"r{index}")
    first = store.list_records("alpha", kind="constraint", limit=1)
    second = store.list_records("alpha", kind="constraint", limit=1, offset=first["next_offset"], expected_revision=first["revision"])
    assert first["items"][0]["record_id"] != second["items"][0]["record_id"]
    assert first["total"] == second["total"] == 3
    evidence(store, key="new-event")
    with pytest.raises(ConflictError, match="restart pagination"):
        store.list_records("alpha", offset=2, expected_revision=first["revision"])
    assert store.list_records("alpha", offset=99)["next_offset"] is None


def test_rebuild_restores_deleted_projection_search_and_versions_without_changing_events(store):
    source = evidence(store, text="Keep full records")
    rule = record(store, source, content="Preserve original evidence")
    preference = record(store, source, key="tea", kind="preference", content="喜欢焙火乌龙茶")
    store.revoke_record("alpha", rule["record_id"], 1, "withdraw", [source], idempotency_key="withdraw")
    before_events = [tuple(row) for row in store._db.execute("SELECT * FROM events ORDER BY sequence")]
    before_listing = store.list_records("alpha")
    store._db.execute("DELETE FROM records WHERE workspace='alpha'")
    if store._fts:
        store._db.execute("DELETE FROM record_search WHERE workspace='alpha'")
    store._db.execute("UPDATE workspaces SET revision=999 WHERE workspace='alpha'")
    restored = store.rebuild("alpha")
    assert restored["events_replayed"] == 4
    assert restored["records_rebuilt"] == 2
    assert store.list_records("alpha") == before_listing
    assert [tuple(row) for row in store._db.execute("SELECT * FROM events ORDER BY sequence")] == before_events
    assert store.search("alpha", "乌龙茶")[0]["record_id"] == preference["record_id"]
    assert store.get_record("alpha", rule["record_id"])["status"] == "revoked"
    assert store.get_record("alpha", rule["record_id"], 1)["content"] == rule["content"]


def test_two_connections_cannot_both_commit_the_same_expected_version(tmp_path):
    path = tmp_path / "memory.sqlite3"
    with MemoryStore(path) as first, MemoryStore(path) as second:
        source = evidence(first)
        initial = record(first, source, kind="decision")

        def write(store, suffix):
            try:
                return store.put_record("alpha", "rule", "decision", suffix, [source], expected_version=1, idempotency_key=suffix)
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda arguments: write(*arguments), [(first, "first"), (second, "second")]))
        assert sum(result == "conflict" for result in results) == 1
        assert first.get_record("alpha", initial["record_id"])["version"] == 2
        assert first.list_records("alpha")["revision"] == 3


def test_concurrent_same_idempotency_key_creates_one_event(tmp_path):
    path = tmp_path / "memory.sqlite3"
    with MemoryStore(path) as first, MemoryStore(path) as second:
        def append(store):
            return store.append_event("alpha", "statement", "one source", idempotency_key="same")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(append, [first, second]))
        assert results[0] == results[1]
        assert first.list_records("alpha")["revision"] == 1
        assert first._db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_lexical_search_handles_literal_sql_and_chinese_after_long_prefix(store):
    source = evidence(store)
    chinese = record(store, source, key="chinese", kind="preference", content="前" * 200 + "喜欢焙火乌龙茶")
    assert store.search("alpha", "乌龙茶")[0]["record_id"] == chinese["record_id"]
    assert store.search("alpha", "'; DROP TABLE events; --") == []
    assert store.read_event("alpha", source)["content"] == "explicit source"


def test_complete_query_terms_precede_chinese_bigrams(store):
    source = evidence(store)
    matched = record(store, source, key="english", kind="decision", content="rarekeyword")
    long_chinese_prefix = "".join(chr(0x4E00 + index) for index in range(100))
    assert store.search("alpha", long_chinese_prefix + " rarekeyword")[0]["record_id"] == matched["record_id"]


@pytest.mark.parametrize("call", [
    lambda s: s.append_event("", "statement", "x", idempotency_key="a"),
    lambda s: s.append_event("alpha", "statement", float("nan"), idempotency_key="a"),
    lambda s: s.append_event("alpha", "statement", {1: "not a JSON key"}, idempotency_key="a"),
    lambda s: s.append_event("alpha", "statement", "x" * 65536, idempotency_key="a"),
    lambda s: s.append_event("alpha", "statement", "x", idempotency_key=""),
    lambda s: s.read_event("\ud800", "evt_x"),
    lambda s: s.search("alpha", "\ud800"),
    lambda s: s.search("alpha", "x", limit=True),
    lambda s: s.search("alpha", "x" * 513),
    lambda s: s.list_records("alpha", kind=[]),
    lambda s: s.list_records("alpha", offset=-1),
    lambda s: s.context("alpha", max_chars=0),
])
def test_invalid_values_are_bounded_and_do_not_create_events(store, call):
    with pytest.raises(MemoryBridgeError):
        call(store)
    assert store.list_records("alpha")["revision"] == 0


def test_invalid_record_fields_leave_the_store_unchanged(store):
    source = evidence(store)
    for fields in [dict(kind=[]), dict(scope=[]), dict(priority=True), dict(expires_at="2030-01-01"),
                   dict(evidence_ids=[]), dict(evidence_ids=[source, source]), dict(content="x" * 8192)]:
        arguments = dict(workspace="alpha", key="rule", kind="constraint", content="rule", evidence_ids=[source], idempotency_key="put")
        arguments.update(fields)
        with pytest.raises(MemoryBridgeError):
            store.put_record(**arguments)
    assert store.list_records("alpha")["revision"] == 1
    assert store.list_records("alpha")["total"] == 0


def test_unrelated_databases_and_future_schema_are_not_modified(tmp_path):
    foreign = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(foreign) as database:
        database.execute("CREATE TABLE private_data(value TEXT)")
        database.execute("INSERT INTO private_data VALUES ('not memory')")
    before = foreign.read_bytes()
    with pytest.raises(MemoryBridgeError, match="unrelated"):
        MemoryStore(foreign)
    assert foreign.read_bytes() == before
    future = tmp_path / "future.sqlite3"
    with sqlite3.connect(future) as database:
        database.execute(f"PRAGMA application_id={storage._APPLICATION_ID}")
        database.execute("PRAGMA user_version=999")
    with pytest.raises(MemoryBridgeError, match="newer"):
        MemoryStore(future)


def test_interrupted_schema_initialization_does_not_poison_a_new_database(tmp_path, monkeypatch):
    path = tmp_path / "interrupted.sqlite3"
    original_connect = sqlite3.connect

    def interrupted_connect(*args, **kwargs):
        database = original_connect(*args, **kwargs)

        def authorizer(action, first, second, database_name, trigger):
            if action == sqlite3.SQLITE_CREATE_INDEX and first == "records_scope":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        database.set_authorizer(authorizer)
        return database

    with monkeypatch.context() as temporary:
        temporary.setattr(storage.sqlite3, "connect", interrupted_connect)
        with pytest.raises(MemoryBridgeError, match="Unable to open"):
            MemoryStore(path)
    with original_connect(path) as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == 0
        assert database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    with MemoryStore(path) as recovered:
        source = evidence(recovered)
        assert recovered.read_event("alpha", source)["content"] == "explicit source"


def test_initialization_reads_one_snapshot_when_another_client_creates_the_schema(tmp_path, monkeypatch):
    path = tmp_path / "concurrent-startup.sqlite3"
    connect = sqlite3.connect
    database = connect(path)
    try:
        # WAL lets the second connection finish initialization while the first
        # connection still holds the snapshot it is validating.
        assert database.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        database.close()

    class InterleavedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql == "PRAGMA application_id":
                with MemoryStore(path) as second:
                    evidence(second)
            return cursor

    def interleaved_connect(*args, **kwargs):
        monkeypatch.setattr(storage.sqlite3, "connect", connect)
        return connect(*args, factory=InterleavedConnection, **kwargs)

    monkeypatch.setattr(storage.sqlite3, "connect", interleaved_connect)
    with MemoryStore(path) as first:
        assert first.list_records("alpha")["revision"] == 1


def test_closed_store_fails_clearly(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.close()
    store.close()
    with pytest.raises(MemoryBridgeError, match="closed"):
        store.context("alpha")
