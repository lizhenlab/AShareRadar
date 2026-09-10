"""Local startup must preserve explicit configuration and never load credentials."""

from pathlib import Path

import pytest

from tools import run_local


def test_optional_key_file_does_not_enable_new_checkout(tmp_path: Path) -> None:
    environment: dict[str, str] = {}
    assert not run_local.configure_local_fuyao(environment, tmp_path)
    assert environment == {}


def test_local_defaults_reference_file_without_reading_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = tmp_path / "data" / "fuyao-api-key"
    key_file.parent.mkdir()
    key_file.write_text("synthetic-key")
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: pytest.fail("launcher read credential"))
    environment: dict[str, str] = {}
    assert run_local.configure_local_fuyao(environment, tmp_path)
    assert environment == {"ASHARE_RADAR_FUYAO_ENABLED": "true", "ASHARE_RADAR_FUYAO_API_KEY_FILE": str(key_file),
                           "ASHARE_RADAR_FUYAO_DOWNLOAD_HOSTS": "o.thsi.cn"}


def test_local_startup_preserves_explicit_disabling_and_hosts(tmp_path: Path) -> None:
    key_file = tmp_path / "data" / "fuyao-api-key"
    key_file.parent.mkdir()
    key_file.touch()
    environment = {"ASHARE_RADAR_FUYAO_ENABLED": "false", "ASHARE_RADAR_FUYAO_API_KEY_FILE": "/another/key",
                   "ASHARE_RADAR_FUYAO_DOWNLOAD_HOSTS": ""}
    expected = environment.copy()
    assert run_local.configure_local_fuyao(environment, tmp_path)
    assert environment == expected


def test_local_startup_ignores_symlink_credential(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    target = tmp_path / "target"
    target.touch()
    (tmp_path / "data" / "fuyao-api-key").symlink_to(target)
    environment: dict[str, str] = {}
    assert not run_local.configure_local_fuyao(environment, tmp_path)
    assert environment == {}


def test_runner_uses_one_local_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_local, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(run_local.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert run_local.main(["--port", "8012"]) == 0
    assert calls == [(("app.main:app",), {"host": "127.0.0.1", "port": 8012, "workers": 1, "timeout_graceful_shutdown": 5})]


@pytest.mark.parametrize("port", ["0", "65536"])
def test_runner_rejects_bad_port(port: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        run_local.main(["--port", port])
