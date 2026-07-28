from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.utils.clock import market_now
from tools.provider_canary import (
    DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_SYMBOLS,
    EXIT_FAILURE,
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    MARKETS,
    main,
    resolve_market_symbols,
    run_canary,
)


class FakeDataHub:
    def __init__(
        self,
        behavior: Mapping[str, object],
        *,
        kline_behavior: Mapping[str, object] | None = None,
        stock_pool_behavior: object | None = None,
        close_result: bool = True,
    ) -> None:
        self.behavior = behavior
        self.kline_behavior = kline_behavior or {}
        self.stock_pool_behavior = stock_pool_behavior
        self.close_result = close_result
        self.attempted: list[str] = []
        self.kline_attempted: list[str] = []
        self.stock_pool_attempts = 0
        self.stock_pool_limits: list[int | None] = []
        self.cancelled: list[str] = []
        self.closed = False
        self.close_timeouts: list[float] = []
        self.all_started = asyncio.Event()

    async def quote(self, symbol: str, use_cache: bool = True) -> object:
        self.attempted.append(symbol)
        if len(self.attempted) == len(MARKETS):
            self.all_started.set()
        outcome = self.behavior.get(symbol, _quote(symbol))
        try:
            if isinstance(outcome, Exception):
                raise outcome
            if callable(outcome):
                outcome = outcome()
            if asyncio.iscoroutine(outcome):
                return await outcome
            return outcome
        except asyncio.CancelledError:
            self.cancelled.append(symbol)
            raise

    async def kline(
        self,
        symbol: str,
        limit: int = 120,
        use_cache: bool = True,
        *,
        allow_stale: bool = False,
        require_provider_response: bool = False,
    ) -> list[object]:
        self.kline_attempted.append(symbol)
        outcome = self.kline_behavior.get(symbol, _klines())
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return list(outcome)

    async def stock_pool(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets: object = None,
        minimum_market_counts: object = None,
    ) -> list[object]:
        self.stock_pool_attempts += 1
        self.stock_pool_limits.append(limit)
        outcome = self.stock_pool_behavior
        if outcome is None:
            outcome = _stock_pool()
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        rows = list(outcome)
        return rows if limit is None else rows[:limit]

    async def aclose(self, timeout: float) -> bool:
        self.closed = True
        self.close_timeouts.append(timeout)
        return self.close_result


def _settings(tmp_path: Path, *, secret: str = "canary-secret-value") -> Settings:
    return Settings(
        cache_path=tmp_path / "canary.sqlite3",
        llm_api_key=secret,
        llm_base_url=f"https://example.invalid/v1/{secret}",
        llm_model="test-model",
        tushare_token=f"token-{secret}",
    )


def _quote(symbol: str) -> SimpleNamespace:
    code, market = symbol.split(".")
    return SimpleNamespace(
        code=code,
        market=market,
        source="fake-provider",
        price=10.5,
        timestamp=market_now().isoformat(),
        from_cache=False,
        fallback_used=False,
    )


def _klines(*, from_cache: bool = False, fallback_used: bool = False) -> list[SimpleNamespace]:
    today = market_now().date()
    return [
        SimpleNamespace(
            date=(today - timedelta(days=offset)).isoformat(),
            open=10.0,
            close=10.5,
            high=10.8,
            low=9.9,
            volume=1000.0,
            source="fake-provider",
            from_cache=from_cache,
            fallback_used=fallback_used,
        )
        for offset in range(4, -1, -1)
    ]


def _stock_pool(*, fallback_used: bool = False) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            symbol=symbol,
            code=symbol.split(".")[0],
            market=market,
            source="fake-provider",
            fallback_used=fallback_used,
        )
        for market, symbol in DEFAULT_SYMBOLS.items()
    ]


def _ordered_full_stock_pool() -> list[SimpleNamespace]:
    rows = [
        SimpleNamespace(
            symbol=f"{code:06d}.SZ",
            code=f"{code:06d}",
            market="SZ",
            source="fake-provider",
            fallback_used=False,
        )
        for code in range(1, 201)
    ]
    rows.extend(row for row in _stock_pool() if row.market in {"SH", "BJ"})
    return rows


async def _sleep_then_quote(symbol: str, delay: float = 1.0) -> object:
    await asyncio.sleep(delay)
    return _quote(symbol)


async def _sleep_then_stock_pool(delay: float = 0.03) -> list[SimpleNamespace]:
    await asyncio.sleep(delay)
    return _stock_pool()


def test_default_and_overridden_symbols_cover_all_three_markets() -> None:
    assert resolve_market_symbols(DEFAULT_SYMBOLS) == DEFAULT_SYMBOLS
    assert resolve_market_symbols(
        {
            "SH": "sh600000",
            "SZ": "000002.sz",
            "BJ": "bj.835185",
        }
    ) == {
        "SH": "600000.SH",
        "SZ": "000002.SZ",
        "BJ": "835185.BJ",
    }

    with pytest.raises(ValueError, match="SH representative symbol belongs to SZ"):
        resolve_market_symbols({**DEFAULT_SYMBOLS, "SH": "000001"})


def test_canary_attempts_every_market_and_reports_success(tmp_path: Path) -> None:
    datahub = FakeDataHub({})

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert datahub.attempted == list(DEFAULT_SYMBOLS.values())
    assert datahub.kline_attempted == list(DEFAULT_SYMBOLS.values())
    assert datahub.stock_pool_attempts == 1
    assert datahub.stock_pool_limits == [None]
    assert datahub.closed is True
    assert datahub.close_timeouts == [DEFAULT_CLEANUP_TIMEOUT_SECONDS]
    assert summary["exit_code"] == EXIT_SUCCESS
    assert summary["success_count"] == 3
    assert summary["failure_count"] == 0
    assert summary["overall_timed_out"] is False
    assert summary["stock_pool"]["status"] == "success"
    assert summary["stock_pool"]["market_counts"] == {"SH": 1, "SZ": 1, "BJ": 1}
    assert {market: result["status"] for market, result in summary["markets"].items()} == {
        "SH": "success",
        "SZ": "success",
        "BJ": "success",
    }
    assert all(result["from_cache"] is False for result in summary["markets"].values())
    assert all(result["kline"]["row_count"] == 5 for result in summary["markets"].values())


def test_canary_aggregates_markets_from_the_complete_ordered_stock_pool(tmp_path: Path) -> None:
    datahub = FakeDataHub({}, stock_pool_behavior=_ordered_full_stock_pool())

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert summary["exit_code"] == EXIT_SUCCESS
    assert summary["stock_pool"]["status"] == "success"
    assert summary["stock_pool"]["row_count"] == 202
    assert summary["stock_pool"]["market_counts"] == {"SH": 1, "SZ": 200, "BJ": 1}
    assert datahub.stock_pool_limits == [None]


def test_cache_or_fallback_is_reported_as_degraded_but_available(tmp_path: Path) -> None:
    symbol = DEFAULT_SYMBOLS["SH"]
    datahub = FakeDataHub(
        {},
        kline_behavior={symbol: _klines(from_cache=True, fallback_used=True)},
    )

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert summary["exit_code"] == EXIT_SUCCESS
    assert summary["success_count"] == 2
    assert summary["degraded_count"] == 1
    assert summary["available_count"] == 3
    assert summary["markets"]["SH"]["status"] == "degraded"
    assert summary["markets"]["SH"]["kline"]["fallback_used"] is True


def test_contract_drift_and_truncated_kline_are_errors(tmp_path: Path) -> None:
    sh_symbol = DEFAULT_SYMBOLS["SH"]
    malformed_pool = _stock_pool()[:-1]
    datahub = FakeDataHub(
        {},
        kline_behavior={sh_symbol: _klines()[:2]},
        stock_pool_behavior=malformed_pool,
    )

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert summary["exit_code"] == EXIT_PARTIAL_FAILURE
    assert summary["markets"]["SH"]["status"] == "error"
    assert "truncated" in summary["markets"]["SH"]["error"]
    assert summary["stock_pool"]["status"] == "error"
    assert "missing markets" in summary["stock_pool"]["error"]


@pytest.mark.parametrize("row_count", [3, 4])
def test_daily_kline_requires_exactly_five_rows(tmp_path: Path, row_count: int) -> None:
    sh_symbol = DEFAULT_SYMBOLS["SH"]
    datahub = FakeDataHub({}, kline_behavior={sh_symbol: _klines()[:row_count]})

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert summary["markets"]["SH"]["status"] == "error"
    assert "expected exactly 5 rows" in summary["markets"]["SH"]["error"]


def test_future_market_dates_are_rejected(tmp_path: Path) -> None:
    symbol = DEFAULT_SYMBOLS["SH"]
    future_quote = _quote(symbol)
    future_quote.timestamp = (market_now() + timedelta(days=1)).isoformat()
    future_klines = _klines()
    future_klines[-1].date = (market_now().date() + timedelta(days=1)).isoformat()

    quote_summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: FakeDataHub({symbol: future_quote}),
        )
    )
    kline_summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: FakeDataHub(
                {},
                kline_behavior={symbol: future_klines},
            ),
        )
    )

    assert "future" in quote_summary["markets"]["SH"]["error"]
    assert "future" in kline_summary["markets"]["SH"]["error"]


def test_per_request_timeout_is_bounded_and_partial_failure(tmp_path: Path) -> None:
    sh_symbol = DEFAULT_SYMBOLS["SH"]
    datahub = FakeDataHub({sh_symbol: lambda: _sleep_then_quote(sh_symbol)})

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.01,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert set(datahub.attempted) == set(DEFAULT_SYMBOLS.values())
    assert datahub.cancelled == [sh_symbol]
    assert summary["exit_code"] == EXIT_PARTIAL_FAILURE
    assert summary["markets"]["SH"]["status"] == "timeout"
    assert summary["markets"]["SH"]["timeout_scope"] == "request"
    assert summary["success_count"] == 2


def test_stock_pool_uses_its_dedicated_timeout(tmp_path: Path) -> None:
    datahub = FakeDataHub(
        {},
        stock_pool_behavior=lambda: _sleep_then_stock_pool(),
    )

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.01,
            stock_pool_timeout=0.1,
            overall_timeout=0.2,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert summary["exit_code"] == EXIT_SUCCESS
    assert summary["request_timeout_seconds"] == 0.01
    assert summary["stock_pool_timeout_seconds"] == 0.1
    assert summary["stock_pool"]["status"] == "success"


def test_overall_timeout_cancels_and_drains_all_pending_markets(tmp_path: Path) -> None:
    datahub = FakeDataHub(
        {
            symbol: (lambda symbol=symbol: _sleep_then_quote(symbol))
            for symbol in DEFAULT_SYMBOLS.values()
        }
    )

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=1.0,
            overall_timeout=0.01,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert set(datahub.attempted) == set(DEFAULT_SYMBOLS.values())
    assert set(datahub.cancelled) == set(DEFAULT_SYMBOLS.values())
    assert datahub.closed is True
    assert summary["exit_code"] == EXIT_FAILURE
    assert summary["overall_timed_out"] is True
    assert all(result["timeout_scope"] == "overall" for result in summary["markets"].values())


def test_external_cancellation_propagates_after_children_are_drained(tmp_path: Path) -> None:
    datahub = FakeDataHub(
        {
            symbol: (lambda symbol=symbol: _sleep_then_quote(symbol, delay=10.0))
            for symbol in DEFAULT_SYMBOLS.values()
        }
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            run_canary(
                _settings(tmp_path),
                symbols=DEFAULT_SYMBOLS,
                request_timeout=20.0,
                overall_timeout=30.0,
                datahub_factory=lambda _settings: datahub,
            )
        )
        await asyncio.wait_for(datahub.all_started.wait(), timeout=0.5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert set(datahub.cancelled) == set(DEFAULT_SYMBOLS.values())
    assert datahub.closed is True


def test_provider_cancellation_is_not_converted_to_an_error_result(tmp_path: Path) -> None:
    async def cancelled_provider_call() -> object:
        raise asyncio.CancelledError

    datahub = FakeDataHub({DEFAULT_SYMBOLS["SH"]: cancelled_provider_call})

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_canary(
                _settings(tmp_path),
                symbols=DEFAULT_SYMBOLS,
                request_timeout=0.2,
                overall_timeout=0.5,
                datahub_factory=lambda _settings: datahub,
            )
        )

    assert datahub.closed is True


def test_errors_are_sanitized_with_settings_secrets(tmp_path: Path) -> None:
    secret = "ultra-private-canary-key"
    base_url = f"https://example.invalid/v1/{secret}"
    settings = _settings(tmp_path, secret=secret).model_copy(update={"llm_base_url": base_url})
    datahub = FakeDataHub(
        {
            symbol: RuntimeError(
                f"Authorization: Bearer {secret}; api_key={secret}; endpoint={base_url}"
            )
            for symbol in DEFAULT_SYMBOLS.values()
        }
    )

    summary = asyncio.run(
        run_canary(
            settings,
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: datahub,
        )
    )
    output = json.dumps(summary, ensure_ascii=False)

    assert summary["exit_code"] == EXIT_FAILURE
    assert secret not in output
    assert base_url not in output
    assert "<redacted>" in output


@pytest.mark.parametrize(
    ("failed_markets", "expected_exit_code"),
    [
        (set(), EXIT_SUCCESS),
        ({"SH"}, EXIT_PARTIAL_FAILURE),
        ({"SH", "SZ", "BJ"}, EXIT_FAILURE),
    ],
)
def test_success_partial_and_all_failure_exit_code_contract(
    tmp_path: Path,
    failed_markets: set[str],
    expected_exit_code: int,
) -> None:
    behavior = {
        DEFAULT_SYMBOLS[market]: RuntimeError(f"{market} unavailable")
        for market in failed_markets
    }
    datahub = FakeDataHub(behavior)

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert summary["exit_code"] == expected_exit_code


def test_cleanup_failure_overrides_partial_failure_exit_code(tmp_path: Path) -> None:
    datahub = FakeDataHub(
        {DEFAULT_SYMBOLS["SH"]: RuntimeError("SH unavailable")},
        close_result=False,
    )

    summary = asyncio.run(
        run_canary(
            _settings(tmp_path),
            symbols=DEFAULT_SYMBOLS,
            request_timeout=0.2,
            overall_timeout=0.5,
            datahub_factory=lambda _settings: datahub,
        )
    )

    assert summary["available_count"] == 2
    assert summary["cleanup"]["status"] == "error"
    assert summary["exit_code"] == EXIT_FAILURE


def test_cli_overrides_emit_one_machine_readable_json_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    symbols = {"SH": "600000.SH", "SZ": "000002.SZ", "BJ": "835185.BJ"}
    datahub = FakeDataHub({})
    configured_cache_path = _settings(tmp_path).cache_path
    runtime_cache_paths: list[Path] = []

    def build_datahub(settings: Settings) -> FakeDataHub:
        runtime_cache_paths.append(settings.cache_path)
        return datahub

    exit_code = main(
        [
            "--sh-symbol",
            symbols["SH"],
            "--sz-symbol",
            symbols["SZ"],
            "--bj-symbol",
            symbols["BJ"],
            "--request-timeout",
            "0.2",
            "--stock-pool-timeout",
            "0.3",
            "--overall-timeout",
            "0.5",
        ],
        settings_factory=lambda: _settings(tmp_path),
        datahub_factory=build_datahub,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert exit_code == EXIT_SUCCESS
    assert payload["stock_pool_timeout_seconds"] == 0.3
    assert {market: payload["markets"][market]["symbol"] for market in MARKETS} == symbols
    assert runtime_cache_paths[0] != configured_cache_path
    assert not runtime_cache_paths[0].exists()


def test_cli_startup_failure_is_json_and_redacted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "startup-secret-token"

    def fail_to_build(_settings: Settings) -> FakeDataHub:
        raise RuntimeError(f"base_url=https://example.invalid?api_key={secret}")

    exit_code = main(
        ["--request-timeout", "0.2", "--overall-timeout", "0.5"],
        settings_factory=lambda: _settings(tmp_path, secret=secret),
        datahub_factory=fail_to_build,
    )

    payload = capsys.readouterr().out
    assert exit_code == EXIT_FAILURE
    assert json.loads(payload)["failure_count"] == 3
    assert secret not in payload
    assert "<redacted>" in payload
