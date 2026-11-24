"""Actual SDK clients and local server processes; no model or user data involved."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def server_environment() -> dict[str, str]:
    # Do not pass model credentials, account settings, or a user's Codex home to children.
    allowed = {"PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "WINDIR"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(PYTHONPATH=str(PACKAGE_ROOT / "src"), PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    return environment


def parameters(tmp_path: Path, workspace: str | None = None) -> StdioServerParameters:
    args = ["-m", "angelbot_memory_bridge", "serve", "--db", str(tmp_path / "memory.sqlite3")]
    if workspace is not None:
        args.extend(["--workspace", workspace])
    return StdioServerParameters(
        command=sys.executable,
        args=args,
        env=server_environment(), cwd=tmp_path,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("mode, expected_protocol, workspace", [
    ("auto", "2026-07-28", None),
    ("legacy", "2025-11-25", "angelbot"),
])
async def test_real_stdio_roundtrip_and_process_restart(
    tmp_path: Path, mode: str, expected_protocol: str, workspace: str | None,
) -> None:
    options = parameters(tmp_path, workspace)
    async with Client(options, mode=mode, read_timeout_seconds=15) as client:
        assert client.protocol_version == expected_protocol
        assert "memory_shape_request" in (client.instructions or "")
        assert {"memory_context", "memory_shape_request"} <= {tool.name for tool in (await client.list_tools()).tools}
        source = await client.call_tool("memory_append", {
            "kind": "user_request", "content": {"text": "本轮验证不得联网调用模型"},
            "idempotency_key": "same-source",
        })
        assert not source.is_error and source.structured_content is not None
        event_id = source.structured_content["event_id"]
        first = await client.call_tool("memory_write", {
            "key": "offline-run", "kind": "constraint", "content": "No live models in this verification.",
            "evidence_ids": [event_id], "idempotency_key": "same-write", "priority": 100,
        })
        replay = await client.call_tool("memory_write", {
            "key": "offline-run", "kind": "constraint", "content": "No live models in this verification.",
            "evidence_ids": [event_id], "idempotency_key": "same-write", "priority": 100,
        })
        assert not first.is_error and first.structured_content is not None
        assert replay.structured_content == first.structured_content
        record_id = first.structured_content["record_id"]

    # A different server process reconstructs the same state from the dedicated database.
    async with Client(options, mode=mode, read_timeout_seconds=15) as restarted:
        current = await restarted.call_tool("memory_get", {"record_id": record_id})
        assert current.structured_content is not None and current.structured_content["version"] == 1
        recalled = await restarted.call_tool("memory_context", {"query": "unrelated keyword"})
        assert recalled.structured_content is not None
        assert recalled.structured_content["workspace"] == (workspace or "default")
        assert recalled.structured_content["coverage"]["complete_constraints"] is True
        assert recalled.structured_content["constraints"][0]["record_id"] == record_id
        shaped = await restarted.call_tool("memory_shape_request", {"wish": "我想让这段流程更省事。\n每次都要重复操作。"})
        assert not shaped.is_error and shaped.structured_content is not None
        assert shaped.structured_content["constraints"] == recalled.structured_content["constraints"]
        assert shaped.structured_content["coverage"]["revision"] == recalled.structured_content["coverage"]["revision"]
        source = await restarted.call_tool("memory_read_event", {"event_id": event_id})
        assert source.structured_content is not None
        assert source.structured_content["content"] == {"text": "本轮验证不得联网调用模型"}

    other_workspace = None if workspace == "angelbot" else "angelbot"
    async with Client(parameters(tmp_path, other_workspace), mode=mode, read_timeout_seconds=15) as isolated:
        assert (await isolated.call_tool("memory_get", {"record_id": record_id})).is_error


@pytest.mark.anyio
async def test_new_stdio_client_resumes_current_task_after_correction(tmp_path: Path) -> None:
    async def call(client: Client, name: str, **arguments):
        result = await client.call_tool(name, arguments)
        assert not result.is_error and result.structured_content is not None
        return result.structured_content

    options = parameters(tmp_path, "cookbook")
    async with Client(options, mode="2026-07-28", read_timeout_seconds=15) as writer:
        source = await call(writer, "memory_append", kind="user_statement",
                            content="Use cups. The Cider recipe stays unpublished.", idempotency_key="original")
        evidence_ids = [source["event_id"]]
        common = {"evidence_ids": evidence_ids, "scope": "task", "scope_id": "cider"}
        await call(writer, "memory_write", key="sharing", kind="constraint",
                   content="Keep the recipe unpublished.", evidence_ids=evidence_ids, idempotency_key="sharing")
        initial = await call(writer, "memory_write", key="units", kind="decision",
                             content="Use cups.", idempotency_key="units-v1", **common)
        await call(writer, "memory_write", key="next", kind="task_state",
                   content="Weigh the apples.", idempotency_key="next-cider", **common)
        await call(writer, "memory_write", key="next-pie", kind="task_state",
                   content="Blind-bake the crust.", idempotency_key="next-pie", evidence_ids=evidence_ids,
                   scope="task", scope_id="pie")
        correction = await call(writer, "memory_append", kind="user_correction",
                                content="Use grams instead.", idempotency_key="correction")
        await call(writer, "memory_write", key="units", kind="decision", content="Use grams instead.",
                   idempotency_key="units-v2", expected_version=1,
                   **{**common, "evidence_ids": [correction["event_id"]]})

    query = "What now?"
    async with Client(options, mode="legacy", read_timeout_seconds=15) as reader:
        search = await call(reader, "memory_context", query=query, task_id="cider")
        assert search["records"] == []
        resumed = await call(reader, "memory_context", mode="resume", query=query, task_id="cider")
        assert [item["content"] for item in resumed["records"]] == ["Weigh the apples.", "Use grams instead."]
        assert resumed["records"][1]["version"] == 2
        assert len(resumed["constraints"]) == 1
        historical = await call(reader, "memory_get", record_id=initial["record_id"], version=1)
        assert historical["content"] == "Use cups." and not historical["is_current"]
        pie = await call(reader, "memory_context", mode="resume", query=query, task_id="pie")
        assert [item["content"] for item in pie["records"]] == ["Blind-bake the crust."]
        tiny = await call(reader, "memory_context", mode="resume", query=query, task_id="cider", max_chars=256)
        assert tiny["constraints"] == tiny["records"] == []
        assert tiny["coverage"]["omitted_constraints"] == 1
        assert tiny["coverage"]["resume"]["omitted_by_budget"] == 2
    async with Client(parameters(tmp_path, "other-cookbook"), mode="legacy", read_timeout_seconds=15) as isolated:
        resumed = await call(isolated, "memory_context", mode="resume", query=query, task_id="cider")
        assert resumed["constraints"] == resumed["records"] == []


@asynccontextmanager
async def running_http(tmp_path: Path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    token = "synthetic-http-secret-" + "z" * 32
    environment = {**server_environment(), "MEMORY_BRIDGE_TOKEN": token}
    log_path = tmp_path / "server.stderr.log"
    with log_path.open("wb") as error_log:
        process = subprocess.Popen([
            sys.executable, "-m", "angelbot_memory_bridge", "serve", "--db", str(tmp_path / "http.sqlite3"),
            "--workspace", "http-test", "--transport", "streamable-http", "--port", str(port),
        ], cwd=tmp_path, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_log)
        base_url = f"http://127.0.0.1:{port}"
        try:
            async with httpx2.AsyncClient(timeout=0.5, trust_env=False) as probe:
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        raise AssertionError(f"HTTP process exited: {log_path.read_text(encoding='utf8')}")
                    try:
                        response = await probe.get(base_url + "/mcp")
                        if response.status_code == 401:
                            break
                    except httpx2.HTTPError:
                        pass
                    if time.monotonic() >= deadline:
                        raise AssertionError("HTTP server did not become ready")
                    await asyncio.sleep(0.05)
            yield base_url + "/mcp", token
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    assert token not in log_path.read_text(encoding="utf8")


@pytest.mark.anyio
async def test_real_authenticated_http_and_namespace_recall(tmp_path: Path) -> None:
    async with running_http(tmp_path) as (url, token):
        async with httpx2.AsyncClient(trust_env=False) as requests:
            assert (await requests.post(url, json={"method": "tools/list"})).status_code == 401
            assert (await requests.delete(url, headers={"Authorization": "Bearer invalid"})).status_code == 401
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False) as http:
            async with Client(streamable_http_client(url, http_client=http), mode="2026-07-28") as client:
                assert client.protocol_version == "2026-07-28"
                source = await client.call_tool("memory_append", {
                    "kind": "note", "content": "HTTP evidence is stored locally.", "idempotency_key": "http-source",
                })
                assert not source.is_error and source.structured_content is not None
                read = await client.call_tool("memory_read_event", {"event_id": source.structured_content["event_id"]})
                assert read.structured_content is not None
                assert read.structured_content["content"] == "HTTP evidence is stored locally."
