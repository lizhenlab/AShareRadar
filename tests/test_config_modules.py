from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings


ROOT = Path(__file__).resolve().parents[1]


def test_operations_documents_every_ashare_radar_environment_variable() -> None:
    app_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "app").rglob("*.py")))
    operations_text = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")

    config_names = set(re.findall(r"ASHARE_RADAR_[A-Z0-9_]+", app_text))
    documented_names = set(re.findall(r"`(ASHARE_RADAR_[A-Z0-9_]+)`", operations_text))

    assert documented_names == config_names


def test_settings_reads_environment_when_instantiated(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_RADAR_CORS_ALLOW_ORIGINS", "http://alpha.test, http://beta.test")
    monkeypatch.setenv("ASHARE_RADAR_FUTU_ENABLED", "yes")
    monkeypatch.setenv("ASHARE_RADAR_FUTU_HOST", "10.0.0.8")
    monkeypatch.setenv("ASHARE_RADAR_FUTU_PORT", "22222")
    monkeypatch.setenv("ASHARE_RADAR_SCHEDULER_QUOTE_INTERVAL_SECONDS", "47")
    monkeypatch.setenv("ASHARE_RADAR_STOCK_POOL_PROVIDER_TIMEOUT_SECONDS", "75")
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_MIN_SH_COUNT", "1900")
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_MIN_SZ_COUNT", "2600")
    monkeypatch.setenv("ASHARE_RADAR_MARKET_SCAN_MIN_BJ_COUNT", "210")
    monkeypatch.setenv("ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS", "180")
    monkeypatch.setenv("ASHARE_RADAR_MAX_DAILY_KLINE_ROWS", "320")
    monkeypatch.setenv("ASHARE_RADAR_RUNTIME_MAINTENANCE_INTERVAL_SECONDS", "7200")
    monkeypatch.setenv("ASHARE_RADAR_MAX_RUNTIME_BACKUPS", "12")
    monkeypatch.setenv("ASHARE_RADAR_TUSHARE_TOKEN", "new-token")

    settings = Settings()

    assert settings.cors_allow_origins == ("http://alpha.test", "http://beta.test")
    assert settings.futu_enabled is True
    assert settings.futu_host == "10.0.0.8"
    assert settings.futu_port == 22222
    assert settings.scheduler_quote_interval_seconds == 47
    assert settings.stock_pool_provider_timeout_seconds == 75
    assert settings.market_scan_min_sh_count == 1900
    assert settings.market_scan_min_sz_count == 2600
    assert settings.market_scan_min_bj_count == 210
    assert settings.max_quote_history_rows == 180
    assert settings.max_daily_kline_rows == 320
    assert settings.runtime_maintenance_interval_seconds == 7200
    assert settings.max_runtime_backups == 12
    assert settings.tushare_token == "new-token"


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("ASHARE_RADAR_LLM_TIMEOUT_SECONDS", "slow", "必须是数字"),
        ("ASHARE_RADAR_FUTU_PORT", "0", "必须大于等于 1"),
        ("ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS", "many", "必须是整数"),
        ("ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS", "119", "必须大于等于 120"),
        ("ASHARE_RADAR_MAX_RUNTIME_BACKUPS", "1", "必须大于等于 2"),
        ("ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS", "-1", "必须大于等于 0.1"),
    ],
)
def test_settings_invalid_numeric_environment_values_fail_fast(
    monkeypatch,
    name: str,
    raw: str,
    message: str,
) -> None:
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=rf"{name} {message}"):
        Settings()


def test_settings_invalid_boolean_environment_value_reports_the_variable(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_RADAR_LLM_ENABLED", "maybe")

    with pytest.raises(ValueError, match="ASHARE_RADAR_LLM_ENABLED 必须是布尔值"):
        Settings()


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_settings_reject_non_finite_environment_floats(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("ASHARE_RADAR_LLM_TIMEOUT_SECONDS", raw)

    with pytest.raises(ValueError, match="ASHARE_RADAR_LLM_TIMEOUT_SECONDS 必须是有限数字"):
        Settings()


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_settings_reject_non_finite_explicit_floats(value: float) -> None:
    with pytest.raises(ValidationError, match="finite number"):
        Settings(request_timeout_seconds=value)


def test_settings_reject_market_scan_history_larger_than_fetch_limit() -> None:
    with pytest.raises(
        ValidationError,
        match="market_scan_min_history_rows 不能大于 market_scan_kline_limit",
    ):
        Settings(market_scan_min_history_rows=120, market_scan_kline_limit=60)


def test_settings_require_daily_retention_to_cover_the_scan_window() -> None:
    with pytest.raises(
        ValidationError,
        match="max_daily_kline_rows 不能小于 market_scan_kline_limit",
    ):
        Settings(market_scan_kline_limit=261, max_daily_kline_rows=260)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_quote_history_rows": 119}, "greater than or equal to 120"),
        ({"runtime_maintenance_interval_seconds": 59}, "greater than or equal to 60"),
        ({"max_runtime_backups": 1}, "greater than or equal to 2"),
    ],
)
def test_settings_reject_retention_values_below_safe_boundaries(overrides: dict[str, int], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(**overrides)


def test_settings_do_not_default_llm_endpoint_or_model(monkeypatch) -> None:
    monkeypatch.setattr("app.config._SHELL_ENV_VALUES", {})
    for name in (
        "ASHARE_RADAR_LLM_API_KEY",
        "ASHARE_RADAR_LLM_BASE_URL",
        "ASHARE_RADAR_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.llm_api_key is None
    assert settings.llm_base_url is None
    assert settings.llm_model is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.test/v1/", "https://example.test/v1"),
        ("https://example.test/v1///", "https://example.test/v1"),
        ("http://localhost:8000/v1/", "http://localhost:8000/v1"),
        ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000/v1"),
        ("http://[::1]:8000/v1/", "http://[::1]:8000/v1"),
    ],
)
def test_settings_normalize_secure_or_loopback_llm_base_urls(raw: str, expected: str) -> None:
    assert Settings(llm_base_url=raw).llm_base_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.test/v1",
        "http://localhost.example.test/v1",
        "ftp://example.test/v1",
        "https:///v1",
        "https://example.test/v1?api-version=1",
        "https://example.test/v1#fragment",
    ],
)
def test_settings_reject_insecure_or_non_absolute_llm_base_urls(raw: str) -> None:
    with pytest.raises(ValidationError, match="llm_base_url"):
        Settings(llm_base_url=raw)


def test_settings_reject_llm_base_url_userinfo_without_echoing_credentials() -> None:
    credential = "alice:private-password"

    with pytest.raises(ValidationError) as exc_info:
        Settings(llm_base_url=f"https://{credential}@example.test/v1")

    rendered = str(exc_info.value)
    assert "userinfo" in rendered
    assert credential not in rendered


def test_settings_validate_and_normalize_llm_base_url_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_RADAR_LLM_BASE_URL", "https://example.test/v1///")

    assert Settings().llm_base_url == "https://example.test/v1"


def test_settings_boolean_environment_values_are_explicit(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_RADAR_LLM_ENABLED", "off")
    monkeypatch.setenv("ASHARE_RADAR_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("ASHARE_RADAR_DEMO_PROVIDER_ENABLED", "true")

    settings = Settings()

    assert settings.llm_enabled is False
    assert settings.scheduler_enabled is False
    assert settings.demo_provider_enabled is True


def test_settings_keep_legacy_environment_aliases(monkeypatch) -> None:
    monkeypatch.setenv("FUTU_ENABLED", "yes")
    monkeypatch.setenv("SCHEDULER_QUOTE_INTERVAL_SECONDS", "47")
    monkeypatch.setenv("MAX_QUOTE_HISTORY_ROWS", "123")
    monkeypatch.setenv("TUSHARE_TOKEN", "legacy-token")

    settings = Settings()

    assert settings.futu_enabled is True
    assert settings.scheduler_quote_interval_seconds == 47
    assert settings.max_quote_history_rows == 123
    assert settings.tushare_token == "legacy-token"


def test_settings_prefer_new_environment_names_over_legacy_aliases(monkeypatch) -> None:
    monkeypatch.setenv("FUTU_PORT", "11111")
    monkeypatch.setenv("ASHARE_RADAR_FUTU_PORT", "22222")
    monkeypatch.setenv("TUSHARE_TOKEN", "legacy-token")
    monkeypatch.setenv("ASHARE_RADAR_TUSHARE_TOKEN", "new-token")

    settings = Settings()

    assert settings.futu_port == 22222
    assert settings.tushare_token == "new-token"


def test_cache_paths_are_project_relative_and_environment_overridable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ASHARE_RADAR_CACHE_PATH", raising=False)
    monkeypatch.delenv("CACHE_PATH", raising=False)

    assert Settings().cache_path == PROJECT_ROOT / "data" / "ashare_radar.sqlite3"
    assert Settings(cache_path=Path("tmp/explicit.sqlite3")).cache_path == PROJECT_ROOT / "tmp" / "explicit.sqlite3"

    monkeypatch.setenv("ASHARE_RADAR_CACHE_PATH", "var/env.sqlite3")

    assert Settings().cache_path == PROJECT_ROOT / "var" / "env.sqlite3"


def test_settings_repr_hides_secret_values() -> None:
    settings = Settings(tushare_token="tushare-secret", llm_api_key="llm-secret")

    rendered = repr(settings)

    assert "tushare-secret" not in rendered
    assert "llm-secret" not in rendered
