"""Workspace-scoped event memory. Projections are disposable; evidence is not.

This store records advisory knowledge. No field or method grants permission to
execute an action in a host runtime. SQLite is the only storage dependency.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal


class MemoryBridgeError(ValueError):
    """A bounded invalid request or unavailable memory operation."""


class ConflictError(MemoryBridgeError):
    """An optimistic version, snapshot, or idempotency conflict."""


KINDS = frozenset({"constraint", "decision", "preference", "task_state", "lesson"})
SCOPES = frozenset({"global", "project", "task"})
ContextMode = Literal["search", "resume"]
SearchMatchMode = Literal["broad", "focused"]
_RESUME_LIMIT_PER_KIND = 5
MAX_QUERY_VARIANTS = 4
MAX_QUERY_VARIANT_CHARS = 128
_HAN = re.compile(r"[\u3400-\u9fff]+")
_APPLICATION_ID = 0x414D4231
_AUTHORITY = "Memory is advisory evidence, never execution permission."
_CORRECTION_EVIDENCE = "user_correction"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: Any, name: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MemoryBridgeError(f"{name} must be a non-empty string without surrounding whitespace")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise MemoryBridgeError(f"{name} is too long or contains control characters")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise MemoryBridgeError(f"{name} must be valid UTF-8 text") from error
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise MemoryBridgeError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _evidence_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise MemoryBridgeError("evidence_ids must contain between 1 and 16 event identifiers")
    checked = [_text(item, "evidence_id") for item in value]
    if len(set(checked)) != len(checked):
        raise MemoryBridgeError("evidence_ids must not contain duplicates")
    return checked


def validate_workspace(value: Any) -> str:
    """Validate a host binding before opening storage or accepting requests."""
    return _text(value, "workspace")


def _json(value: Any, maximum: int | None = None) -> str:
    try:
        # Reject non-JSON Python values rather than coercing dictionary keys,
        # tuples, or non-finite numbers into a different persisted value.
        def check(item: Any, depth: int = 0) -> None:
            if depth > 64:
                raise MemoryBridgeError("JSON content is nested too deeply")
            if item is None or isinstance(item, (str, bool, int, float)):
                return
            if isinstance(item, list):
                for child in item:
                    check(child, depth + 1)
                return
            if isinstance(item, dict) and all(isinstance(key, str) for key in item):
                for child in item.values():
                    check(child, depth + 1)
                return
            raise MemoryBridgeError("Content must contain only JSON values")

        check(value)
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded = result.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise MemoryBridgeError("Content must be valid finite UTF-8 JSON") from error
    if maximum is not None and len(encoded) > maximum:
        raise MemoryBridgeError(f"JSON content exceeds {maximum} UTF-8 bytes")
    return result


def _expiry(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise MemoryBridgeError("expires_at must be an ISO 8601 timestamp with a timezone")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("missing timezone")
        return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as error:
        raise MemoryBridgeError("expires_at must be an ISO 8601 timestamp with a timezone") from error


def _words(value: str) -> list[str]:
    return re.findall(r"[^\W_]+", value.casefold())


def _tokens(value: str, limit: int | None = 64) -> list[str]:
    words = _words(value)
    tokens: list[str] = list(words)
    for word in words:
        # unicode61 does not segment a continuous Chinese phrase. Include
        # bounded bigrams in both index and query, with complete terms first.
        for run in _HAN.findall(word):
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    unique = list(dict.fromkeys(tokens))
    return unique if limit is None else unique[:limit]


class MemoryStore:
    def __init__(self, path: str | Path):
        if not isinstance(path, (str, Path)) or not str(path):
            raise MemoryBridgeError("path must identify a memory database")
        self.path = str(path)
        self._lock = threading.RLock()
        self._closed = False
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            # Another client may initialize the schema during startup. Read
            # its application ID, schema, and version from the same snapshot.
            self._db.execute("BEGIN")
            application_id = self._db.execute("PRAGMA application_id").fetchone()[0]
            tables = self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            self._db.commit()
            if application_id not in (0, _APPLICATION_ID) or (application_id == 0 and tables):
                self._db.close()
                raise MemoryBridgeError("Refusing to open an unrelated SQLite database")
            if version not in (0, 1):
                self._db.close()
                raise MemoryBridgeError("This memory database requires a newer Memory Bridge")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS workspaces (
                    workspace TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    workspace TEXT NOT NULL REFERENCES workspaces(workspace),
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    event_type TEXT NOT NULL CHECK(event_type IN ('source','record_put','record_revoke')),
                    kind TEXT NOT NULL, content_json TEXT NOT NULL, source_ref TEXT,
                    record_id TEXT, record_version INTEGER,
                    created_at TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, result_json TEXT NOT NULL,
                    UNIQUE(workspace, revision), UNIQUE(workspace, idempotency_key),
                    UNIQUE(workspace, record_id, record_version)
                );
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
                CREATE TABLE IF NOT EXISTS records (
                    workspace TEXT NOT NULL REFERENCES workspaces(workspace), record_id TEXT NOT NULL,
                    record_key TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
                    kind TEXT NOT NULL, scope TEXT NOT NULL, scope_id TEXT, priority INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','revoked')), expires_at TEXT,
                    updated_at TEXT NOT NULL, snapshot_json TEXT NOT NULL, search_text TEXT NOT NULL,
                    PRIMARY KEY(workspace, record_id), UNIQUE(workspace, record_key)
                );
                CREATE INDEX IF NOT EXISTS records_scope ON records(workspace, kind, scope, scope_id);
                PRAGMA application_id=1095582257;
                PRAGMA user_version=1;
                COMMIT;
            """)
            self._fts = True
            try:
                self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS record_search USING fts5(workspace UNINDEXED, record_id UNINDEXED, text, tokenize='unicode61')")
            except sqlite3.OperationalError as error:
                if "no such module: fts5" not in str(error).lower():
                    raise
                self._fts = False
        except (sqlite3.Error, OSError) as error:
            if hasattr(self, "_db"):
                self._db.close()
            raise MemoryBridgeError("Unable to open the memory database") from error

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise MemoryBridgeError("Memory store is closed")
            try:
                self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield self._db
                self._db.commit()
            except sqlite3.Error as error:
                self._db.rollback()
                raise MemoryBridgeError("Memory database operation failed") from error
            except BaseException:
                self._db.rollback()
                raise

    @staticmethod
    def _workspace(value: Any) -> str:
        return validate_workspace(value)

    @staticmethod
    def _request(operation: str, fields: dict[str, Any]) -> str:
        return hashlib.sha256(_json({"operation": operation, **fields}).encode("utf-8")).hexdigest()

    @staticmethod
    def _revision(db: sqlite3.Connection, workspace: str) -> int:
        row = db.execute("SELECT revision FROM workspaces WHERE workspace=?", (workspace,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _replay(db: sqlite3.Connection, workspace: str, key: str, request_hash: str) -> dict[str, Any] | None:
        row = db.execute("SELECT request_hash,result_json FROM events WHERE workspace=? AND idempotency_key=?", (workspace, key)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ConflictError("idempotency_key was already used for a different request")
        return json.loads(row["result_json"])

    @staticmethod
    def _evidence(db: sqlite3.Connection, workspace: str, evidence_ids: list[str]) -> set[str]:
        source_kinds = set()
        for event_id in evidence_ids:
            row = db.execute("SELECT event_type,kind FROM events WHERE workspace=? AND event_id=?", (workspace, event_id)).fetchone()
            if row is None:
                raise MemoryBridgeError("Evidence event was not found in this workspace")
            if row["event_type"] == "source":
                source_kinds.add(row["kind"])
        return source_kinds

    def _save_event(self, db: sqlite3.Connection, workspace: str, event_type: str, kind: str,
                    content: Any, source_ref: str | None, record_id: str | None, version: int | None,
                    key: str, request_hash: str, result: dict[str, Any]) -> None:
        db.execute("INSERT INTO workspaces(workspace,revision) VALUES (?,0) ON CONFLICT(workspace) DO NOTHING", (workspace,))
        db.execute("UPDATE workspaces SET revision=? WHERE workspace=?", (result["revision"], workspace))
        db.execute("""INSERT INTO events(event_id,workspace,revision,event_type,kind,content_json,source_ref,
            record_id,record_version,created_at,idempotency_key,request_hash,result_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (result["event_id"], workspace, result["revision"], event_type, kind,
            _json(content), source_ref, record_id, version, result["updated_at"] if record_id else result["created_at"],
            key, request_hash, _json(result)))

    def _project(self, db: sqlite3.Connection, result: dict[str, Any]) -> None:
        text = _json(result["content"])
        search_text = text + " " + " ".join(_tokens(text, limit=None))
        db.execute("""INSERT INTO records(workspace,record_id,record_key,version,kind,scope,scope_id,priority,
            status,expires_at,updated_at,snapshot_json,search_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(workspace,record_id) DO UPDATE SET record_key=excluded.record_key,version=excluded.version,
            kind=excluded.kind,scope=excluded.scope,scope_id=excluded.scope_id,priority=excluded.priority,
            status=excluded.status,expires_at=excluded.expires_at,updated_at=excluded.updated_at,
            snapshot_json=excluded.snapshot_json,search_text=excluded.search_text""", (
            result["workspace"], result["record_id"], result["key"], result["version"], result["kind"], result["scope"],
            result["scope_id"], result["priority"], result["lifecycle_status"], result["expires_at"], result["updated_at"],
            _json(result), search_text))
        if self._fts:
            db.execute("DELETE FROM record_search WHERE workspace=? AND record_id=?", (result["workspace"], result["record_id"]))
            db.execute("INSERT INTO record_search(workspace,record_id,text) VALUES (?,?,?)", (result["workspace"], result["record_id"], search_text))

    @staticmethod
    def _effective(result: dict[str, Any], now: str) -> dict[str, Any]:
        result = dict(result)
        status = result["lifecycle_status"]
        if status == "active" and result["expires_at"] is not None and result["expires_at"] <= now:
            status = "expired"
        result["status"] = status
        return result

    def append_event(self, workspace: str, kind: str, content: Any, source_ref: str | None = None,
                     *, idempotency_key: str) -> dict[str, Any]:
        workspace = self._workspace(workspace)
        kind = _text(kind, "kind", 64)
        content = json.loads(_json(content, 65536))
        if source_ref is not None:
            source_ref = _text(source_ref, "source_ref", 4096)
        key = _text(idempotency_key, "idempotency_key")
        request_hash = self._request("append_event", dict(workspace=workspace, kind=kind, content=content, source_ref=source_ref))
        with self._transaction(True) as db:
            replay = self._replay(db, workspace, key, request_hash)
            if replay is not None:
                return replay
            result = dict(event_id="evt_" + uuid.uuid4().hex, workspace=workspace, event_type="source", kind=kind,
                          content=content, source_ref=source_ref, created_at=_now(), revision=self._revision(db, workspace) + 1,
                          advisory=True, authority=_AUTHORITY)
            self._save_event(db, workspace, "source", kind, content, source_ref, None, None, key, request_hash, result)
            return result

    def read_event(self, workspace: str, event_id: str) -> dict[str, Any]:
        workspace, event_id = self._workspace(workspace), _text(event_id, "event_id")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM events WHERE workspace=? AND event_id=?", (workspace, event_id)).fetchone()
            if row is None:
                raise MemoryBridgeError("Event was not found in this workspace")
            return dict(event_id=row["event_id"], workspace=workspace, event_type=row["event_type"], kind=row["kind"],
                        content=json.loads(row["content_json"]), source_ref=row["source_ref"], created_at=row["created_at"],
                        revision=row["revision"], advisory=True, authority=_AUTHORITY)

    def put_record(self, workspace: str, key: str, kind: str, content: Any, evidence_ids: list[str],
                   scope: str = "project", scope_id: str | None = None, priority: int = 50,
                   expires_at: str | None = None, expected_version: int = 0, *, idempotency_key: str) -> dict[str, Any]:
        workspace, record_key = self._workspace(workspace), _text(key, "key", 256)
        if not isinstance(kind, str) or kind not in KINDS:
            raise MemoryBridgeError("Unknown record kind")
        if not isinstance(scope, str) or scope not in SCOPES:
            raise MemoryBridgeError("Unknown record scope")
        if scope == "task":
            scope_id = _text(scope_id, "scope_id")
        elif scope_id is not None:
            raise MemoryBridgeError("scope_id is only supported for task scope")
        content = json.loads(_json(content, 8192))
        priority = _integer(priority, "priority", 0, 100)
        expected_version = _integer(expected_version, "expected_version", 0, 2**63 - 2)
        expires_at = _expiry(expires_at)
        command_key = _text(idempotency_key, "idempotency_key")
        evidence_ids = _evidence_ids(evidence_ids)
        fields = dict(key=record_key, kind=kind, content=content, evidence_ids=evidence_ids,
                      scope=scope, scope_id=scope_id, priority=priority, expires_at=expires_at)
        request_hash = self._request("put_record", dict(workspace=workspace, expected_version=expected_version, **fields))
        with self._transaction(True) as db:
            replay = self._replay(db, workspace, command_key, request_hash)
            if replay is not None:
                return replay
            source_kinds = self._evidence(db, workspace, evidence_ids)
            previous = db.execute("SELECT snapshot_json FROM records WHERE workspace=? AND record_key=?", (workspace, record_key)).fetchone()
            old = json.loads(previous[0]) if previous else None
            if (old["version"] if old else 0) != expected_version:
                raise ConflictError("Record version changed; read the current record before writing")
            if old is None and _CORRECTION_EVIDENCE in source_kinds:
                raise ConflictError("A user correction must target an existing record; find it before writing")
            return self._put_version(db, workspace, old, fields, command_key, request_hash, _now())

    def correct_record(self, workspace: str, record_id: str, expected_version: int, content: Any,
                       evidence_ids: list[str], *, idempotency_key: str) -> dict[str, Any]:
        workspace, record_id = self._workspace(workspace), _text(record_id, "record_id")
        expected_version = _integer(expected_version, "expected_version", 1, 2**63 - 2)
        content = json.loads(_json(content, 8192))
        evidence_ids = _evidence_ids(evidence_ids)
        command_key = _text(idempotency_key, "idempotency_key")
        request_hash = self._request("correct_record", dict(workspace=workspace, record_id=record_id,
            expected_version=expected_version, content=content, evidence_ids=evidence_ids))
        with self._transaction(True) as db:
            replay = self._replay(db, workspace, command_key, request_hash)
            if replay is not None:
                return replay
            row = db.execute("SELECT snapshot_json FROM records WHERE workspace=? AND record_id=?", (workspace, record_id)).fetchone()
            if row is None:
                raise MemoryBridgeError("Record was not found in this workspace")
            old = json.loads(row[0])
            if old["version"] != expected_version:
                raise ConflictError("Record version changed; read the current record before correcting it")
            stamp = _now()
            if self._effective(old, stamp)["status"] != "active":
                raise ConflictError("Only a current active record can be corrected")
            if _CORRECTION_EVIDENCE not in self._evidence(db, workspace, evidence_ids):
                raise MemoryBridgeError("A correction requires the original user_correction evidence")
            if (old["kind"] == "preference" and isinstance(old["content"], dict)
                    and isinstance(old["content"].get("communication"), str) and isinstance(content, str)):
                content = {"communication": content}
                _json(content, 8192)
            fields = {name: old[name] for name in ("key", "kind", "scope", "scope_id", "priority", "expires_at")}
            fields.update(content=content, evidence_ids=evidence_ids)
            return self._put_version(db, workspace, old, fields, command_key, request_hash, stamp)

    def _put_version(self, db: sqlite3.Connection, workspace: str, old: dict[str, Any] | None, fields: dict[str, Any],
                     command_key: str, request_hash: str, stamp: str) -> dict[str, Any]:
        result = dict(**fields, record_id=old["record_id"] if old else "rec_" + uuid.uuid4().hex, workspace=workspace,
                      version=old["version"] + 1 if old else 1, lifecycle_status="active",
                      created_at=old["created_at"] if old else stamp, updated_at=stamp, event_id="evt_" + uuid.uuid4().hex,
                      revision=self._revision(db, workspace) + 1, revocation=None, advisory=True, authority=_AUTHORITY)
        result = self._effective(result, stamp)
        self._save_event(db, workspace, "record_put", result["kind"], result, None, result["record_id"], result["version"],
                         command_key, request_hash, result)
        self._project(db, result)
        return result

    def revoke_record(self, workspace: str, record_id: str, expected_version: int, reason: str,
                      evidence_ids: list[str], *, idempotency_key: str) -> dict[str, Any]:
        workspace, record_id = self._workspace(workspace), _text(record_id, "record_id")
        expected_version = _integer(expected_version, "expected_version", 1, 2**63 - 2)
        reason, command_key = _text(reason, "reason", 4096), _text(idempotency_key, "idempotency_key")
        evidence_ids = _evidence_ids(evidence_ids)
        request_hash = self._request("revoke_record", dict(workspace=workspace, record_id=record_id,
            expected_version=expected_version, reason=reason, evidence_ids=evidence_ids))
        with self._transaction(True) as db:
            replay = self._replay(db, workspace, command_key, request_hash)
            if replay is not None:
                return replay
            self._evidence(db, workspace, evidence_ids)
            row = db.execute("SELECT snapshot_json FROM records WHERE workspace=? AND record_id=?", (workspace, record_id)).fetchone()
            if row is None:
                raise MemoryBridgeError("Record was not found in this workspace")
            result = json.loads(row[0])
            if result["version"] != expected_version:
                raise ConflictError("Record version changed; read the current record before revoking")
            if result["lifecycle_status"] == "revoked":
                raise ConflictError("Record is already revoked")
            result.update(version=expected_version + 1, lifecycle_status="revoked", status="revoked", updated_at=_now(),
                          event_id="evt_" + uuid.uuid4().hex, revision=self._revision(db, workspace) + 1,
                          revocation={"reason": reason, "evidence_ids": evidence_ids})
            self._save_event(db, workspace, "record_revoke", result["kind"], result, None, record_id, result["version"], command_key, request_hash, result)
            self._project(db, result)
            return result

    def get_record(self, workspace: str, record_id: str, version: int | None = None) -> dict[str, Any]:
        workspace, record_id = self._workspace(workspace), _text(record_id, "record_id")
        if version is not None:
            version = _integer(version, "version", 1, 2**63 - 1)
        with self._transaction() as db:
            current = db.execute("SELECT version,snapshot_json FROM records WHERE workspace=? AND record_id=?", (workspace, record_id)).fetchone()
            if current is None:
                raise MemoryBridgeError("Record was not found in this workspace")
            if version is None:
                result = json.loads(current["snapshot_json"])
            else:
                row = db.execute("SELECT result_json FROM events WHERE workspace=? AND record_id=? AND record_version=?", (workspace, record_id, version)).fetchone()
                if row is None:
                    raise MemoryBridgeError("Record version was not found in this workspace")
                result = json.loads(row[0])
            result = self._effective(result, _now())
            result.update(is_current=result["version"] == current["version"], current_version=current["version"])
            return result

    @staticmethod
    def _task(task_id: Any) -> str | None:
        return _text(task_id, "task_id") if task_id is not None else None

    @staticmethod
    def _scope_sql() -> str:
        return "(r.scope <> 'task' OR r.scope_id=?)"

    def _search_rows(self, db: sqlite3.Connection, workspace: str, query: str, task_id: str | None, limit: int,
                     now: str, exclude_constraints: bool, *, match_all: bool = False) -> list[sqlite3.Row]:
        base = "r.workspace=? AND r.status='active' AND (r.expires_at IS NULL OR r.expires_at>?) AND " + self._scope_sql()
        parameters: list[Any] = [workspace, now, task_id]
        if exclude_constraints:
            base += " AND r.kind<>'constraint'"

        if match_all:
            words = dict.fromkeys(_words(query))
            if not words:
                return []
            anchors = []
            for word in words:
                if _HAN.fullmatch(word):
                    # The first bigram narrows FTS candidates. The complete
                    # contiguous term is checked before ranking and LIMIT.
                    base += " AND instr(r.search_text,?)>0"
                    parameters.append(word)
                    if len(word) >= 2:
                        anchors.append(word[:2])
                else:
                    # The projection appends case-folded, space-delimited words.
                    base += " AND instr(' ' || r.search_text || ' ',' ' || ? || ' ')>0"
                    parameters.append(word)
                    anchors.append(word)
            tokens = list(dict.fromkeys(anchors))
            operator = " AND "
        else:
            tokens = _tokens(query)
            operator = " OR "
            if tokens and not self._fts:
                matches = operator.join("instr(lower(r.search_text),?)>0" for _ in tokens)
                base += " AND (" + matches + ")"
                parameters.extend(tokens)

        source = "records r"
        order = "r.priority DESC,r.updated_at DESC,r.record_id"
        if tokens and self._fts:
            expression = operator.join('"' + token + '"' for token in tokens)
            source = ("record_search JOIN records r "
                      "ON r.workspace=record_search.workspace AND r.record_id=record_search.record_id")
            base = "record_search MATCH ? AND " + base
            parameters.insert(0, expression)
            order = "bm25(record_search)," + order
        return db.execute(
            f"SELECT r.record_id,r.snapshot_json FROM {source} WHERE {base} ORDER BY {order} LIMIT ?",
            [*parameters, limit],
        ).fetchall()

    def _search(self, db: sqlite3.Connection, workspace: str, query: str, task_id: str | None, limit: int,
                now: str, exclude_constraints: bool = False, *,
                query_variants: tuple[str, ...] = (), match_mode: SearchMatchMode = "broad") -> list[dict[str, Any]]:
        def rows(phrase: str, *, match_all: bool = False) -> list[sqlite3.Row]:
            return self._search_rows(db, workspace, phrase, task_id, limit, now, exclude_constraints,
                                     match_all=match_all)

        if not query_variants and match_mode == "broad":
            return [self._effective(json.loads(row["snapshot_json"]), now) for row in rows(query)]

        selected: dict[str, tuple[sqlite3.Row, str | None]] = {
            row["record_id"]: (row, None) for row in rows(query, match_all=True)
        }
        expanded: dict[str, tuple[sqlite3.Row, str]] = {}
        scores: dict[str, float] = {}
        if len(selected) < limit:
            for variant in query_variants:
                for rank, row in enumerate(rows(variant, match_all=True), start=1):
                    record_id = row["record_id"]
                    expanded.setdefault(record_id, (row, variant))
                    # Fuse ranks across searches whose BM25 scores aren't comparable.
                    scores[record_id] = scores.get(record_id, 0) + 1 / (60 + rank)

            for record_id in sorted(expanded, key=scores.__getitem__, reverse=True):
                selected.setdefault(record_id, expanded[record_id])
        if len(selected) < limit and match_mode == "broad" and _words(query):
            for row in rows(query):
                selected.setdefault(row["record_id"], (row, None))

        records = []
        for row, variant in list(selected.values())[:limit]:
            record = self._effective(json.loads(row["snapshot_json"]), now)
            if match_mode == "focused":
                record["recall_match"] = {
                    "source": "query" if variant is None else "query_variant",
                    "terms": list(dict.fromkeys(_words(query if variant is None else variant))),
                }
            records.append(record)
        return records

    def _resume_candidates(self, db: sqlite3.Connection, workspace: str, task_id: str | None,
                           now: str) -> tuple[list[dict[str, Any]], int]:
        candidates = []
        count = 0
        for kind in ("task_state", "decision"):
            rows = db.execute(
                f"""SELECT r.snapshot_json, count(*) OVER () AS eligible
                    FROM records r
                    WHERE r.workspace=? AND r.kind=? AND r.status='active'
                      AND (r.expires_at IS NULL OR r.expires_at>?) AND {self._scope_sql()}
                    ORDER BY (r.scope='task') DESC, r.priority DESC, r.updated_at DESC, r.record_id
                    LIMIT ?""",
                (workspace, kind, now, task_id, _RESUME_LIMIT_PER_KIND),
            ).fetchall()
            if rows:
                count += rows[0]["eligible"]
                candidates.extend(self._effective(json.loads(row["snapshot_json"]), now) for row in rows)
        return candidates, count

    @staticmethod
    def _query(query: Any) -> str:
        if not isinstance(query, str) or len(query) > 512 or any(ord(character) < 32 for character in query):
            raise MemoryBridgeError("query must be a string of at most 512 characters without control characters")
        try:
            query.encode("utf-8")
        except UnicodeError as error:
            raise MemoryBridgeError("query must be valid UTF-8 text") from error
        return query

    @staticmethod
    def _query_variants(query: str, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or len(value) > MAX_QUERY_VARIANTS:
            raise MemoryBridgeError(f"query_variants must be a list of at most {MAX_QUERY_VARIANTS} strings")
        variants: list[str] = []
        seen = {frozenset(_words(query))}
        for item in value:
            variant = _text(item, "query variant", MAX_QUERY_VARIANT_CHARS)
            terms = frozenset(_words(variant))
            if not terms:
                raise MemoryBridgeError("query variants must contain searchable terms")
            if terms not in seen:
                variants.append(variant)
                seen.add(terms)
        return tuple(variants)

    def search(self, workspace: str, query: str, task_id: str | None = None, limit: int = 20, *,
               query_variants: list[str] | None = None, match_mode: SearchMatchMode = "broad") -> list[dict[str, Any]]:
        workspace, query, task_id = self._workspace(workspace), self._query(query), self._task(task_id)
        limit = _integer(limit, "limit", 1, 100)
        variants = self._query_variants(query, query_variants)
        if match_mode not in ("broad", "focused"):
            raise MemoryBridgeError("Unknown search match mode")
        with self._transaction() as db:
            return self._search(db, workspace, query, task_id, limit, _now(), query_variants=variants,
                                match_mode=match_mode)

    def list_records(self, workspace: str, kind: str | None = None, task_id: str | None = None,
                     limit: int = 50, offset: int = 0, expected_revision: int | None = None) -> dict[str, Any]:
        workspace, task_id = self._workspace(workspace), self._task(task_id)
        if kind is not None and (not isinstance(kind, str) or kind not in KINDS):
            raise MemoryBridgeError("Unknown record kind")
        limit, offset = _integer(limit, "limit", 1, 100), _integer(offset, "offset", 0, 2**63 - 1)
        if expected_revision is not None:
            expected_revision = _integer(expected_revision, "expected_revision", 0, 2**63 - 1)
        with self._transaction() as db:
            revision = self._revision(db, workspace)
            if expected_revision is not None and expected_revision != revision:
                raise ConflictError("Workspace changed; restart pagination from a fresh snapshot")
            where = "r.workspace=? AND " + self._scope_sql()
            parameters: list[Any] = [workspace, task_id]
            if kind is not None:
                where += " AND r.kind=?"
                parameters.append(kind)
            total = db.execute("SELECT count(*) FROM records r WHERE " + where, parameters).fetchone()[0]
            rows = db.execute("SELECT r.snapshot_json FROM records r WHERE " + where + " ORDER BY r.priority DESC,r.record_id LIMIT ? OFFSET ?", [*parameters, limit, offset]).fetchall()
            now = _now()
            return dict(items=[self._effective(json.loads(row[0]), now) for row in rows], total=total,
                        next_offset=offset + len(rows) if offset + len(rows) < total else None, revision=revision,
                        advisory=True, authority=_AUTHORITY)

    def context(self, workspace: str, query: str = "", task_id: str | None = None, max_chars: int = 12000,
                *, mode: ContextMode = "search", query_variants: list[str] | None = None) -> dict[str, Any]:
        workspace, query, task_id = self._workspace(workspace), self._query(query), self._task(task_id)
        max_chars = _integer(max_chars, "max_chars", 1, 100000)
        if mode not in ("search", "resume"):
            raise MemoryBridgeError("Unknown context mode")
        variants = self._query_variants(query, query_variants)
        with self._transaction() as db:
            now, revision = _now(), self._revision(db, workspace)
            rows = db.execute("SELECT r.snapshot_json FROM records r WHERE r.workspace=? AND r.kind='constraint' AND r.status='active' AND (r.expires_at IS NULL OR r.expires_at>?) AND " + self._scope_sql() + " ORDER BY r.priority DESC,r.record_id", (workspace, now, task_id)).fetchall()
            constraints = [self._effective(json.loads(row[0]), now) for row in rows]
            # Only preferences explicitly marked as communication habits travel
            # with unrelated topics. Ordinary likes remain query-dependent.
            styles = db.execute(
                """SELECT r.snapshot_json FROM records r WHERE r.workspace=? AND r.kind='preference'
                   AND r.status='active' AND (r.expires_at IS NULL OR r.expires_at>?)
                   AND json_type(r.snapshot_json,'$.content.communication')='text'
                   AND length(trim(json_extract(r.snapshot_json,'$.content.communication')))>0 AND """
                + self._scope_sql()
                + " ORDER BY (r.scope='task') DESC,r.priority DESC,r.updated_at DESC,r.record_id LIMIT 4",
                (workspace, now, task_id),
            ).fetchall()
            resume_count = None
            if mode == "resume":
                candidates, resume_count = self._resume_candidates(db, workspace, task_id, now)
            else:
                candidates = self._search(db, workspace, query, task_id, 100, now, exclude_constraints=True,
                                          query_variants=variants)

            def fit(items: Iterable[dict[str, Any]], available: int) -> tuple[list[dict[str, Any]], int]:
                selected = []
                for item in items:
                    cost = len(_json(item))
                    if cost <= available:
                        selected.append(item)
                        available -= cost
                return selected, available

            selected_constraints, remaining = fit(constraints, max_chars)
            # Reserve most of the remaining budget for task content.
            style_budget = min(remaining, max_chars // 4, 2500)
            selected_styles, unspent = fit(
                (self._effective(json.loads(row[0]), now) for row in styles[:3]), style_budget)
            style_ids = {item["record_id"] for item in selected_styles}
            selected_records, remaining = fit(
                (item for item in candidates if item["record_id"] not in style_ids),
                remaining - (style_budget - unspent))
            used = max_chars - remaining
            omitted = len(constraints) - len(selected_constraints)
            coverage = dict(complete_constraints=omitted == 0, total_constraints=len(constraints),
                included_constraints=len(selected_constraints), omitted_constraints=omitted,
                revision=revision, chars_used=used, max_chars=max_chars,
                retrieval_scope="Constraints are exhaustive before budgeting; other records are lexical top-100 candidates",
                budget_basis="Sum of complete record JSON character counts; coverage metadata is excluded",
                pagination=dict(method="list_records", kind="constraint", task_id=task_id, limit=50,
                                offset=0, expected_revision=revision, includes_inactive=True))
            if resume_count is not None:
                coverage["retrieval_scope"] = (
                    "Constraints are exhaustive before budgeting; resume selects current task states and decisions"
                )
                coverage["resume"] = dict(per_kind_limit=_RESUME_LIMIT_PER_KIND, eligible_records=resume_count,
                    omitted_by_limit=resume_count - len(candidates), omitted_by_budget=len(candidates) - len(selected_records))
            elif variants:
                coverage["retrieval_scope"] = (
                    "Constraints are exhaustive before budgeting; other records are lexical top-100 candidates "
                    "from the original query and caller-provided variants"
                )
            if styles:
                coverage["communication_preferences"] = dict(included=len(selected_styles),
                    has_omissions=len(styles) > len(selected_styles))
            return dict(workspace=workspace, task_id=task_id, constraints=selected_constraints,
                        communication_preferences=selected_styles,
                        records=selected_records, coverage=coverage, advisory=True, authority=_AUTHORITY)

    def rebuild(self, workspace: str) -> dict[str, Any]:
        workspace = self._workspace(workspace)
        with self._transaction(True) as db:
            rows = db.execute("SELECT * FROM events WHERE workspace=? ORDER BY revision", (workspace,)).fetchall()
            db.execute("DELETE FROM records WHERE workspace=?", (workspace,))
            if self._fts:
                db.execute("DELETE FROM record_search WHERE workspace=?", (workspace,))
            revision, versions = 0, {}
            for row in rows:
                if row["revision"] != revision + 1:
                    raise MemoryBridgeError("Event history has a revision gap; projection was not rebuilt")
                revision = row["revision"]
                if row["event_type"] != "source":
                    snapshot = json.loads(row["result_json"])
                    record_id = row["record_id"]
                    if snapshot["workspace"] != workspace or snapshot["record_id"] != record_id or snapshot["version"] != versions.get(record_id, 0) + 1:
                        raise MemoryBridgeError("Event history has an invalid record sequence")
                    versions[record_id] = snapshot["version"]
                    self._project(db, snapshot)
            db.execute("UPDATE workspaces SET revision=? WHERE workspace=?", (revision, workspace))
            return dict(workspace=workspace, events_replayed=len(rows), records_rebuilt=len(versions), revision=revision,
                        advisory=True, authority=_AUTHORITY)
