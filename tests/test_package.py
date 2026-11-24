import json
import os
import subprocess

import pytest

from scripts import package_memory_bridge as builder


def test_marketplace_packages_the_discoverable_coding_request_skill(tmp_path):
    marketplace = builder.package(tmp_path / "marketplace")
    catalog = json.loads((marketplace / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    plugin = marketplace / catalog["plugins"][0]["source"]["path"]
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    skill = plugin / manifest["skills"] / "shape-coding-request" / "SKILL.md"
    assert skill.read_bytes() == (builder.PACKAGE / skill.relative_to(plugin)).read_bytes()


def test_package_omits_runtime_data_and_preserves_existing_destination(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "example.py").write_text("pass\n")
    (source / "src" / "unlisted.py").write_text("pass\n")
    (source / "src" / "private.sqlite3").write_bytes(b"private")
    (source / ".venv").mkdir()
    (source / ".venv" / "private.txt").write_text("private")
    monkeypatch.setattr(builder, "PACKAGE", source)
    monkeypatch.setattr(builder, "FILES", ("src/example.py",))
    target = builder.package(tmp_path / "marketplace")
    assert (target / "plugin" / "src" / "example.py").is_file()
    assert not (target / "plugin" / "src" / "unlisted.py").exists()
    assert not (target / "plugin" / "src" / "private.sqlite3").exists()
    assert not (target / "plugin" / ".venv").exists()
    with pytest.raises(ValueError, match="new or empty"):
        builder.package(target)
    assert (target / "plugin" / "src" / "example.py").read_text() == "pass\n"


def test_package_rejects_directory_link_outside_source(tmp_path, monkeypatch):
    source, outside = tmp_path / "source", tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "private.py").write_text("sensitive = True\n")
    linked = source / "src"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(linked), str(outside)], capture_output=True)
        if result.returncode:
            pytest.skip("Directory junction creation is unavailable in this test environment")
    else:
        linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(builder, "PACKAGE", source)
    monkeypatch.setattr(builder, "FILES", ("src/private.py",))
    try:
        with pytest.raises(ValueError, match="source tree|symbolic links"):
            builder.package(tmp_path / "package")
        assert not (tmp_path / "package").exists()
        assert (outside / "private.py").read_text() == "sensitive = True\n"
    finally:
        # Remove only this test's link, never its target.
        if os.name == "nt":
            os.rmdir(linked)
        else:
            linked.unlink()
