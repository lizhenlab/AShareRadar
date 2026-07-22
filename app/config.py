from functools import lru_cache
import math
import os
from pathlib import Path
import re
import shlex
import stat
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


LLM_SHELL_ENV_PATH = Path.home() / ".zshrc"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_PATH = PROJECT_ROOT / "data" / "ashare_radar.sqlite3"
CACHE_PATH_ENV_NAME = "ASHARE_RADAR_CACHE_PATH"
LLM_SHELL_ENV_NAMES = {
    "ASHARE_RADAR_LLM_ENABLED",
    "ASHARE_RADAR_LLM_API_KEY",
    "ASHARE_RADAR_LLM_BASE_URL",
    "ASHARE_RADAR_LLM_MODEL",
    "ASHARE_RADAR_LLM_TIMEOUT_SECONDS",
}
LLM_SHELL_SECRET_ENV_NAMES = {"ASHARE_RADAR_LLM_API_KEY"}
DEFAULT_CORS_ALLOW_ORIGINS = ("http://127.0.0.1:8010", "http://localhost:8010")
DEFAULT_ASHARE_RADAR_LLM_ENABLED = True
DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS = 30.0
MIN_QUOTE_HISTORY_RETENTION_ROWS = 120
MIN_RUNTIME_BACKUP_COUNT = 2
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
_LLM_HTTP_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SHELL_BLOCK_WORD_RE = re.compile(r"(?:^|[;&|])\s*(if|for|while|until|case|select|repeat|fi|done|esac)\b")
_SHELL_BLOCK_OPENERS = frozenset({"if", "for", "while", "until", "case", "select", "repeat"})


def _env_tuple(name: str, default: tuple[str, ...], *, aliases: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _env_text(name, aliases=aliases)
    if not raw:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


def _env_text(name: str, default: str | None = None, *, aliases: tuple[str, ...] = ()) -> str | None:
    raw = _first_env_value(name, aliases)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _env_bool(name: str, default: bool, *, aliases: tuple[str, ...] = ()) -> bool:
    raw = _env_text(name, aliases=aliases)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUE_ENV_VALUES:
        return True
    if value in FALSE_ENV_VALUES:
        return False
    raise ValueError(f"{name} 必须是布尔值，支持 1/0、true/false、yes/no 或 on/off")


def env_bool(name: str, default: bool, *, aliases: tuple[str, ...] = ()) -> bool:
    return _env_bool(name, default, aliases=aliases)


def _env_int(name: str, default: int, *, minimum: int | None = None, aliases: tuple[str, ...] = ()) -> int:
    raw = _env_text(name, aliases=aliases)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是整数") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, aliases: tuple[str, ...] = ()) -> float:
    raw = _env_text(name, aliases=aliases)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是数字") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限数字")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return value


def resolve_project_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    return resolved.resolve()


def _env_path(name: str, default: Path, *, aliases: tuple[str, ...] = ()) -> Path:
    raw = _env_text(name, aliases=aliases)
    return resolve_project_path(raw) if raw is not None else default


def _first_env_value(name: str, aliases: tuple[str, ...]) -> str | None:
    for candidate in (name, *aliases):
        raw = os.getenv(candidate)
        if raw is not None:
            return raw
    for candidate in (name, *aliases):
        raw = _SHELL_ENV_VALUES.get(candidate)
        if raw is not None:
            return raw
    return None


def _load_shell_env(path: Path, names: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    group_depth = 0
    block_depth = 0
    quote: str | None = None
    continued = False
    heredocs: list[tuple[str, bool]] = []
    for line in _shell_env_lines(path):
        if heredocs:
            if _shell_heredoc_closed(line, heredocs[0]):
                heredocs.pop(0)
            continue
        if group_depth == 0 and block_depth == 0 and quote is None and not continued:
            values.update(_shell_env_line_values(line, names))
        control_text, quote = _shell_control_text(line, quote)
        group_depth, block_depth = _shell_nesting_after_control_text(control_text, group_depth, block_depth)
        heredocs.extend(_shell_heredoc_delimiters(line))
        continued = quote is not None or _shell_command_continues(line, control_text)
    _validate_shell_secret_permissions(path, values)
    return values


def _validate_shell_secret_permissions(path: Path, values: dict[str, str]) -> None:
    if not any(values.get(name) for name in LLM_SHELL_SECRET_ENV_NAMES):
        return
    try:
        metadata = path.stat()
    except OSError:
        raise ValueError("无法验证包含 LLM API Key 的 shell 配置文件权限") from None
    owned_by_current_user = not hasattr(os, "getuid") or metadata.st_uid == os.getuid()
    private_mode = stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    if not stat.S_ISREG(metadata.st_mode) or not owned_by_current_user or not private_mode:
        raise ValueError("包含 LLM API Key 的 shell 配置文件权限过宽，请设置为仅当前用户可读写（chmod 600）")


def _shell_env_lines(path: Path) -> tuple[str, ...]:
    try:
        return tuple(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return ()


def _shell_env_line_values(line: str, names: set[str]) -> dict[str, str]:
    parts = _shell_env_words(line)
    if len(parts) == 1:
        part = parts[0]
    elif len(parts) == 2 and parts[0] == "export":
        part = parts[1]
    else:
        return {}
    assignment = _shell_env_assignment(part, names)
    return {assignment[0]: assignment[1]} if assignment is not None else {}


def _shell_env_words(line: str) -> tuple[str, ...]:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return tuple(lexer)
    except ValueError:
        return ()


def _shell_env_assignment(part: str, names: set[str]) -> tuple[str, str] | None:
    if "=" not in part:
        return None
    key, value = part.split("=", 1)
    stripped = value.strip()
    if key not in names or not stripped or any(marker in stripped for marker in ("$(", "`", "<(", ">(")):
        return None
    return key, stripped


def _shell_nesting_after_control_text(control_text: str, group_depth: int, block_depth: int) -> tuple[int, int]:
    for char in control_text:
        if char in "({":
            group_depth += 1
        elif char in ")}":
            group_depth = max(0, group_depth - 1)
    for match in _SHELL_BLOCK_WORD_RE.finditer(control_text):
        if match.group(1) in _SHELL_BLOCK_OPENERS:
            block_depth += 1
        else:
            block_depth = max(0, block_depth - 1)
    return group_depth, block_depth


def _shell_control_text(line: str, quote: str | None) -> tuple[str, str | None]:
    output: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            output.append(" ")
            escaped = False
        elif char == "\\" and quote != "'":
            output.append(" ")
            escaped = True
        elif quote is not None:
            output.append(" ")
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            output.append(" ")
            quote = char
        elif char == "#":
            break
        else:
            output.append(char)
    return "".join(output), quote


def _shell_heredoc_delimiters(line: str) -> tuple[tuple[str, bool], ...]:
    parts = _shell_env_words(line)
    delimiters: list[tuple[str, bool]] = []
    for index, part in enumerate(parts[:-1]):
        if part != "<<":
            continue
        raw = parts[index + 1]
        strip_tabs = raw.startswith("-")
        delimiter = raw.removeprefix("-") if strip_tabs else raw
        if delimiter:
            delimiters.append((delimiter, strip_tabs))
    return tuple(delimiters)


def _shell_heredoc_closed(line: str, heredoc: tuple[str, bool]) -> bool:
    delimiter, strip_tabs = heredoc
    candidate = line.lstrip("\t") if strip_tabs else line
    return candidate == delimiter


def _shell_command_continues(line: str, control_text: str) -> bool:
    trailing_backslashes = len(line) - len(line.rstrip("\\"))
    if trailing_backslashes % 2 == 1:
        return True
    return control_text.rstrip().endswith(("&&", "||", "|", "|&"))


def _normalized_llm_base_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    parsed, host = _parse_llm_base_url(text)
    scheme = parsed.scheme.casefold()
    _validate_llm_url_policy(parsed, scheme, host)
    return urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _parse_llm_base_url(text: str) -> tuple[SplitResult, str]:
    if any(char.isspace() for char in text):
        raise ValueError("llm_base_url 必须是绝对 HTTP(S) URL")
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        parsed.port
    except ValueError:
        raise ValueError("llm_base_url 必须是合法的绝对 HTTP(S) URL") from None
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError("llm_base_url 不允许包含 userinfo")
    if host is None:
        raise ValueError("llm_base_url 必须是绝对 HTTP(S) URL")
    return parsed, host


def _validate_llm_url_policy(parsed: SplitResult, scheme: str, host: str) -> None:
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("llm_base_url 必须是绝对 HTTP(S) URL")
    if scheme == "http" and host.casefold() not in _LLM_HTTP_LOOPBACK_HOSTS:
        raise ValueError("llm_base_url 必须使用 HTTPS，只有 localhost、127.0.0.1 和 [::1] 可使用 HTTP")
    if parsed.query or parsed.fragment:
        raise ValueError("llm_base_url 不允许包含查询参数或片段")


_SHELL_ENV_VALUES = _load_shell_env(LLM_SHELL_ENV_PATH, LLM_SHELL_ENV_NAMES)


class Settings(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, hide_input_in_errors=True, validate_default=True)

    app_name: str = "AShareRadar"
    cors_allow_origins: tuple[str, ...] = Field(
        default_factory=lambda: _env_tuple(
            "ASHARE_RADAR_CORS_ALLOW_ORIGINS",
            DEFAULT_CORS_ALLOW_ORIGINS,
            aliases=("CORS_ALLOW_ORIGINS",),
        )
    )
    data_provider: str = "datahub"
    quote_provider_priority: tuple[str, ...] = ("tencent", "akshare")
    kline_provider_priority: tuple[str, ...] = ("tencent", "akshare", "baostock")
    minute_provider_priority: tuple[str, ...] = ("futu", "akshare")
    stock_provider_priority: tuple[str, ...] = ("akshare", "tushare", "baostock", "local")
    plate_provider_priority: tuple[str, ...] = ("akshare", "local")
    cache_path: Path = Field(default_factory=lambda: _env_path(CACHE_PATH_ENV_NAME, DEFAULT_CACHE_PATH, aliases=("CACHE_PATH",)))
    demo_provider_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_DEMO_PROVIDER_ENABLED", False, aliases=("DEMO_PROVIDER_ENABLED",)))
    quote_cache_seconds: int = 8
    kline_cache_seconds: int = 60 * 60 * 6
    minute_kline_cache_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MINUTE_KLINE_CACHE_SECONDS",
            60,
            minimum=1,
            aliases=("MINUTE_KLINE_CACHE_SECONDS",),
        )
    )
    stock_pool_cache_seconds: int = 60 * 60 * 24
    stock_pool_authoritative_min_count: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_STOCK_POOL_AUTHORITATIVE_MIN_COUNT",
            1000,
            minimum=1,
            aliases=("STOCK_POOL_AUTHORITATIVE_MIN_COUNT",),
        )
    )
    plate_rank_cache_seconds: int = 60 * 10
    stock_concept_cache_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_STOCK_CONCEPT_CACHE_SECONDS",
            60 * 60 * 6,
            minimum=1,
            aliases=("STOCK_CONCEPT_CACHE_SECONDS",),
        )
    )
    tushare_token: str | None = Field(repr=False, default_factory=lambda: _env_text("ASHARE_RADAR_TUSHARE_TOKEN", aliases=("TUSHARE_TOKEN",)))
    futu_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_FUTU_ENABLED", False, aliases=("FUTU_ENABLED",)))
    futu_host: str = Field(default_factory=lambda: str(_env_text("ASHARE_RADAR_FUTU_HOST", "127.0.0.1", aliases=("FUTU_HOST",))))
    futu_port: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_FUTU_PORT", 11111, minimum=1, aliases=("FUTU_PORT",)))
    quote_refresh_seconds: int = 3
    request_timeout_seconds: float = 8.0
    provider_call_timeout_seconds: float = 8.0
    stock_pool_provider_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_STOCK_POOL_PROVIDER_TIMEOUT_SECONDS",
            60.0,
            minimum=1.0,
        ),
        le=300,
    )
    workbench_optional_timeout_seconds: float = 1.5
    provider_failure_cooldown_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_PROVIDER_FAILURE_COOLDOWN_SECONDS",
            90,
            minimum=1,
            aliases=("PROVIDER_FAILURE_COOLDOWN_SECONDS",),
        )
    )
    llm_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_LLM_ENABLED", DEFAULT_ASHARE_RADAR_LLM_ENABLED))
    llm_api_key: str | None = Field(
        default_factory=lambda: _env_text("ASHARE_RADAR_LLM_API_KEY"),
        repr=False,
    )
    llm_base_url: str | None = Field(default_factory=lambda: _env_text("ASHARE_RADAR_LLM_BASE_URL"))
    llm_model: str | None = Field(default_factory=lambda: _env_text("ASHARE_RADAR_LLM_MODEL"))
    llm_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_LLM_TIMEOUT_SECONDS",
            DEFAULT_ASHARE_RADAR_LLM_TIMEOUT_SECONDS,
            minimum=0.1,
        )
    )
    scheduler_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_SCHEDULER_ENABLED", True, aliases=("SCHEDULER_ENABLED",)))
    scheduler_quote_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_QUOTE_INTERVAL_SECONDS",
            30,
            minimum=1,
            aliases=("SCHEDULER_QUOTE_INTERVAL_SECONDS",),
        )
    )
    scheduler_kline_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_KLINE_INTERVAL_SECONDS",
            900,
            minimum=1,
            aliases=("SCHEDULER_KLINE_INTERVAL_SECONDS",),
        )
    )
    scheduler_plate_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_PLATE_INTERVAL_SECONDS",
            300,
            minimum=1,
            aliases=("SCHEDULER_PLATE_INTERVAL_SECONDS",),
        )
    )
    scheduler_health_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_HEALTH_INTERVAL_SECONDS",
            45,
            minimum=1,
            aliases=("SCHEDULER_HEALTH_INTERVAL_SECONDS",),
        )
    )
    scheduler_kline_symbols_limit: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_SCHEDULER_KLINE_SYMBOLS_LIMIT",
            5,
            minimum=1,
            aliases=("SCHEDULER_KLINE_SYMBOLS_LIMIT",),
        )
    )
    scheduler_shutdown_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS",
            5.0,
            minimum=0.1,
            aliases=("SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS",),
        )
    )
    market_scan_auto_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_RADAR_MARKET_SCAN_AUTO_ENABLED", False))
    market_scan_schedule_hour: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_SCHEDULE_HOUR", 16, minimum=0),
        le=23,
    )
    market_scan_schedule_minute: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_SCHEDULE_MINUTE", 30, minimum=0),
        le=59,
    )
    market_scan_batch_size: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_BATCH_SIZE", 50, minimum=1),
        le=500,
    )
    market_scan_concurrency: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_CONCURRENCY", 5, minimum=1),
        le=32,
    )
    market_scan_kline_limit: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_KLINE_LIMIT", 260, minimum=60),
        le=1000,
    )
    market_scan_min_history_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_HISTORY_ROWS", 60, minimum=60),
        le=260,
    )
    market_scan_min_data_quality_score: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE", 50, minimum=0),
        le=100,
    )
    market_scan_min_universe_count: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_UNIVERSE_COUNT", 4000, minimum=1))
    market_scan_min_sh_count: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_SH_COUNT", 1800, minimum=1))
    market_scan_min_sz_count: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_SZ_COUNT", 2500, minimum=1))
    market_scan_min_bj_count: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_MIN_BJ_COUNT", 200, minimum=1))
    market_scan_symbol_timeout_seconds: float = Field(
        default_factory=lambda: _env_float("ASHARE_RADAR_MARKET_SCAN_SYMBOL_TIMEOUT_SECONDS", 30.0, minimum=0.1),
        le=300,
    )
    market_scan_quote_batch_timeout_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_MARKET_SCAN_QUOTE_BATCH_TIMEOUT_SECONDS",
            60.0,
            minimum=0.1,
        ),
        le=600,
    )
    market_scan_retry_attempts: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_RETRY_ATTEMPTS", 2, minimum=1),
        le=5,
    )
    market_scan_retry_backoff_seconds: float = Field(
        default_factory=lambda: _env_float("ASHARE_RADAR_MARKET_SCAN_RETRY_BACKOFF_SECONDS", 1.0, minimum=0),
        le=30,
    )
    market_scan_batch_retry_attempts: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_BATCH_RETRY_ATTEMPTS", 3, minimum=1),
        le=5,
    )
    market_scan_provider_wait_budget_seconds: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_MARKET_SCAN_PROVIDER_WAIT_BUDGET_SECONDS",
            120.0,
            minimum=0,
        ),
        le=600,
    )
    market_scan_new_stock_days: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MARKET_SCAN_NEW_STOCK_DAYS", 120, minimum=1),
        le=730,
    )
    max_quote_history_rows: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS",
            MIN_QUOTE_HISTORY_RETENTION_ROWS,
            minimum=MIN_QUOTE_HISTORY_RETENTION_ROWS,
            aliases=("MAX_QUOTE_HISTORY_ROWS",),
        ),
        ge=MIN_QUOTE_HISTORY_RETENTION_ROWS,
        le=50000,
    )
    max_daily_kline_rows: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MAX_DAILY_KLINE_ROWS",
            260,
            minimum=60,
            aliases=("MAX_DAILY_KLINE_ROWS",),
        ),
        ge=60,
        le=5000,
    )
    max_minute_kline_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_MINUTE_KLINE_ROWS", 20000, minimum=1, aliases=("MAX_MINUTE_KLINE_ROWS",))
    )
    max_stock_concept_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_STOCK_CONCEPT_ROWS", 20000, minimum=1, aliases=("MAX_STOCK_CONCEPT_ROWS",))
    )
    max_task_run_rows: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MAX_TASK_RUN_ROWS", 2000, minimum=1, aliases=("MAX_TASK_RUN_ROWS",)))
    max_market_scan_runs: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MAX_MARKET_SCAN_RUNS", 30, minimum=1))
    max_monitor_event_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_MONITOR_EVENT_ROWS", 3000, minimum=1, aliases=("MAX_MONITOR_EVENT_ROWS",))
    )
    max_cache_event_rows: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MAX_CACHE_EVENT_ROWS", 5000, minimum=1, aliases=("MAX_CACHE_EVENT_ROWS",)))
    max_alert_event_rows: int = Field(default_factory=lambda: _env_int("ASHARE_RADAR_MAX_ALERT_EVENT_ROWS", 5000, minimum=1, aliases=("MAX_ALERT_EVENT_ROWS",)))
    max_advice_history_rows: int = Field(
        default_factory=lambda: _env_int("ASHARE_RADAR_MAX_ADVICE_HISTORY_ROWS", 20000, minimum=1, aliases=("MAX_ADVICE_HISTORY_ROWS",))
    )
    max_database_size_mb: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MAX_DATABASE_SIZE_MB",
            512,
            minimum=16,
        )
    )
    runtime_maintenance_interval_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_RUNTIME_MAINTENANCE_INTERVAL_SECONDS",
            60 * 60,
            minimum=60,
        ),
        ge=60,
        le=7 * 24 * 60 * 60,
    )
    max_runtime_backups: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_MAX_RUNTIME_BACKUPS",
            10,
            minimum=MIN_RUNTIME_BACKUP_COUNT,
        ),
        ge=MIN_RUNTIME_BACKUP_COUNT,
        le=100,
    )
    advice_history_dedupe_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_ADVICE_HISTORY_DEDUPE_SECONDS",
            180,
            minimum=0,
            aliases=("ADVICE_HISTORY_DEDUPE_SECONDS",),
        )
    )
    quote_stale_warning_seconds: int = Field(
        default_factory=lambda: _env_int(
            "ASHARE_RADAR_QUOTE_STALE_WARNING_SECONDS",
            900,
            minimum=1,
            aliases=("QUOTE_STALE_WARNING_SECONDS",),
        )
    )
    quote_consistency_warning_pct: float = Field(
        default_factory=lambda: _env_float(
            "ASHARE_RADAR_QUOTE_CONSISTENCY_WARNING_PCT",
            1.0,
            minimum=0.0,
            aliases=("QUOTE_CONSISTENCY_WARNING_PCT",),
        )
    )
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

    @field_validator("cache_path")
    @classmethod
    def _resolve_cache_path(cls, value: Path) -> Path:
        return resolve_project_path(value)

    @field_validator("llm_base_url")
    @classmethod
    def _validate_llm_base_url(cls, value: str | None) -> str | None:
        return _normalized_llm_base_url(value)

    @model_validator(mode="after")
    def _validate_market_scan_limits(self) -> "Settings":
        if self.market_scan_min_history_rows > self.market_scan_kline_limit:
            raise ValueError("market_scan_min_history_rows 不能大于 market_scan_kline_limit")
        if self.max_daily_kline_rows < self.market_scan_kline_limit:
            raise ValueError("max_daily_kline_rows 不能小于 market_scan_kline_limit")
        if self.market_scan_auto_enabled and not self.scheduler_enabled:
            raise ValueError("market_scan_auto_enabled 开启时必须同时开启 scheduler_enabled")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
