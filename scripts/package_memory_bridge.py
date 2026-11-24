"""Build a clean local marketplace; never package virtualenvs or runtime data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

PACKAGE = Path(__file__).resolve().parents[1]
FILES = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "plugin.json",
    "mcp.json",
    ".codex-plugin/plugin.json",
    ".mcp.json",
    "skills/shape-coding-request/SKILL.md",
    "src/angelbot_memory_bridge/__init__.py",
    "src/angelbot_memory_bridge/__main__.py",
    "src/angelbot_memory_bridge/server.py",
    "src/angelbot_memory_bridge/store.py",
)


def package(destination: Path) -> Path:
    destination = destination.resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Destination must be new or empty; existing plugin files are preserved")

    root = PACKAGE.resolve()
    for relative in FILES:
        source = PACKAGE / relative
        if not source.resolve().is_relative_to(root) or any(
            (root / path).is_symlink() for path in (Path(relative), *Path(relative).parents)
        ):
            raise ValueError("Plugin source must stay inside its source tree without symbolic links")
        if not source.is_file():
            raise ValueError(f"Incomplete plugin source: {relative}")

    plugin = destination / "plugin"
    for relative in FILES:
        output = plugin / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACKAGE / relative, output)
    catalog = destination / ".agents" / "plugins" / "marketplace.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(json.dumps({
        "name": "memory-bridge-local",
        "interface": {"displayName": "Memory Bridge"},
        "plugins": [{
            "name": "angelbot-memory-bridge",
            "source": {"source": "local", "path": "./plugin"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        print(package(args.destination))
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
