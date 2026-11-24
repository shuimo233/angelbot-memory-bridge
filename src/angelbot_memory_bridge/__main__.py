"""Command line entrypoint; stdout is reserved for MCP in stdio mode."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .server import create_http_app, create_server, require_loopback, validate_http_token
from .store import MemoryBridgeError, MemoryStore, validate_workspace


def default_db_path() -> Path:
    """Use a dedicated application-data directory, independent of the project/CWD."""
    if os.name == "nt":
        base = Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else Path.home() / "AppData" / "Local"
        if not base.is_absolute():
            base = Path.home() / "AppData" / "Local"
        return base / "AngelBotMemoryBridge" / "memory.sqlite3"
    base = Path(os.environ["XDG_DATA_HOME"]) if os.environ.get("XDG_DATA_HOME") else Path.home() / ".local" / "share"
    if not base.is_absolute():
        base = Path.home() / ".local" / "share"
    return base / "angelbot-memory-bridge" / "memory.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="angelbot-memory-bridge")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "check", "rebuild"):
        command = commands.add_parser(name)
        command.add_argument("--db", type=Path, help="Dedicated SQLite path (default: Memory Bridge application-data directory)")
        command.add_argument("--workspace", default="default", help="Namespace fixed for this process (default: default)")
        if name == "serve":
            command.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8766)
    arguments = parser.parse_args(argv)
    if arguments.db is None:
        arguments.db = default_db_path()
    try:
        arguments.workspace = validate_workspace(arguments.workspace)
        if arguments.command == "serve":
            if arguments.transport == "stdio":
                create_server(arguments.db, arguments.workspace).run(transport="stdio")
            else:
                require_loopback(arguments.host)
                token = validate_http_token(os.environ.get("MEMORY_BRIDGE_TOKEN"))
                if not 0 <= arguments.port <= 65535:
                    raise ValueError("port must be between 0 and 65535")
                import uvicorn

                app = create_http_app(create_server(arguments.db, arguments.workspace), token, arguments.host)
                uvicorn.run(app, host=arguments.host, port=arguments.port, access_log=False, log_level="warning")
        elif arguments.command == "check":
            with MemoryStore(arguments.db) as store:
                result = store.list_records(arguments.workspace, limit=1)
            print(json.dumps({"ok": True, "workspace": arguments.workspace, "revision": result["revision"]}))
        else:
            with MemoryStore(arguments.db) as store:
                result = store.rebuild(arguments.workspace)
            print(json.dumps({"ok": True, "workspace": arguments.workspace, "result": result}))
    except (MemoryBridgeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
