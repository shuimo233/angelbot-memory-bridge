# Memory Bridge

Memory Bridge is a standalone local MCP server for source evidence, decisions,
and task state. It keeps original events in SQLite and builds recall from
versioned records that cite those events. Codex, Claude, and other stdio MCP
clients can use it without running AngelBot or configuring a model on the server.
The project originated in [AngelBot](https://github.com/shuimo233/AngelBot).

## Start with what feels difficult

Tell your coding assistant what you were trying to do. For example, “I keep losing
my place in settings. Can you fix it?” The assistant can inspect the project, use
relevant decisions, and ask an everyday question if the answer changes the fix.
“It happens when I hit Save” can be enough to try a change and check that the same
setting stays visible after saving. If you already know the outcome you want, say
it directly. You can also ask for a suggestion or let the assistant choose a first draft.

The Codex marketplace plugin includes the `shape-coding-request` skill. For a
stdio client, the same workflow is available as the read-only `memory_shape_request`
tool. If your assistant misses it, ask it to use that tool with your original
description. The host model guides the conversation and uses its own project
tools to implement a change. Shaping a request does not save a memory.

## Install and connect

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Run the following
PowerShell commands from the cloned repository or the `plugin` directory of a packaged copy.

```powershell
$bridge = (Resolve-Path .).Path
$bridgeUv = (Get-Command uv -CommandType Application).Source
& $bridgeUv sync --locked --project "$bridge"
& $bridgeUv run --offline --no-sync --project "$bridge" angelbot-memory-bridge check
```

Run the registration command for your client in a terminal. Then start a new
conversation and check that `memory_context` is available.

```powershell
codex mcp add memory-bridge -- "$bridgeUv" run --offline --no-sync --project "$bridge" angelbot-memory-bridge serve
claude mcp add --transport stdio memory-bridge -- "$bridgeUv" run --offline --no-sync --project "$bridge" angelbot-memory-bridge serve
```

Any stdio client can launch the same command and arguments. Use absolute paths
for both the `uv` executable and `--project` when the client has a different PATH
or working directory. Stdout carries MCP messages during `serve`.

For Claude Desktop, merge the entry below into `claude_desktop_config.json` and
replace both example paths. Fully quit and reopen the desktop app to load the
change. [Claude Desktop configuration locations](https://py.sdk.modelcontextprotocol.io/get-started/real-host/#claude-desktop)

```json
{
  "mcpServers": {
    "memory-bridge": {
      "command": "C:\\Tools\\uv.exe",
      "args": [
        "run", "--offline", "--no-sync", "--project", "C:\\Tools\\memory-bridge",
        "angelbot-memory-bridge", "serve"
      ]
    }
  }
}
```

To install through a local Codex marketplace, run this from a repository checkout
and supply a new or empty destination.

```powershell
$bridgeMarketplace = Join-Path (Split-Path -Parent $PWD.Path) 'memory-bridge-marketplace'
python scripts/package_memory_bridge.py "$bridgeMarketplace"
codex plugin marketplace add "$bridgeMarketplace"
codex plugin add angelbot-memory-bridge@memory-bridge-local
```

First launch from the plugin can install locked dependencies. The packaged copy
contains source, manifests, dependency metadata, documentation, and the license.

## Storage and compatibility

The server binds one workspace at process startup. `--workspace` defaults to
`default` regardless of the client's current project or directory. Give each
project its own workspace name when you want separate recall. Add `--db` with an
absolute path to use a separate SQLite file. To share records between clients,
configure both clients with the same file and workspace. Workspace selection
restricts MCP calls and has no effect on operating-system access to the file.

The application-data path remains
`%LOCALAPPDATA%/AngelBotMemoryBridge/memory.sqlite3` on Windows and
`${XDG_DATA_HOME:-~/.local/share}/angelbot-memory-bridge/memory.sqlite3` otherwise.
To recover records from the 0.1.0 plugin, add `--workspace angelbot` after `serve`
in the client launch command or JSON argument list. If the earlier setup used
a custom workspace or `--db` path, pass those same values. Existing events,
versions, and idempotency keys remain usable. Only dedicated Memory Bridge
databases are accepted.

`check` opens or initializes the selected database and reports the workspace
revision. `rebuild` reconstructs the selected workspace's records and search
projection from its event log. Both commands accept `--db` and `--workspace`.
Keep the SQLite file private and back it up while clients are stopped. Revoked
records retain their evidence and historical versions.

## Use with a client

For a synthetic walkthrough, use a temporary database and set
`--workspace cookbook-demo`.

1. Call `memory_append` with `kind: "user_decision"`, `content: "Use metric units in recipes."`
   and `idempotency_key: "cookbook/units/source-v1"`. Save the returned `event_id`.
2. Call `memory_write` with `key: "recipe-units"`, `kind: "decision"`,
   `content: "Use metric units."`, `evidence_ids` containing that ID,
   `expected_version: 0`, and `idempotency_key: "cookbook/units/write-v1"`.
3. Call `memory_context` with `query: "recipe units"`. Use `memory_read_event` to
   verify the original evidence. `memory_search` looks up active records by
   keywords. `memory_get` reads the current record or a specified historical version.
4. To correct a remembered decision, append the user's words with `kind: "user_correction"`.
   Call `memory_write` with the recalled `record_id`, last observed `expected_version`,
   replacement content, the new event ID in `evidence_ids`, and a fresh idempotency key.
   The previous version and its source remain available. `memory_revoke` requires a current version, a reason,
   evidence, and its own idempotency key.

Every write needs a stable idempotency key. Retry the same mutation with the same
key and arguments. Changed arguments under the same key return a conflict.
Records can be a `constraint`, `decision`, `preference`, `task_state`, or `lesson`.
A task-scoped record needs `scope: "task"` and a `scope_id` matching the recall
`task_id`. Project and global records apply within the configured workspace.
Expiration timestamps require a time zone.

When a user explicitly picks up an existing project or task, call
`memory_context` with `{"mode":"resume","task_id":"cookbook-demo"}` using the
known task ID. Omit `task_id` to include only project and global scopes. Resume considers
applicable constraints first, then up to five current task states and five
current decisions. Within each kind, matching task-scoped records come first,
followed by priority and recency. This works even when the user's wording has
no overlap with the saved text. A call without `mode` still uses keyword search.
Read `coverage.resume` for records omitted by the selection limit or the
character budget. Historical versions remain available through `memory_get`.

Check `coverage.complete_constraints` when recalling context. If it is false,
page through `memory_list` using the returned kind, task, and revision, and
check each item's status. Restart pagination when its revision guard reports a
conflict. Memory responses are advisory. The client retains responsibility for
permission and execution decisions.

### Everyday conversation

The host decides when to call MCP tools. For a client that skips memory on a new
chat, add this reminder to its project or agent instructions.

> At the start of a conversation, call `memory_context` for this workspace. Use
> any returned communication preferences unless my current request says otherwise.

- **“From now on, give me the conclusion first.”** Append the user's words as evidence
  and call `memory_save_communication_preference` with the preference, a stable key,
  the evidence ID, and an idempotency key.
  On a new topic, call `memory_context`. Its `communication_preferences` includes
  up to three applicable habits, with task-specific ones first. Constraints have first claim on
  the character budget. Apply the habits quietly and follow the user's current request.
- **“Just for this one, walk me through it.”** Give a detailed answer for this turn.
  The saved preference still applies next time. If the user changes the standing preference,
  correct the existing record. If “change that” could refer to two decisions, ask which one.
- **“Choose the routine details yourself.”** This can be a standing communication
  preference. For example, pick a layout for a requested draft and show it. If the
  user asked to send it and two recipients fit, check the conversation first and
  ask which recipient only if it is still unclear. The host handles any required approval.
- **“Where did we agree to send our write-up?”** The client can pass up to four short
  alternative phrasings or keyword groups in `query_variants` alongside the user's query.
  Search runs locally on words in stored records. The default broad mode also returns partial
  matches. Use `memory_search` with `match_mode: "focused"` to require a whole group,
  then check the source before answering. An empty result can also mean the wording differs.

## Local HTTP

The same server accepts `serve --transport streamable-http --host 127.0.0.1 --port 8766`.
Set a random `MEMORY_BRIDGE_TOKEN` of at least 32 non-whitespace characters in
the server environment. Clients send `Authorization: Bearer <token>` to
`http://127.0.0.1:8766/mcp`. HTTP is loopback-only.

## Verify

From the repository root, install the test dependencies once and then run the
suite offline with synthetic data and isolated databases.

```powershell
uv sync --locked --extra test
uv run --offline --no-sync python -m pytest tests
```

[MIT license](LICENSE)
