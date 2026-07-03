from __future__ import annotations

from app.config import DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS, Settings


def test_settings_reads_environment_when_instantiated(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_RADAR_CORS_ALLOW_ORIGINS", "http://alpha.test, http://beta.test")
    monkeypatch.setenv("ASHARE_RADAR_FUTU_ENABLED", "yes")
    monkeypatch.setenv("ASHARE_RADAR_FUTU_HOST", "10.0.0.8")
    monkeypatch.setenv("ASHARE_RADAR_FUTU_PORT", "22222")
    monkeypatch.setenv("ASHARE_RADAR_SCHEDULER_QUOTE_INTERVAL_SECONDS", "47")
    monkeypatch.setenv("ASHARE_RADAR_TUSHARE_TOKEN", "new-token")

    settings = Settings()

    assert settings.cors_allow_origins == ("http://alpha.test", "http://beta.test")
    assert settings.futu_enabled is True
    assert settings.futu_host == "10.0.0.8"
    assert settings.futu_port == 22222
    assert settings.scheduler_quote_interval_seconds == 47
    assert settings.tushare_token == "new-token"


def test_settings_invalid_environment_values_fall_back_to_safe_defaults(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_RADAR_LLM_ENABLED", "maybe")
    monkeypatch.setenv("ASHARE_RADAR_LLM_TIMEOUT_SECONDS", "slow")
    monkeypatch.setenv("ASHARE_RADAR_FUTU_PORT", "0")
    monkeypatch.setenv("ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS", "many")
    monkeypatch.setenv("ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS", "-1")

    settings = Settings()

    assert settings.llm_enabled is True
    assert settings.llm_timeout_seconds == DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS
    assert settings.futu_port == 11111
    assert settings.max_quote_history_rows == 50000
    assert settings.scheduler_shutdown_timeout_seconds == 5.0


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
