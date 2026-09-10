from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
import app.config_settings as config_module
from app.services.fuyao_contracts import FuyaoError, resolve_api_key


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    monkeypatch.setattr(config_module, "_SHELL_ENV_VALUES", {})
    for name in ("ASHARE_RADAR_FUYAO_ENABLED", "ASHARE_RADAR_FUYAO_API_KEY", "HITHINK_FINANCE_API_KEY",
                 "ASHARE_RADAR_FUYAO_API_KEY_FILE", "ASHARE_RADAR_FUYAO_REQUEST_INTERVAL_SECONDS",
                 "ASHARE_RADAR_FUYAO_DAILY_REQUEST_LIMIT", "ASHARE_RADAR_FUYAO_TIMEOUT_SECONDS", "ASHARE_RADAR_FUYAO_DOWNLOAD_HOSTS"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_disabled_and_never_guess_a_key_file():
    settings = Settings()
    assert not settings.fuyao_enabled
    assert settings.fuyao_api_key is None and settings.fuyao_api_key_file is None
    assert settings.fuyao_request_interval_seconds == 1
    assert settings.fuyao_daily_request_limit == 1000
    assert settings.fuyao_timeout_seconds == 20
    assert settings.fuyao_download_hosts == ()


def test_keys_only_use_explicit_environment_and_are_excluded(monkeypatch):
    secret = "synthetic-fixture-only-key"
    monkeypatch.setattr(config_module, "_SHELL_ENV_VALUES", {"ASHARE_RADAR_FUYAO_API_KEY": "not-allowed-shell-value"})
    assert Settings().fuyao_api_key is None
    monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "alias-fixture-key")
    assert Settings().fuyao_api_key.get_secret_value() == "alias-fixture-key"
    monkeypatch.setenv("ASHARE_RADAR_FUYAO_API_KEY", secret)
    settings = Settings()
    assert isinstance(settings.fuyao_api_key, SecretStr)
    assert settings.fuyao_api_key.get_secret_value() == secret
    assert secret not in repr(settings) + settings.model_dump_json() + repr(settings.model_dump())
    assert "fuyao_api_key" not in settings.model_dump()


def test_explicit_missing_key_file_is_not_read_during_settings_creation(monkeypatch, tmp_path):
    path = tmp_path / "not-created.key"
    monkeypatch.setenv("ASHARE_RADAR_FUYAO_API_KEY_FILE", str(path))
    settings = Settings()
    assert settings.fuyao_api_key_file == path
    assert not path.exists()
    assert str(path) not in repr(settings) + settings.model_dump_json()


@pytest.mark.parametrize(("field", "value"), [("fuyao_request_interval_seconds", -1), ("fuyao_request_interval_seconds", 61),
    ("fuyao_daily_request_limit", 0), ("fuyao_timeout_seconds", 0), ("fuyao_timeout_seconds", float("inf"))])
def test_invalid_limits_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_key_file_permissions_type_and_symlinks_are_checked(tmp_path):
    path = tmp_path / "test.key"
    path.write_text("synthetic-file-key\n", encoding="utf-8")
    path.chmod(0o600)
    assert resolve_api_key(None, path) == "synthetic-file-key"
    assert resolve_api_key(SecretStr("explicit-fixture-key"), path) == "explicit-fixture-key"
    path.chmod(0o644)
    with pytest.raises(FuyaoError, match="insecure_key_file"):
        resolve_api_key(None, path)
    path.chmod(0o600)
    alias = tmp_path / "alias.key"
    alias.symlink_to(path)
    with pytest.raises(FuyaoError, match="unreadable_key_file"):
        resolve_api_key(None, alias)
    with pytest.raises(FuyaoError):
        resolve_api_key(None, tmp_path)


@pytest.mark.parametrize("value", ["", "secret\nheader", "密钥", "x" * 4097])
def test_key_material_errors_never_echo_input(value):
    with pytest.raises(FuyaoError) as caught:
        resolve_api_key(SecretStr(value), None)
    assert str(caught.value) == "扶摇数据请求失败：missing_or_invalid_key"


def test_environment_file_keeps_symlink_identity(monkeypatch, tmp_path: Path):
    target = tmp_path / "target.key"
    target.write_text("synthetic-fixture-key")
    target.chmod(0o600)
    alias = tmp_path / "alias.key"
    alias.symlink_to(target)
    monkeypatch.setenv("ASHARE_RADAR_FUYAO_API_KEY_FILE", str(alias))
    settings = Settings()
    assert settings.fuyao_api_key_file == alias
    with pytest.raises(FuyaoError, match="unreadable_key_file"):
        resolve_api_key(None, settings.fuyao_api_key_file)


@pytest.mark.parametrize("host", ["https://files.example.com", "files.example.com/x", "files.example.com:443",
    "files.example.com?token=x", "*.example.com", "localhost", "127.0.0.1", "10.0.0.1", "[::1]",
    "download.local", "download.internal", "files.example.com.", "example..com", ""])
def test_download_hosts_reject_urls_ips_and_nonpublic_names(host):
    with pytest.raises(ValidationError):
        Settings(fuyao_download_hosts=(host,))


def test_download_hosts_use_exact_normalized_explicit_names(monkeypatch):
    monkeypatch.setenv("ASHARE_RADAR_FUYAO_DOWNLOAD_HOSTS", "Files.Example.com, cdn.example.com,files.example.com")
    assert Settings().fuyao_download_hosts == ("files.example.com", "cdn.example.com")
