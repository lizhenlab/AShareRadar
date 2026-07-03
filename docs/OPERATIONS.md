# Operations Guide

## 1. Local Runtime

Use Python 3.12 or newer. In this workspace the default runtime is `/opt/anaconda3/bin/python3`.

Start the app:

```bash
export PYTHON=${PYTHON:-/opt/anaconda3/bin/python3}
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

Detached local service used during development:

```bash
screen -dmS ashare_radar bash -lc 'cd /Users/zl/Documents/AShareRadar && /opt/anaconda3/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8010 > /tmp/ashare_radar.log 2>&1'
```

Check status:

```bash
screen -ls
lsof -nP -iTCP:8010 -sTCP:LISTEN
curl -sS http://127.0.0.1:8010/api/health
```

Stop:

```bash
screen -S ashare_radar -X quit
```

## 2. Local Data Boundary

Runtime files under `data/` are local state, not source code:

```text
data/ashare_radar.sqlite3
data/ashare_radar.sqlite3-wal
data/ashare_radar.sqlite3-shm
data/app.db
data/trading_calendar.json
```

The repository keeps `data/.gitkeep` only so the directory exists. Runtime data is ignored by `.gitignore`.

If local data becomes inconsistent during development, stop the service and remove the affected runtime files. The app will recreate SQLite schema on startup.

The trading-calendar refresh API (`POST /api/data/trading-calendar/refresh`) reports `ok=false` and an `error` field when the optional AKShare calendar source fails, so a failed refresh can be distinguished from an empty but valid local cache.

## 3. Environment Variables

Use the `ASHARE_RADAR_*` namespace for new configuration. Legacy aliases are accepted for local compatibility.

| Variable | Default | Legacy alias | Notes |
| --- | --- | --- | --- |
| `ASHARE_RADAR_LLM_API_KEY` | empty | - | Secret. Also read from `/Users/zl/.zshrc` for this local desktop setup. |
| `ASHARE_RADAR_LLM_BASE_URL` | empty | - | OpenAI-compatible endpoint; required together with API key and model for LLM answers. |
| `ASHARE_RADAR_LLM_MODEL` | empty | - | LLM explanation model; required together with API key and base URL. |
| `ASHARE_RADAR_LLM_ENABLED` | `1` | - | Set `0` to force rule-only answers. |
| `ASHARE_RADAR_LLM_TIMEOUT_SECONDS` | `12` | - | Malformed values fall back safely. |
| `ASHARE_RADAR_TUSHARE_TOKEN` | empty | `TUSHARE_TOKEN` | Secret for optional Tushare provider. |
| `ASHARE_RADAR_FUTU_ENABLED` | `0` | `FUTU_ENABLED` | Requires local Futu OpenD. |
| `ASHARE_RADAR_FUTU_HOST` | `127.0.0.1` | `FUTU_HOST` | Futu OpenD host. |
| `ASHARE_RADAR_FUTU_PORT` | `11111` | `FUTU_PORT` | Futu OpenD port. |
| `ASHARE_RADAR_DEMO_PROVIDER_ENABLED` | `0` | `DEMO_PROVIDER_ENABLED` | Demo data must stay disabled for real research. |
| `ASHARE_RADAR_CORS_ALLOW_ORIGINS` | local 8010 origins | `CORS_ALLOW_ORIGINS` | Comma-separated origins. |
| `ASHARE_RADAR_MINUTE_KLINE_CACHE_SECONDS` | `60` | `MINUTE_KLINE_CACHE_SECONDS` | Minute K-line cache TTL. |
| `ASHARE_RADAR_STOCK_POOL_AUTHORITATIVE_MIN_COUNT` | `1000` | `STOCK_POOL_AUTHORITATIVE_MIN_COUNT` | Fresh cache count needed to confirm an empty stock search. |
| `ASHARE_RADAR_STOCK_CONCEPT_CACHE_SECONDS` | `21600` | `STOCK_CONCEPT_CACHE_SECONDS` | Stock concept cache TTL. |
| `ASHARE_RADAR_PROVIDER_FAILURE_COOLDOWN_SECONDS` | `90` | `PROVIDER_FAILURE_COOLDOWN_SECONDS` | Provider retry cooldown after failures. |
| `ASHARE_RADAR_SCHEDULER_ENABLED` | `1` | `SCHEDULER_ENABLED` | Local refresh scheduler switch. |
| `ASHARE_RADAR_SCHEDULER_QUOTE_INTERVAL_SECONDS` | `30` | `SCHEDULER_QUOTE_INTERVAL_SECONDS` | Quote refresh interval. |
| `ASHARE_RADAR_SCHEDULER_KLINE_INTERVAL_SECONDS` | `900` | `SCHEDULER_KLINE_INTERVAL_SECONDS` | K-line refresh interval. |
| `ASHARE_RADAR_SCHEDULER_PLATE_INTERVAL_SECONDS` | `300` | `SCHEDULER_PLATE_INTERVAL_SECONDS` | Plate refresh interval. |
| `ASHARE_RADAR_SCHEDULER_HEALTH_INTERVAL_SECONDS` | `45` | `SCHEDULER_HEALTH_INTERVAL_SECONDS` | Data-health check interval. |
| `ASHARE_RADAR_SCHEDULER_KLINE_SYMBOLS_LIMIT` | `5` | `SCHEDULER_KLINE_SYMBOLS_LIMIT` | Per-cycle K-line symbol cap. |
| `ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | `5` | `SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | Shutdown wait. |
| `ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS` | `50000` | `MAX_QUOTE_HISTORY_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_MINUTE_KLINE_ROWS` | `20000` | `MAX_MINUTE_KLINE_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_STOCK_CONCEPT_ROWS` | `20000` | `MAX_STOCK_CONCEPT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_TASK_RUN_ROWS` | `2000` | `MAX_TASK_RUN_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_MONITOR_EVENT_ROWS` | `3000` | `MAX_MONITOR_EVENT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_ADVICE_HISTORY_ROWS` | `20000` | `MAX_ADVICE_HISTORY_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_ADVICE_HISTORY_DEDUPE_SECONDS` | `180` | `ADVICE_HISTORY_DEDUPE_SECONDS` | Advice-history de-duplication window. |
| `ASHARE_RADAR_QUOTE_STALE_WARNING_SECONDS` | `900` | `QUOTE_STALE_WARNING_SECONDS` | Quote freshness warning threshold. |
| `ASHARE_RADAR_QUOTE_CONSISTENCY_WARNING_PCT` | `1.0` | `QUOTE_CONSISTENCY_WARNING_PCT` | Multi-source price-difference warning threshold. |
| `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH` | `0` | `TRADE_CALENDAR_AUTO_FETCH` | Optional AKShare calendar fetch when cache is missing. |

## 4. Verification Gates

Run before delivery:

```bash
npm run check
$PYTHON tools/architecture_inventory.py
$PYTHON tools/api_inventory.py
```

Smoke checks:

```bash
curl -sS http://127.0.0.1:8010/api/health
curl -sS 'http://127.0.0.1:8010/api/stocks?keyword=600519&limit=5'
curl -sS 'http://127.0.0.1:8010/api/stock/workbench?symbol=600519'
```

## 5. Provider Failure Handling

- AKShare is optional. If its pandas/numpy stack is broken, the app should degrade to backup providers or local stock data without dumping native traceback noise into service logs.
- Demo provider remains disabled unless `ASHARE_RADAR_DEMO_PROVIDER_ENABLED=1`.
- Tushare should be reported as disabled until `ASHARE_RADAR_TUSHARE_TOKEN` is configured.
- Futu should be reported as disabled until `ASHARE_RADAR_FUTU_ENABLED=1` and OpenD is reachable.
