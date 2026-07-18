# Operations Guide

## 1. Local Runtime

Use Python 3.12 and a virtual environment owned by the checkout. Set `PROJECT_ROOT` to the repository location; the default below assumes a checkout directly under `$HOME`.

```bash
export PROJECT_ROOT="${PROJECT_ROOT:-$HOME/AShareRadar}"
cd "$PROJECT_ROOT"
python3.12 -m venv "$PROJECT_ROOT/.venv"
source "$PROJECT_ROOT/.venv/bin/activate"
export PYTHON="$PROJECT_ROOT/.venv/bin/python"
export PYTHONNOUSERSITE=1
$PYTHON -m pip install --require-hashes -r requirements-lock.txt
```

Start the app:

```bash
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1
```

Detached local service used during development:

```bash
screen -dmS ashare_radar bash -lc 'cd "$PROJECT_ROOT" && exec env PYTHONNOUSERSITE=1 "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 > /tmp/ashare_radar.log 2>&1'
```

The scheduler is in-process. Background startup acquires and holds the non-blocking advisory lock `<SQLite path>.scheduler.lock`; manual runs acquire or reuse the same lock. A second process sharing that database stays available for ordinary API traffic but skips scheduler ownership, and a manual task routed there fails with a busy error while the owner still holds the lock. Stop waits only for `ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS`; if a runner remains alive at that deadline, guard release is deferred until the runner actually exits, so restart/manual work and another process remain blocked from scheduler ownership. Task-run completion is cancellation-dominant: `cancelled` may replace a success written during the cancellation race, while a late `success` or `failed` update cannot replace `cancelled`. Keep `--workers 1` because scheduler status and controls are process-local; the lock prevents duplicate work but does not make a multi-worker deployment supported. Disable the scheduler explicitly for short-lived smoke processes.

Synchronous container construction, route-level repository/diagnostic calls, workflow cache access, market-sampling and stock-confirmation event writes, SSE watchlist fallback reads, and scheduler SQLite work are offloaded from the asyncio event-loop thread. Blocking provider SDK work uses a separate four-worker executor owned by each `ProviderRuntime`, rather than the default SQLite executor. Provider calls carry a result-defining request key: identical concurrent requests share one task, while different requests use a bounded two-slot queue per provider capability before work is submitted. A caller timeout or cancellation does not cancel a shared SDK task; once that task has no foreground waiter it is treated as orphaned, and different requests fail over to another provider until it exits. Admission pressure is not recorded as a provider outage or cooldown. On normal shutdown and startup failure, the lifespan calls `DataHub.aclose()`: the runtime rejects new calls, cancels tracked async waiters and queued executor work, and waits only for its bounded timeout; provider clients close after runtime quiescence. Python cannot forcibly stop a provider SDK call already running in a worker thread, so a non-quiescent close returns `False` and that worker remains tracked until it finishes in the background.

Browser mutation requests are same-origin protected before route side effects. For `POST`, `PUT`, `PATCH`, `DELETE`, and `GET` with truthy `refresh`, a request carrying browser origin metadata is accepted only when the Host-derived request origin is configured as allowed and any supplied `Origin` or `Referer` is also allowed; when both source headers are absent, `Sec-Fetch-Site: cross-site` is rejected. This rejects Host-header/DNS-rebinding attempts even when an attacker supplies an Origin matching that hostile Host. Ordinary read-only `GET`/`HEAD` requests are unaffected, and CLI/health tooling without browser origin metadata remains supported. The same configured origins are used for CORS and mutation trust; adding an origin therefore grants browser write access and should be done narrowly.

Check status:

```bash
screen -ls
lsof -nP -iTCP:8010 -sTCP:LISTEN
curl -sS http://127.0.0.1:8010/api/health
```

Stop:

```bash
screen -S ashare_radar -X stuff $'\003'
for _ in $(seq 1 40); do
    lsof -tiTCP:8010 -sTCP:LISTEN >/dev/null || break
    sleep 0.25
done
lsof -nP -iTCP:8010 -sTCP:LISTEN
```

The final `lsof` command must print no listener. Sending `Ctrl-C` lets Uvicorn run the application shutdown path; `screen -X quit` alone can remove the terminal session before confirming that its child process exited. If a listener remains, inspect it with `ps -p <PID> -o command=` and send `kill -TERM <PID>` only after confirming that it is this checkout's Uvicorn process, then re-run `lsof` before starting, restoring, or deleting data.

Inspect logs for the detached service:

```bash
tail -n 200 /tmp/ashare_radar.log
tail -f /tmp/ashare_radar.log
```

## 2. Local Data Boundary

Runtime files under `data/` are local state, not source code:

```text
data/ashare_radar.sqlite3
data/ashare_radar.sqlite3-wal
data/ashare_radar.sqlite3-shm
data/ashare_radar.sqlite3.scheduler.lock
data/trading_calendar.json
```

The supported SQLite runtime database is `data/ashare_radar.sqlite3`. Legacy or smoke-test files such as `data/app.db` and `data/smoke.sqlite3*` are disposable local artifacts, not supported runtime state. The scheduler lock records the owner's PID, but ownership comes from the operating-system lock rather than the file contents; the file may remain after a clean or unclean exit. Never delete or replace it while any process using that database is running, because recreating the pathname would split cross-process protection. The repository keeps `data/.gitkeep` only so the directory exists; all generated files in this list are local-only and must remain uncommitted.

### User-Data Export and Import

The Tools view exports a versioned JSON bundle containing only the local watchlist, alert rules/events, stock notes, advice history, and advice-review plans/results that exist in the current schema. Market caches, provider status, task/monitor records, settings, and credentials are excluded. The browser rejects files larger than 50 MB.

Import supports `merge` and `replace`. In merge mode, supported stable keys identify logical rows and the source bundle wins when a stable key already exists. For surrogate-key tables, an incoming ID collision that does not identify the same row is remapped to an unused target ID, and bundled child foreign keys are rewritten to follow the remapped parent. Related parent and child tables must travel as a group: any non-empty child table requires every referenced surrogate-ID parent table in the same bundle. Rows whose stable key matches, or whose original surrogate ID and complete contents still match, are idempotent. A collision-remapped copy has no portable identity outside that import, so review a new dry run before deliberately importing the same bundle into the same non-empty database again.

Column order may differ between the bundle and target database, but their column sets, declared column types, and primary-key definitions must be compatible. Version-1 bundles created before the review-price provenance columns were added receive only those known columns with conservative `unknown`/`null` defaults; other schema drift is rejected. Both modes validate row shapes and foreign-key relationships inside one transaction. Replace still requires a complete snapshot of every user-data table available in the target database and removes target rows absent from that snapshot. The UI requires a successful dry-run preview for the same file and mode before enabling commit; create a current export or full runtime backup before a replace.

The equivalent endpoints are `POST /api/local-data/export` and `POST /api/local-data/import?mode=merge|replace&dry_run=true|false`.

Advice-review plans can be deleted from the Replay workspace or through `DELETE /api/reviews/plans/{plan_id}`. Deletion is irreversible: it removes the plan and all evaluation rows associated with that plan, including results from earlier revisions. The persisted advice snapshot used to create the plan is retained because it is shared recommendation history rather than plan-owned data.

### Runtime Backup and Restore

Use the runtime-data tool for a consistent full-database snapshot. It uses SQLite's backup API, writes `runtime.sqlite3` plus `manifest.json`, and records SHA-256, schema/user versions, table counts, user-table counts, and `PRAGMA integrity_check`:

```bash
$PYTHON tools/runtime_data.py backup
$PYTHON tools/runtime_data.py verify data/backups/ashare_radar_TIMESTAMP
```

The backup command prints the created path as JSON and, unless `--destination` is supplied, creates a new timestamped directory under `data/backups/`. Verification accepts either that directory or its `manifest.json`.

Restore a backup while the service is stopped. Restore verifies the bundle first, refuses a held scheduler lock or active database connection, creates a pre-restore rollback snapshot when a target database exists, replaces the database atomically, and verifies the result again:

```bash
screen -S ashare_radar -X stuff $'\003' || true
for _ in $(seq 1 40); do
    lsof -tiTCP:8010 -sTCP:LISTEN >/dev/null || break
    sleep 0.25
done
lsof -nP -iTCP:8010 -sTCP:LISTEN
$PYTHON tools/runtime_data.py restore data/backups/ashare_radar_TIMESTAMP --confirm-service-stopped
```

Before deleting or replacing local data, stop the service and create and verify a backup with the tool above. Do not copy a live SQLite database or its WAL/SHM files directly:

```bash
screen -S ashare_radar -X stuff $'\003' || true
for _ in $(seq 1 40); do
    lsof -tiTCP:8010 -sTCP:LISTEN >/dev/null || break
    sleep 0.25
done
lsof -nP -iTCP:8010 -sTCP:LISTEN
backup_dir="data/backups/before-reset-$(date +%Y%m%d_%H%M%S)"
$PYTHON tools/runtime_data.py backup --destination "$backup_dir"
$PYTHON tools/runtime_data.py verify "$backup_dir"
```

If local data becomes inconsistent during development, remove only the affected runtime files after a backup exists. The app will recreate the SQLite schema on startup.

```bash
rm -f data/ashare_radar.sqlite3 data/ashare_radar.sqlite3-wal data/ashare_radar.sqlite3-shm
rm -f data/ashare_radar.sqlite3.scheduler.lock
rm -f data/app.db data/smoke.sqlite3 data/smoke.sqlite3-wal data/smoke.sqlite3-shm
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1
```

Restore only through `tools/runtime_data.py`: it validates the manifest before replacement and creates a rollback snapshot of the current target. Start the service again only after restore succeeds.

Verify after cleanup or restore:

```bash
curl -sS http://127.0.0.1:8010/api/health
curl -sS http://127.0.0.1:8010/api/data/status
curl -sS 'http://127.0.0.1:8010/api/stock/workbench?symbol=600519'
```

### Retention Cleanup

Opening the Tools view loads `GET /api/local-data/cleanup-preview`. Cleanup removes only rows above configured retention limits for quote history, minute K-lines, stock concepts, cache events, task runs, monitor events, alert events, and advice history. It does not directly delete watchlist rows, alert rules, stock notes, or advice-review plans. The preview reports per-table and total counts; when advice or alert history is included it sets `requires_user_backup=true`, and the UI asks for backup confirmation.

After reviewing the preview, the UI calls `POST /api/local-data/cleanup?confirm=retention-cleanup`. The preview is advisory: concurrent scheduler activity can change the committed count, so use the returned result as the deletion record. Export user data or create a full runtime backup before cleanup when user-history rows are listed.

The trading-calendar refresh API (`POST /api/data/trading-calendar/refresh`) reports `ok=false` and an `error` field when the optional AKShare calendar source fails, so a failed refresh can be distinguished from an empty but valid local cache.

Cache and quality checks use Shanghai market time rather than file age alone. Quote and minute data share one session policy: live morning/afternoon rows have a bounded delay; the lunch break requires an 11:25-11:30 snapshot; 13:00-13:15 accepts that morning-close snapshot or a fresh afternoon row; after the grace period, afternoon data is required; and after close, same-trading-day events at or after 14:55 are accepted, including provider-stamped after-hours updates that are not later than the check time. Daily research accepts the explicit `qfq` contract; incompatible adjustment modes and migrated legacy `unknown` rows remain isolated and trigger another provider/cache path rather than entering analysis. Daily K-line cache reuse and quality both continue to require the previous trading day through 15:14:59 and switch to the current trading day at 15:15:00. Weekend and holiday checks use the prior trading day's closing snapshot.

SQLite persistence has two ordering guarantees worth preserving during recovery or concurrency testing:

- Quote event timestamps are stored as fixed `Asia/Shanghai` `YYYY-MM-DD HH:MM:SS` text. Snapshot and daily-history upserts use SQLite `ashare_market_epoch()` for both `quote_timestamp` and `fetched_at`: a replacement is accepted only for a newer event epoch, or for an equal event with a non-older fetch epoch. The parser also accepts existing UTC ISO/offset cache values, so a legacy row is upgraded without raw-string misordering and a late provider completion cannot overwrite fresher market data.
- Advice snapshot de-duplication uses `BEGIN IMMEDIATE` around latest-row lookup and update/insert. A new advice row and its watchlist unread increment commit or roll back together; a repeated identical conclusion increments only `repeat_count`, while a changed/new snapshot increments unread once. Timeline rows retain snapshot/rule/model version, conclusion basis, market time, and data-quality source so legacy or version-changed rows can be shown as non-comparable instead of false changes.
- An empty SSE symbol query reads the watchlist selection off the event loop. Active non-excluded symbols are preferred. Configured seed symbols are used only when the watchlist table has no rows; a table containing only excluded rows returns `422` and requires an explicit symbol instead of silently reactivating seeds.

## 3. Diagnostics and Browser Notifications

### System Diagnostics

The data-source/monitoring panel reads `GET /api/system/diagnostics`. The response separates cache fetch activity from market-data freshness and includes database size/budget, cache/runtime/user row totals, scheduler state, provider status, table counts, bounded warnings, and remediation suggestions. Freshness covers quotes, daily/minute K-lines, the stock pool, and plate metadata; a non-empty stock-pool or plate cache without a usable update timestamp is reported as missing freshness metadata rather than healthy. Storage warns at 80% of `ASHARE_RADAR_MAX_DATABASE_SIZE_MB` and reports an over-budget state above the configured limit. The monitoring surface also reads data-source status, recent task runs, and monitor events; its normal refresh interval is 15 seconds.

Use diagnostics to distinguish a stale market snapshot from a recent failed fetch, identify capability-level provider failures, see when fewer than two real-time quote sources are enabled, detect demo data, check the trading-calendar fallback, and confirm that alerts are not waiting on a stopped scheduler. A task result of `degraded` means the run completed with fallback or incomplete source coverage and should not be interpreted as either full success or total failure. A scheduler with `standby=true` is not stopped: another process owns the scheduler lock, and the current process is deliberately not running duplicate background work. Diagnostics are read-only; use the explicit task controls, calendar refresh, backup, or cleanup operations for changes.

### Browser Notifications

Create and enable alert rules, then click `启用桌面提醒` and grant browser permission. Permission is requested only from that user action. Once enabled, the page polls alert events in pages of up to 50 every 30 seconds and notifies only new `触发` events. The authoritative keyset cursor is the monotonically increasing event `id`; the legacy `after_created_at` value does not participate in ordering. The first successful poll establishes a no-backfill baseline. Up to three new events are shown individually, while a larger burst becomes one summary notification. Clicking a notification focuses the page.

Click the active notification control to disable delivery. The enabled/disabled preference is stored in browser local storage and restored after a page or application restart. Disabling stops polling, invalidates any in-flight delivery, and clears the prior cursor; re-enabling establishes a new baseline, so events created during the disabled period are not replayed. Polling failure leaves the persisted cursor unchanged, while a notification-construction failure advances only through the successfully delivered prefix so the failed event and all later events remain eligible for ordered retry.

Notifications require the page to remain open; there is no service worker or operating-system background delivery after the page closes. A denied permission must be changed in browser settings. If event polling fails, alert evaluation and persistence continue, while the notification control shows a synchronization warning.

## 4. Environment Variables

Use the `ASHARE_RADAR_*` namespace for new configuration. Legacy aliases are accepted where listed for local compatibility. Process environment values take precedence. For the five allowlisted `ASHARE_RADAR_LLM_*` names only, the application falls back to simple top-level assignments in `$HOME/.zshrc`; it parses that file without sourcing or executing it and ignores command substitutions, nested shell blocks, and unrelated names. It does not read `.env` files, project configuration, user-data imports, or browser storage for credentials. Settings are captured by the application container, and scheduler intervals/task registration are not hot-reloaded. Restart the single process after changing configuration.

| Variable | Default | Legacy alias | Notes |
| --- | --- | --- | --- |
| `ASHARE_RADAR_LLM_API_KEY` | empty | - | Secret; process environment first, then the allowlisted `$HOME/.zshrc` fallback. |
| `ASHARE_RADAR_LLM_BASE_URL` | empty | - | OpenAI-compatible absolute endpoint; HTTPS is required except for loopback development, and query/fragment/userinfo components are rejected. |
| `ASHARE_RADAR_LLM_MODEL` | empty | - | LLM explanation model; required together with API key and base URL. |
| `ASHARE_RADAR_LLM_ENABLED` | `1` | - | Set `0` to force rule-only answers. |
| `ASHARE_RADAR_LLM_TIMEOUT_SECONDS` | `30` | - | Positive finite total budget shared by initial generation and the optional validation-correction request. The browser allows 35 seconds so it does not abort before this server budget expires. |
| `ASHARE_RADAR_TUSHARE_TOKEN` | empty | `TUSHARE_TOKEN` | Secret for optional Tushare provider. |
| `ASHARE_RADAR_FUTU_ENABLED` | `0` | `FUTU_ENABLED` | Requires local Futu OpenD. |
| `ASHARE_RADAR_FUTU_HOST` | `127.0.0.1` | `FUTU_HOST` | Futu OpenD host. |
| `ASHARE_RADAR_FUTU_PORT` | `11111` | `FUTU_PORT` | Futu OpenD port. |
| `ASHARE_RADAR_DEMO_PROVIDER_ENABLED` | `0` | `DEMO_PROVIDER_ENABLED` | Demo data must stay disabled for real research. |
| `ASHARE_RADAR_CORS_ALLOW_ORIGINS` | local 8010 origins | `CORS_ALLOW_ORIGINS` | Comma-separated CORS origins; for browser mutations/refresh writes, both the Host-derived origin and supplied Origin/Referer must be in this list. |
| `ASHARE_RADAR_CACHE_PATH` | project `data/ashare_radar.sqlite3` | `CACHE_PATH` | Absolute path or project-root-relative SQLite path. |
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
| `ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | `5` | `SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | Bounded stop wait; the scheduler lock remains held after timeout until unfinished runners exit. |
| `ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS` | `50000` | `MAX_QUOTE_HISTORY_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_MINUTE_KLINE_ROWS` | `20000` | `MAX_MINUTE_KLINE_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_STOCK_CONCEPT_ROWS` | `20000` | `MAX_STOCK_CONCEPT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_TASK_RUN_ROWS` | `2000` | `MAX_TASK_RUN_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_MONITOR_EVENT_ROWS` | `3000` | `MAX_MONITOR_EVENT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_CACHE_EVENT_ROWS` | `5000` | `MAX_CACHE_EVENT_ROWS` | Runtime retention cap for cache/provider events. |
| `ASHARE_RADAR_MAX_ALERT_EVENT_ROWS` | `5000` | `MAX_ALERT_EVENT_ROWS` | Runtime retention cap for alert events. |
| `ASHARE_RADAR_MAX_ADVICE_HISTORY_ROWS` | `20000` | `MAX_ADVICE_HISTORY_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_DATABASE_SIZE_MB` | `512` | - | Local SQLite capacity budget in MiB; minimum `16`. Diagnostics warn at 80%. |
| `ASHARE_RADAR_ADVICE_HISTORY_DEDUPE_SECONDS` | `180` | `ADVICE_HISTORY_DEDUPE_SECONDS` | Advice-history de-duplication window. |
| `ASHARE_RADAR_QUOTE_STALE_WARNING_SECONDS` | `900` | `QUOTE_STALE_WARNING_SECONDS` | Quote freshness warning threshold. |
| `ASHARE_RADAR_QUOTE_CONSISTENCY_WARNING_PCT` | `1.0` | `QUOTE_CONSISTENCY_WARNING_PCT` | Multi-source price-difference warning threshold. |
| `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH` | `0` | `TRADE_CALENDAR_AUTO_FETCH` | Optional AKShare calendar fetch when the local trading-calendar cache is missing. |

Missing optional values use documented defaults. Present but malformed boolean, numeric, path, or LLM endpoint values fail configuration at startup instead of silently changing behavior. Restart after changing process variables or the allowlisted LLM assignments in `$HOME/.zshrc`.

### LLM Remote Data Boundary

When LLM enhancement is enabled, the app sends an OpenAI-compatible chat-completion request to the configured remote endpoint. Chat messages contain the current question and topic; symbol and stock name; the deterministic rule answer; authoritative conclusion, confidence, support, resistance, actions, and invalidations; selected current quote/MA/trend/risk facts; data-quality score, level, source, and at most four notes; and at most six rule-evidence items. Local watchlists, stock notes, alert rules/events, advice history, provider credentials, full workbench payloads, and other local collections are not sent.

The transport also sends the configured model name, generation parameters, and `ASHARE_RADAR_LLM_API_KEY` as authentication to that endpoint. The API key is not inserted into chat messages, but the remote service necessarily receives it as a request credential. Use only an endpoint whose data-handling policy is acceptable, or set `ASHARE_RADAR_LLM_ENABLED=0` to keep Q&A rule-only.

The first model response is validated locally. If and only if that output fails local validation, the app may send one format-correction request with the same bounded context and a stricter instruction that the explanation contain no numbers or action words; it does not resend the previous raw model output. One outer timeout covers the first request, local validation, and correction together, so correction receives only the remaining `ASHARE_RADAR_LLM_TIMEOUT_SECONDS` budget rather than a new full timeout. The SDK's automatic retries are disabled. A request error, total-budget expiry, or second validation failure returns the deterministic rule answer without another remote attempt.

## 5. Verification Gates

Run before delivery:

```bash
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
$PYTHON -m ruff check app tests tools
$PYTHON -m mypy
npm ci
npm run check:js
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing
npx --no-install playwright install chromium
npm run test:e2e
```

`requirements-dev-lock.txt` is installed directly because it includes both runtime and engineering dependencies. Tests run with `PYTHONNOUSERSITE=1` and must resolve packages from the active Python 3.12 runtime, never from a user-level or machine-specific interpreter path. Repository/database tests use temporary SQLite state and provider/network behavior is replaced with fakes at unit-test boundaries; live provider access belongs only in an explicit smoke check.

Smoke checks:

```bash
curl -sS http://127.0.0.1:8010/api/health
curl -sS 'http://127.0.0.1:8010/api/stocks?keyword=600519&limit=5'
curl -sS 'http://127.0.0.1:8010/api/stock/workbench?symbol=600519'
```

## 6. Dependency and Documentation Maintenance

Keep direct dependencies in the appropriate input file. Runtime libraries belong in `requirements.txt`; pytest, coverage, Ruff, mypy, pyflakes, and lock tooling belong in `requirements-dev.txt`. These input files are for lock generation: reproducible installs use `--require-hashes` and the generated lock files. A runtime-input change requires rebuilding both locks because `requirements-dev.txt` includes `requirements.txt`; a development-only input change requires rebuilding `requirements-dev-lock.txt`. Do not edit generated locks by hand. Verify the development lock in a clean Python 3.12 environment:

```bash
$PYTHON -m piptools compile --generate-hashes \
  --output-file=requirements-lock.txt requirements.txt
$PYTHON -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements-dev-lock.txt requirements-dev.txt
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
```

Regenerate inventory files only when accepting their source changes. CI and review should use the non-mutating checks:

```bash
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
```

## 7. Provider Failure Handling

- AKShare is optional. The app and checks isolate user-level Python packages so pandas/numpy resolve from `$PROJECT_ROOT/.venv`. If AKShare still fails, the app should degrade to backup providers or local stock data without dumping native traceback noise into service logs.
- Demo provider remains disabled unless `ASHARE_RADAR_DEMO_PROVIDER_ENABLED=1`.
- Tushare should be reported as disabled until `ASHARE_RADAR_TUSHARE_TOKEN` is configured.
- Futu should be reported as disabled until `ASHARE_RADAR_FUTU_ENABLED=1` and OpenD is reachable.
- Provider exceptions are sanitized before being appended to request diagnostics or written to aggregate/capability status. Repository writes sanitize and cap the stored value, and row mappers sanitize `last_error` again when reading older databases; URL userinfo, authorization/bearer values, token/key/password-style assignments, sensitive query parameters, known credential values, and quoted sensitive entries inside JSON/Python-style mappings must not reach API responses.
- Client request-shape errors remain `422`. A Pydantic `ValidationError` raised while constructing an internal response/model is treated as unavailable internal data: the server logs the traceback and returns generic `503` detail without the rejected value. SQLite `DatabaseError` and provider/runtime failures also return `503`, with their public text sanitized.
- On mobile, the source DOM and focus order remain query, workspace, then local controls; tab/tabpanel, validation, and chart-filter ARIA state must stay synchronized. Watchlist, alert, and note persistence has an independent request scope per write: navigation or another write may suppress stale UI/readback work but must not abort a server commit already in flight. Advice timeline and minute chart loads have independent abort controllers and sequence counters; timeline ownership immediately renders the requested symbol's loading state and rejects stale A-B-A completions. Minute 204/205, empty, `null`, non-object, wrong-symbol, and wrong-interval responses clear minute state and show unavailable rather than leaving a loading or mismatched chart. Daily 20/60/120/240 switching redraws the existing 240-row payload, while only a new minute interval makes one minute request.
