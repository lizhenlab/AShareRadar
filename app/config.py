from functools import lru_cache
import os
from pathlib import Path
import shlex
from pydantic import BaseModel


LLM_SHELL_ENV_PATH = Path.home() / ".zshrc"
LLM_SHELL_ENV_NAMES = {
    "ASHARE_RADAR_LLM_ENABLED",
    "ASHARE_RADAR_LLM_API_KEY",
    "ASHARE_RADAR_LLM_BASE_URL",
    "ASHARE_RADAR_LLM_MODEL",
    "ASHARE_RADAR_LLM_TIMEOUT_SECONDS",
}
DEFAULT_ASHARE_RADAR_LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_ASHARE_RADAR_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_ASHARE_RADAR_LLM_ENABLED = True
DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS = 12.0


def _env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


def _env_text(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        raw = _SHELL_ENV_VALUES.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_text(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_shell_env(path: Path, names: set[str]) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not parts:
            continue
        if parts[0] == "export":
            parts = parts[1:]
        for part in parts:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key in names and value.strip():
                values[key] = value.strip()
    return values


_SHELL_ENV_VALUES = _load_shell_env(LLM_SHELL_ENV_PATH, LLM_SHELL_ENV_NAMES)


class Settings(BaseModel):
    app_name: str = "AShareRadar"
    cors_allow_origins: tuple[str, ...] = _env_tuple(
        "CORS_ALLOW_ORIGINS",
        ("http://127.0.0.1:8010", "http://localhost:8010"),
    )
    data_provider: str = "datahub"
    quote_provider_priority: tuple[str, ...] = ("tencent", "akshare")
    kline_provider_priority: tuple[str, ...] = ("tencent", "akshare", "baostock")
    minute_provider_priority: tuple[str, ...] = ("futu", "akshare")
    stock_provider_priority: tuple[str, ...] = ("akshare", "tushare", "baostock", "local")
    plate_provider_priority: tuple[str, ...] = ("akshare", "local")
    cache_path: Path = Path("data/ashare_radar.sqlite3")
    demo_provider_enabled: bool = os.getenv("DEMO_PROVIDER_ENABLED", "0") == "1"
    quote_cache_seconds: int = 8
    kline_cache_seconds: int = 60 * 60 * 6
    minute_kline_cache_seconds: int = int(os.getenv("MINUTE_KLINE_CACHE_SECONDS", "60"))
    stock_pool_cache_seconds: int = 60 * 60 * 24
    stock_pool_authoritative_min_count: int = int(os.getenv("STOCK_POOL_AUTHORITATIVE_MIN_COUNT", "1000"))
    plate_rank_cache_seconds: int = 60 * 10
    stock_concept_cache_seconds: int = int(os.getenv("STOCK_CONCEPT_CACHE_SECONDS", str(60 * 60 * 6)))
    futu_enabled: bool = os.getenv("FUTU_ENABLED", "0") == "1"
    futu_host: str = os.getenv("FUTU_HOST", "127.0.0.1")
    futu_port: int = int(os.getenv("FUTU_PORT", "11111"))
    quote_refresh_seconds: int = 3
    request_timeout_seconds: float = 8.0
    provider_call_timeout_seconds: float = 8.0
    provider_failure_cooldown_seconds: int = int(os.getenv("PROVIDER_FAILURE_COOLDOWN_SECONDS", "90"))
    llm_enabled: bool = _env_bool("ASHARE_RADAR_LLM_ENABLED", DEFAULT_ASHARE_RADAR_LLM_ENABLED)
    llm_api_key: str | None = _env_text("ASHARE_RADAR_LLM_API_KEY")
    llm_base_url: str = str(_env_text("ASHARE_RADAR_LLM_BASE_URL", DEFAULT_ASHARE_RADAR_LLM_BASE_URL))
    llm_model: str = str(_env_text("ASHARE_RADAR_LLM_MODEL", DEFAULT_ASHARE_RADAR_LLM_MODEL))
    llm_timeout_seconds: float = float(_env_text("ASHARE_RADAR_LLM_TIMEOUT_SECONDS", str(DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS)))
    scheduler_enabled: bool = os.getenv("SCHEDULER_ENABLED", "1") == "1"
    scheduler_quote_interval_seconds: int = int(os.getenv("SCHEDULER_QUOTE_INTERVAL_SECONDS", "30"))
    scheduler_kline_interval_seconds: int = int(os.getenv("SCHEDULER_KLINE_INTERVAL_SECONDS", "900"))
    scheduler_plate_interval_seconds: int = int(os.getenv("SCHEDULER_PLATE_INTERVAL_SECONDS", "300"))
    scheduler_health_interval_seconds: int = int(os.getenv("SCHEDULER_HEALTH_INTERVAL_SECONDS", "45"))
    scheduler_kline_symbols_limit: int = int(os.getenv("SCHEDULER_KLINE_SYMBOLS_LIMIT", "5"))
    scheduler_shutdown_timeout_seconds: float = float(os.getenv("SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS", "5"))
    max_quote_history_rows: int = int(os.getenv("MAX_QUOTE_HISTORY_ROWS", "50000"))
    max_minute_kline_rows: int = int(os.getenv("MAX_MINUTE_KLINE_ROWS", "20000"))
    max_stock_concept_rows: int = int(os.getenv("MAX_STOCK_CONCEPT_ROWS", "20000"))
    max_task_run_rows: int = int(os.getenv("MAX_TASK_RUN_ROWS", "2000"))
    max_monitor_event_rows: int = int(os.getenv("MAX_MONITOR_EVENT_ROWS", "3000"))
    max_advice_history_rows: int = int(os.getenv("MAX_ADVICE_HISTORY_ROWS", "20000"))
    advice_history_dedupe_seconds: int = int(os.getenv("ADVICE_HISTORY_DEDUPE_SECONDS", "180"))
    quote_stale_warning_seconds: int = int(os.getenv("QUOTE_STALE_WARNING_SECONDS", "900"))
    quote_consistency_warning_pct: float = float(os.getenv("QUOTE_CONSISTENCY_WARNING_PCT", "1.0"))
    seed_symbols: tuple[str, ...] = (
        "600519",
        "000001",
        "300750",
        "601318",
        "000858",
        "002594",
        "600036",
        "600900",
        "000333",
        "002475",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
