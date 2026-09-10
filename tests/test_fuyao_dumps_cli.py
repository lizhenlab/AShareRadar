from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from tools import sync_fuyao_history as cli


def test_status_explicit_root_is_offline_without_loading_credentials(tmp_path, monkeypatch, capsys):
    def forbidden():
        raise AssertionError("offline root must not inspect configuration")
    monkeypatch.setattr(cli, "get_settings", forbidden)
    assert cli.main(["status", "--root", str(tmp_path / "empty")]) == 0
    assert '"unavailable"' in capsys.readouterr().out


def test_sync_rejects_root_override_before_loading_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: (_ for _ in ()).throw(AssertionError("no config read")))
    assert cli.main(["full", "--root", str(tmp_path)]) == 2
    assert "持久预算" in capsys.readouterr().err


def test_export_requires_explicit_output(tmp_path, capsys):
    assert cli.main(["export", "--root", str(tmp_path)]) == 2
    assert "--output" in capsys.readouterr().err


def test_online_cli_reuses_project_budget_and_closes_client(tmp_path, monkeypatch):
    settings = Settings(cache_path=tmp_path / "cache.sqlite3", fuyao_enabled=False, fuyao_download_hosts=("files.example.com",))
    observed = {}
    class Client:
        def __init__(self, supplied, *, budget):
            observed.update(settings=supplied, budget_path=budget.repository.path)
        async def aclose(self):
            observed["closed"] = True
    async def sync(client, root, mode, **kwargs):
        observed.update(root=root, mode=mode)
        return SimpleNamespace(model_dump=lambda **kwargs: {"version": "sample"})
    monkeypatch.setattr(cli, "FuyaoClient", Client)
    monkeypatch.setattr(cli, "sync_market_dumps", sync)
    result = asyncio.run(cli._sync(settings, argparse.Namespace(operation="full", max_download_bytes=100)))
    assert result["status"] == "published" and observed["closed"]
    assert observed["settings"].fuyao_enabled is False
    assert observed["root"] == tmp_path / "cache.fuyao" / "history"
    assert observed["budget_path"] == tmp_path / "cache.fuyao" / "research.sqlite3"


def test_cli_local_failures_are_sanitized(monkeypatch, capsys):
    def fail(_args):
        raise OSError("private local context")
    monkeypatch.setattr(cli, "_execute", fail)
    assert cli.main(["verify", "--root", str(Path("/missing"))]) == 2
    assert "private" not in capsys.readouterr().err
