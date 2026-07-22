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
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --timeout-graceful-shutdown 5
```

Detached local service used during development:

```bash
screen -dmS ashare_radar bash -lc 'cd "$PROJECT_ROOT" && exec env PYTHONNOUSERSITE=1 "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --timeout-graceful-shutdown 5 > /tmp/ashare_radar.log 2>&1'
```

All supported starts use `--timeout-graceful-shutdown 5`. Without an explicit bound, Uvicorn can wait indefinitely for an open SSE connection or an in-flight data request during graceful shutdown. The five-second limit bounds HTTP connection/request draining; it does not force-stop a provider SDK thread, which is handled separately by the daemon-executor exit boundary below.

The scheduler and full-market scanner are in-process services owned by one non-blocking advisory lock at `<SQLite path>.runtime-leader.lock`. The leader starts both services; another process sharing the database remains available for ordinary reads, reports standby through both service views, and polls for leadership. On takeover it activates scheduler plus scanner together and reconciles orphaned scan rows before new scan mutation. Shutdown invokes both stop paths and returns after their configured bounded wait, but it keeps the runtime lease while any cancellation-resistant task remains alive; a deferred release occurs only after scheduler and scanner both report quiescence. A standby therefore cannot write in parallel with the old task. Partial activation uses restartable rollback, so a later takeover retry can start the same scanner instance. Operators must still confirm clean Uvicorn process exit before manual restart. Task-run completion is cancellation-dominant: `cancelled` may replace a success written during the cancellation race, while a late `success` or `failed` update cannot replace `cancelled`. Keep `--workers 1` because scheduler and scan status/control remain process-local; leadership prevents duplicate normal background ownership but does not make a multi-worker deployment supported. Disable the scheduler and automatic scan explicitly for short-lived smoke processes.

Synchronous container construction, route-level repository/diagnostic calls, workflow cache access, market-sampling and stock-confirmation event writes, SSE watchlist fallback reads, and scheduler SQLite work are offloaded from the asyncio event-loop thread. Blocking provider SDK work uses a separate four-worker `DaemonThreadPoolExecutor` owned by each `ProviderRuntime`, rather than the default SQLite executor. Provider calls carry a result-defining request key: identical concurrent requests share one task, while different requests use a bounded two-slot admission queue per provider capability before executor submission. A caller timeout or cancellation does not stop an already-running shared SDK task; once that task has no foreground waiter it is treated as orphaned, and different requests fail over until it exits. Admission pressure is not recorded as a provider outage or cooldown.

On normal shutdown and startup failure, the lifespan cancels shared workbench builds before calling `DataHub.aclose()`: the runtime rejects new calls, cancels tracked async waiters and executor items that have not started, and waits only for its bounded timeout. A non-quiescent close returns `False` and leaves one tracked deferred close task; provider clients stay open while a worker is active, then close automatically after runtime quiescence without requiring a second shutdown call. Python cannot forcibly terminate a thread inside an uncooperative SDK call. These workers are daemon threads, so a call that never returns does not keep the Python process alive after shutdown reaches interpreter exit. This prevents process-exit hangs, but does not guarantee SDK cleanup, transaction completion, or an immediate in-process worker stop; use provider-level timeouts whenever the SDK supports them.

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
data/ashare_radar.sqlite3.runtime-leader.lock
data/ashare_radar.sqlite3.scheduler.lock
data/ashare_radar.sqlite3.market-scan.lock
data/backups/
data/trading_calendar.json
```

The supported SQLite runtime database is `data/ashare_radar.sqlite3`. Legacy or smoke-test files such as `data/app.db` and `data/smoke.sqlite3*` are disposable local artifacts, not supported runtime state. The `runtime-leader` file is the current runtime ownership path; `.scheduler.lock` and `.market-scan.lock` are legacy compatibility files checked by restore safety. Lock files may record a PID, but ownership comes from the operating-system lock rather than file contents, and a file may remain after clean or unclean exit. Never delete or replace any lock file while a process using that database is running, because recreating its pathname can split cross-process protection. The repository keeps `data/.gitkeep` only so the directory exists; generated databases, locks, calendars, and `data/backups/` are local-only and must remain uncommitted.

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

The backup command prints the created path as JSON and, unless `--destination` is supplied, creates a new timestamped directory under `data/backups/`. Managed bundles are rotated to `ASHARE_RADAR_MAX_RUNTIME_BACKUPS` (default 10, minimum 2); an explicit destination outside that managed directory is not rotated. Creation, verification, rotation, restore, and rollback acquire per-database/per-directory thread locks plus cross-process file leases in a fixed order. Lease acquisition has one 30-second deadline; a timeout reports that backup operations are busy without exposing a local path. This prevents concurrent rotation from deleting a bundle being verified or restored and keeps the final managed count within the configured limit. Verification accepts either the bundle directory or its `manifest.json`. New manifests record only the source database filename, not an absolute machine path; existing version-1 manifests containing an absolute `source_path` remain readable because that display field is not used to locate or verify backup content. API and CLI entrypoints pass the configured backup limit explicitly to creation/restore, keeping rotation consistent with the active application settings.

Restore a backup while the service is stopped. Restore verifies the bundle first, refuses a held unified `runtime-leader` lock, either held legacy `.scheduler.lock`/`.market-scan.lock`, or an active database connection, creates a pre-restore rollback snapshot when a target database exists, replaces the database atomically, and verifies the result again. Rotation protects the selected source bundle and new rollback bundle during that operation:

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
rm -f data/ashare_radar.sqlite3.runtime-leader.lock
rm -f data/ashare_radar.sqlite3.scheduler.lock data/ashare_radar.sqlite3.market-scan.lock
rm -f data/app.db data/smoke.sqlite3 data/smoke.sqlite3-wal data/smoke.sqlite3-shm
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --timeout-graceful-shutdown 5
```

Restore only through `tools/runtime_data.py`: it validates the manifest before replacement and creates a rollback snapshot of the current target. Start the service again only after restore succeeds.

Verify after cleanup or restore:

```bash
curl -sS http://127.0.0.1:8010/api/health
curl -sS http://127.0.0.1:8010/api/data/status
curl -sS 'http://127.0.0.1:8010/api/stock/workbench?symbol=600519'
```

### Retention Cleanup

Opening the Tools view loads `GET /api/local-data/cleanup-preview`. Cleanup removes only rows above configured retention targets for quote history, daily/minute K-lines, stock concepts, cache events, full-market scans/results, task runs, monitor events, alert events, and advice history. Quote rows are limited per symbol, daily K-lines per symbol and adjustment mode, minute K-lines per symbol and interval, and concepts per symbol; the remaining limits are global. Candidate selection uses SQLite window functions and each table is removed with one set-based `DELETE`, so thousands of partitions do not create one query loop per symbol. It does not directly delete watchlist rows, alert rules, stock notes, or advice-review plans. The preview reports per-table and total counts; when advice or alert history is included it sets `requires_user_backup=true`, and the UI asks for backup confirmation.

After reviewing the preview, the UI calls `POST /api/local-data/cleanup?confirm=retention-cleanup`. The preview is advisory: concurrent scheduler activity can change the committed count, so use the returned result as the deletion record. Export user data or create a full runtime backup before cleanup when user-history rows are listed.

Automatic health maintenance calls only the regenerable subset and is throttled by `ASHARE_RADAR_RUNTIME_MAINTENANCE_INTERVAL_SECONDS` (3600 seconds by default), even when health checks run more often. Active market scans and running tasks are never retention candidates. `ASHARE_RADAR_MAX_MARKET_SCAN_RUNS` is a target for unprotected historical runs, not an unconditional hard maximum: each retained or active retry protects its direct parent, while deeper expired ancestry may be removed in the same pass and its `retry_of_run_id` is cleared by SQLite. Scan cleanup runs before task cleanup, so removing scan references can make old tasks eligible immediately. Result rows cascade with their parent run.

After a cleanup commits, the repository checks SQLite free-page pressure. It runs best-effort `VACUUM` only when at least 8 MiB and 25% of allocated pages are reclaimable, using a short busy timeout so a competing reader or writer is not held up. Retention remains committed if compaction is unavailable and a later maintenance pass can retry it.

Trading-calendar reads validate both the writable runtime cache at `data/trading_calendar.json` and the read-only baseline at `app/resources/trading_calendar.json`. A trusted snapshot must contain a non-empty, sorted, unique `YYYY-MM-DD` date list plus matching source, `updated_at`, minimum/maximum date, and count metadata. For a target date or complete interval covered by both snapshots, newer `updated_at` wins and runtime wins an exact timestamp tie. A refreshed runtime therefore governs its overlap even when its maximum date is shorter, while the bundle fills dates outside runtime coverage; a damaged runtime is ignored and an older runtime may still yield to a newer bundle. A successful refresh writes atomically to `data/` only and never mutates the bundle.

The refresh API (`POST /api/data/trading-calendar/refresh`) keeps the explicit synchronous AKShare call off the event loop with `asyncio.to_thread` and reports `ok=false` plus an `error` when fetch or atomic persistence fails. Each synchronous fetch wait is bounded at 15 seconds, and only one underlying fetch may remain in flight per process; retries fail promptly until a timed-out worker actually exits. `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH` remains disabled by default. When enabled, a missing, invalid, or non-empty runtime cache that does not cover the current/target date starts one daemon background refresh and immediately returns the currently trusted bundle decision or a conservative closed result. Background success atomically replaces only the runtime file and clears the in-process calendar cache for later calls. If neither trusted snapshot covers a target, weekday inference is not used: trading checks close conservatively, concrete expected/previous/gap date derivation fails explicitly, and system diagnostics direct the operator to refresh the runtime calendar or update the bundled annual baseline.

Cache and quality checks use Shanghai market time rather than file age alone. Quote and minute data share one session policy: live morning/afternoon rows have a bounded delay; the lunch break requires an 11:25-11:30 snapshot; 13:00-13:15 accepts that morning-close snapshot or a fresh afternoon row; after the grace period, afternoon data is required; and after close, same-trading-day events at or after 14:55 are accepted, including provider-stamped after-hours updates that are not later than the check time. Daily research accepts the explicit `qfq` contract; incompatible adjustment modes and migrated legacy `unknown` rows remain isolated and trigger another provider/cache path rather than entering analysis. Daily K-line cache reuse and quality both continue to require the previous trading day through 15:14:59 and switch to the current trading day at 15:15:00. Weekend and holiday checks use the prior trading day's closing snapshot only when the selected trusted calendar covers the requested date.

SQLite persistence has two ordering guarantees worth preserving during recovery or concurrency testing:

- Quote event timestamps are stored as fixed `Asia/Shanghai` `YYYY-MM-DD HH:MM:SS` text. Snapshot and daily-history upserts use SQLite `ashare_market_epoch()`: a newer event always wins. At an equal event time, a non-fallback quote wins first, then the row with more populated optional quality fields, then a non-older `fetched_at`. The parser also accepts existing UTC ISO/offset cache values, so a fallback, sparse, or late provider completion cannot erase a cleaner/richer quote at the same market instant.
- Advice snapshot de-duplication uses `BEGIN IMMEDIATE` around latest-row lookup and update/insert. A new advice row and its watchlist unread increment commit or roll back together; a repeated identical conclusion increments only `repeat_count`, while a changed/new snapshot increments unread once. Timeline rows retain snapshot/rule/model version, conclusion basis, market time, and data-quality source so legacy or version-changed rows can be shown as non-comparable instead of false changes.
- An empty SSE symbol query reads the watchlist selection off the event loop. Active non-excluded symbols are preferred. Configured seed symbols are used only when the watchlist table has no rows; a table containing only excluded rows returns `422` and requires an explicit symbol instead of silently reactivating seeds.

### Full-Market Scan

The **全市场榜单** workspace is the normal manual entrypoint. It creates a background run and returns immediately; keep the page open to see progress, or inspect the same state from a shell:

```bash
curl -sS -X POST http://127.0.0.1:8010/api/market-scans \
  -H 'Content-Type: application/json' -d '{}'
curl -sS http://127.0.0.1:8010/api/market-scans/latest
curl -sS 'http://127.0.0.1:8010/api/market-scans/1/results?page=1&page_size=100&status=success&sort=rank&order=asc'
curl -sS -X POST http://127.0.0.1:8010/api/market-scans/1/cancel
curl -sS -X POST http://127.0.0.1:8010/api/market-scans/1/retry
```

Replace `1` with the returned run ID. `queued`, `running`, and `cancelling` are active states. `success` means every seeded stock produced a clean ranked row from a current stock pool. `degraded` also covers a locally cached `stale-fallback` stock pool even when every per-stock score succeeds; run diagnostics and `stock_pool_source` retain that provenance. Per-result decisions use structured `quote_fallback_used`, `kline_fallback_used`, `metadata_degraded`, and `degradation_reasons` fields; Chinese display tags are derived and do not control retry or terminal status. `failed` means no usable ranking or a run-level prerequisite failed; `cancelled` and `interrupted` can be retried. Repeated starts return the existing active run rather than creating overlapping work. Runtime-leader startup or takeover changes orphaned active rows to `interrupted` before mutation.

Retry creates a new run whose `retry_of_run_id` points to the frozen original. The repository returns one `MarketScanRetryPlan`, and both manager validation and atomic copy use that same plan; a concurrent change aborts retry rather than mixing decisions. Clean successful rows are copied, while unresolved, fallback-derived, metadata-degraded, or stale-pool rows are recalculated. When pending rows exist, retry validates a complete same-data-date stock pool and refreshes metadata only on those existing pending symbols; it neither adds newly listed symbols nor changes retained clean successes. A fully processed interrupted run has no pending rows, so it is finalized without another stock-pool request. The readable `rule_version` contract includes the base scoring version plus K-line limit, minimum history, minimum data-quality score, and new-stock window. Retry requires an exact contract match; after any of those settings changes, create a new scan instead of mixing scores.

The completed `data_date` is the scan's frozen end-of-day boundary. A quote revision timestamped after run creation remains valid when its Shanghai market date still equals that `data_date`; a quote or K-line from another date is rejected. Retry that still needs market data is accepted only while the source run's date is the current completed trading date. Because providers expose only the current snapshot, an explicit historical `as_of` cannot create a new run; historical rankings are read only from already persisted snapshots. The `task_run` row is created and attached to its queued scan in one transaction. Scan terminal state, ranking/count validation, and the linked task terminal update also commit together. Terminal writes retry only SQLite `BUSY`/`LOCKED` errors, at most three attempts with bounded backoff. If all attempts fail, the owning process records the run; once its local worker is gone and it still holds unified leadership, a later status or scan operation converges the row to `interrupted`. Another instance cannot perform that local recovery, while a crash/takeover still uses the startup reconciliation path.

The stock pool has no fixed 5,000-row cap. Provider rows are canonicalized, required identity/provenance fields are validated, and symbols are de-duplicated before both coverage calculation and persistence; those two steps use the same normalized set. It must satisfy configured total and per-market SH/SZ/BJ minimums; a provider that lacks a required market is skipped. When a recent authoritative snapshot exists, a candidate must also retain at least 90% of its total and each market count once the comparison floor is reached. This drift guard catches plausible-looking truncation that still clears static minimums, while normal small listing changes remain allowed. Shanghai/Shenzhen B shares are excluded. AKShare exchange listing APIs are the default source. If the BSE listing endpoint is unavailable, the adapter tries the AKShare Eastmoney BSE list and then the independently paged Sina `hs_bjs` node; every Sina page is count-validated, so a truncated response fails instead of becoming a partial pool. Tushare can provide a token-backed fallback. A full scan may use only the normal fresh-cache window, while the 30-day stale stock-master fallback remains limited to keyword/profile lookup. An authoritative three-market response uses one `BEGIN IMMEDIATE` delete/write/count-verification transaction, so failure rolls back to the previous pool. Full stock-pool calls use their own longer timeout because exchange-list fallbacks can require several sequential requests. Delisted rows are excluded. ST/new listings remain tagged; an unavailable listing date is structured metadata degradation instead of silently declaring the stock old. A pool-coverage failure occurs before any per-stock downloads, so inspect provider capability status and cached stock-pool counts rather than lowering guards casually.

On trading days, a manual request before 15:15 is rejected because the current daily bar is incomplete. Every score uses bars no later than the run's completed cutoff and a quote whose event date matches the expected trading date. A current zero-volume quote/K-line pair, or a missing quote with a current zero-volume K-line, is recorded as possibly suspended; a current actively traded quote paired with stale K-lines is instead a data error. Suspended/stale stocks, histories shorter than the configured minimum, non-`qfq` data, missing liquidity inputs, low-quality data, malformed rows, and genuine per-symbol coverage misses are stored as `skipped`/`missing`, not zero scores. A `degraded` run is therefore usable but must be read together with coverage, issue counts, and per-result structured fallback/metadata provenance.

The first uncached pass may make thousands of daily-K requests and can take tens of minutes or longer depending on providers. Keep one Uvicorn worker, begin with the defaults, and change batch/concurrency only after observing provider latency and error rates. The daily-K primary path uses Tencent's current `newfqkline` endpoint for SH/SZ/BJ. Normal provider fallback then reaches AKShare; if its own history request and the Eastmoney direct fallback both fail, the rate-limited Sina forward-adjusted client is the final fallback. Every accepted sequence must still satisfy the shared `daily-kline.v1`/`qfq` contract.

Quote work is one bounded batch at a time; K-line work is limited by a semaphore, per-symbol timeout, retries, and backoff. Per-symbol retries apply only to symbol-specific failures. A quote-batch failure or an unavailable complete daily-K provider chain is raised immediately to the batch recovery loop, so thousands of symbol tasks cannot start independent recovery sleeps or bypass the shared budget. Only the affected rows remain `pending`; the runner retries that pending subset up to `ASHARE_RADAR_MARKET_SCAN_BATCH_RETRY_ATTEMPTS`, waiting only while the cumulative actual sleep for the scan remains within `ASHARE_RADAR_MARKET_SCAN_PROVIDER_WAIT_BUDGET_SECONDS`. If the chain does not recover, the run becomes `failed` and remains retryable, with those rows still pending and no bulk increase in `missing_count`. Clean rows from the same work remain reusable. Set the wait budget to `0` to fail immediately after the configured batch attempts without sleeping.

Quote snapshots/history and daily K-lines persist `fallback_used`, so a cached second pass cannot silently turn a degraded source into clean success. The next business-date pass uses an overlap-verified incremental K-line refresh when compatible cache exists and preserves fallback provenance per row. Corporate-action adjustment differences automatically force a full refresh, and repository-level vintage checks prevent an older `as_of` or same-date older version from replacing a newer sequence.

Set both `ASHARE_RADAR_SCHEDULER_ENABLED=1` and `ASHARE_RADAR_MARKET_SCAN_AUTO_ENABLED=1` to enable one after-close attempt per data date. The effective start time is the later of the configured schedule and 15:15. Active/manual work suppresses automatic overlap; a failed automatic/retry attempt is not recreated on every scheduler tick, and a same-day manual cancellation is not overridden automatically. Run snapshots are retained with their results; the configured run count is a convergence target with active/retained-retry ancestry safety exceptions, as described under Retention Cleanup. Full-market scoring never calls the LLM.

The browser revalidates all static assets with `Cache-Control: no-cache`, and the scan entrypoint plus contracts/controller/polling/view modules share one import-map version token. The scan controller validates API shapes before replacing state and runs only one poll timer: an active run refreshes every two seconds, while a visible idle workspace checks `/latest` every 30 seconds so work started elsewhere can be discovered. It applies bounded exponential backoff, falls back to `/latest` after a run `404` or repeated refresh failures, and retries immediately on `online`; hiding or leaving the workspace stops its timer. Discovering a new run resets the old page/result snapshot. One polite live region announces meaningful milestones; progress keeps its ARIA busy/value/label state synchronized. If the UI appears stale after a deployment, inspect the network panel for one consistent scan-module version rather than disabling browser cache globally.

Troubleshooting order:

1. Check `/api/market-scans/latest` for `last_error`, counts, and the current message.
2. Check `/api/data/status`, `/api/system/diagnostics`, recent task runs, and provider capability failures.
3. Query results with `status=missing`, then `status=skipped`, to group concrete per-symbol reasons.
4. Retry after correcting a same-data-date source/network issue. A new linked run retains only clean successful rows and resets non-success or explicitly degraded rows. If the completed trading date has advanced, start a new scan instead.
5. If a run remains active after an unclean process exit, let one process acquire the unified runtime-leader lock; startup or standby takeover reconciles it to `interrupted`. Do not edit status rows manually.

## 3. Diagnostics and Browser Notifications

### System Diagnostics

The data-source/monitoring panel reads `GET /api/system/diagnostics`. The response separates cache fetch activity from market-data freshness and includes storage budget, scheduler state, provider status, table counts, bounded warnings, and remediation suggestions. Storage reports `sqlite_size_bytes` separately from managed `backup_size_bytes`/bundle count, with their sum as the budgeted total. Row details separate quotes, daily/minute K-lines, full-market scan runs/results, other cache, other runtime, and user data instead of exposing only broad totals. Freshness covers quotes, daily/minute K-lines, the stock pool, and plate metadata; a non-empty stock-pool or plate cache without a usable update timestamp is reported as missing freshness metadata rather than healthy. Storage warns at 80% of `ASHARE_RADAR_MAX_DATABASE_SIZE_MB` and reports an over-budget state above the configured limit. The monitoring surface also reads data-source status, recent task runs, and monitor events; its normal refresh interval is 15 seconds.

Use diagnostics to distinguish a stale market snapshot from a recent failed fetch, identify capability-level provider failures, see when fewer than two real-time quote sources are enabled, detect demo data, check trading-calendar availability/coverage, and confirm that alerts are not waiting on a stopped scheduler. Calendar status distinguishes `runtime_cache`, `bundled_baseline`, `out_of_coverage`, and `unavailable`; the latter two skip calendar-dependent freshness conclusions and close trading tasks conservatively. A task result of `degraded` means the run completed with fallback or incomplete source coverage and should not be interpreted as either full success or total failure. A scheduler with `standby=true` is not stopped: another process owns unified runtime leadership, so this process deliberately runs neither scheduler nor scanner. Diagnostics are read-only; use the explicit task controls, calendar refresh, backup, or cleanup operations for changes.

### Browser Notifications

Create and enable alert rules, then click `启用桌面提醒` and grant browser permission. Permission is requested only from that user action. Once enabled, the page polls alert events in pages of up to 50 every 30 seconds and notifies only new `触发` events. The authoritative keyset cursor is the monotonically increasing event `id`; the legacy `after_created_at` value does not participate in ordering. The first successful poll establishes a no-backfill baseline. Up to three new events are shown individually, while a larger burst becomes one summary notification. Clicking a notification focuses the page.

Click the active notification control to disable delivery. The enabled/disabled preference is stored in browser local storage and restored after a page or application restart. Disabling stops polling, invalidates any in-flight delivery, and clears the prior cursor; re-enabling establishes a new baseline, so events created during the disabled period are not replayed. Polling failure leaves the persisted cursor unchanged, while a notification-construction failure advances only through the successfully delivered prefix so the failed event and all later events remain eligible for ordered retry.

Notifications require the page to remain open; there is no service worker or operating-system background delivery after the page closes. A denied permission must be changed in browser settings. If event polling fails, alert evaluation and persistence continue, while the notification control shows a synchronization warning.

## 4. Environment Variables

Use the `ASHARE_RADAR_*` namespace for new configuration. Legacy aliases are accepted where listed for local compatibility. Process environment values take precedence. For the five allowlisted `ASHARE_RADAR_LLM_*` names only, the application falls back to simple top-level assignments in `$HOME/.zshrc`; it parses that file without sourcing or executing it and ignores command substitutions, nested shell blocks, and unrelated names. When that file contains `ASHARE_RADAR_LLM_API_KEY`, it must be owned by the current user and have no group/other permissions; run `chmod 600 "$HOME/.zshrc"` before startup. It does not read `.env` files, project configuration, user-data imports, or browser storage for credentials. Settings are captured by the application container, and scheduler intervals/task registration are not hot-reloaded. Restart the single process after changing configuration.

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
| `ASHARE_RADAR_STOCK_POOL_PROVIDER_TIMEOUT_SECONDS` | `60` | - | Timeout for one full stock-pool provider call; range 1-300 seconds. Kept separate from short quote/K-line calls because exchange-list fallbacks may require several pages. |
| `ASHARE_RADAR_STOCK_CONCEPT_CACHE_SECONDS` | `21600` | `STOCK_CONCEPT_CACHE_SECONDS` | Stock concept cache TTL. |
| `ASHARE_RADAR_PROVIDER_FAILURE_COOLDOWN_SECONDS` | `90` | `PROVIDER_FAILURE_COOLDOWN_SECONDS` | Provider retry cooldown after failures. |
| `ASHARE_RADAR_MARKET_SCAN_AUTO_ENABLED` | `0` | - | Enable the after-close full-market scan. |
| `ASHARE_RADAR_MARKET_SCAN_SCHEDULE_HOUR` | `16` | - | Automatic-scan local hour; the 15:15 daily publication floor still applies. |
| `ASHARE_RADAR_MARKET_SCAN_SCHEDULE_MINUTE` | `30` | - | Automatic-scan local minute. |
| `ASHARE_RADAR_MARKET_SCAN_BATCH_SIZE` | `50` | - | Symbols per persisted scan batch; range 1-500. |
| `ASHARE_RADAR_MARKET_SCAN_CONCURRENCY` | `5` | - | Maximum concurrent per-symbol K-line jobs; range 1-32. |
| `ASHARE_RADAR_MARKET_SCAN_KLINE_LIMIT` | `260` | - | Requested completed `qfq` daily rows per symbol; range 60-1000. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_HISTORY_ROWS` | `60` | - | Minimum complete daily rows required for ranking; range 60-260 and no greater than the K-line limit. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE` | `50` | - | Results below this 0-100 quality floor are skipped. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_UNIVERSE_COUNT` | `4000` | - | Reject a purported full-market pool below this total count. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_SH_COUNT` | `1800` | - | Reject a scan pool with fewer Shanghai A shares. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_SZ_COUNT` | `2500` | - | Reject a scan pool with fewer Shenzhen A shares. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_BJ_COUNT` | `200` | - | Reject a scan pool with fewer Beijing A shares. |
| `ASHARE_RADAR_MARKET_SCAN_SYMBOL_TIMEOUT_SECONDS` | `30` | - | Timeout for one symbol's K-line attempt; range 0.1-300 seconds. |
| `ASHARE_RADAR_MARKET_SCAN_QUOTE_BATCH_TIMEOUT_SECONDS` | `60` | - | Outer timeout for one quote batch; range 0.1-600 seconds. |
| `ASHARE_RADAR_MARKET_SCAN_RETRY_ATTEMPTS` | `2` | - | K-line attempts per symbol; range 1-5. |
| `ASHARE_RADAR_MARKET_SCAN_RETRY_BACKOFF_SECONDS` | `1` | - | Linear delay multiplier between K-line attempts; range 0-30 seconds. |
| `ASHARE_RADAR_MARKET_SCAN_BATCH_RETRY_ATTEMPTS` | `3` | - | Attempts for the pending subset of a batch after a system-wide quote/daily-K chain outage; range 1-5 and independent of per-symbol K-line retries. |
| `ASHARE_RADAR_MARKET_SCAN_PROVIDER_WAIT_BUDGET_SECONDS` | `120` | - | Cumulative actual provider-recovery sleep budget across one scan's pending work; range 0-600 seconds. Exhaustion fails the run while affected rows remain pending; `0` disables recovery sleeps. |
| `ASHARE_RADAR_MARKET_SCAN_NEW_STOCK_DAYS` | `120` | - | Calendar-day window used only for the new-stock tag; range 1-730. |
| `ASHARE_RADAR_SCHEDULER_ENABLED` | `1` | `SCHEDULER_ENABLED` | Local refresh scheduler switch. |
| `ASHARE_RADAR_SCHEDULER_QUOTE_INTERVAL_SECONDS` | `30` | `SCHEDULER_QUOTE_INTERVAL_SECONDS` | Quote refresh interval. |
| `ASHARE_RADAR_SCHEDULER_KLINE_INTERVAL_SECONDS` | `900` | `SCHEDULER_KLINE_INTERVAL_SECONDS` | K-line refresh interval. |
| `ASHARE_RADAR_SCHEDULER_PLATE_INTERVAL_SECONDS` | `300` | `SCHEDULER_PLATE_INTERVAL_SECONDS` | Plate refresh interval. |
| `ASHARE_RADAR_SCHEDULER_HEALTH_INTERVAL_SECONDS` | `45` | `SCHEDULER_HEALTH_INTERVAL_SECONDS` | Data-health check interval. |
| `ASHARE_RADAR_SCHEDULER_KLINE_SYMBOLS_LIMIT` | `5` | `SCHEDULER_KLINE_SYMBOLS_LIMIT` | Per-cycle K-line symbol cap. |
| `ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | `5` | `SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | Bounded scheduler stop wait; unified runtime leadership is not released while unfinished service work is still shutting down. |
| `ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS` | `120` | `MAX_QUOTE_HISTORY_ROWS` | Per-symbol daily quote-history cap; minimum `120`, matching the analysis window. |
| `ASHARE_RADAR_MAX_DAILY_KLINE_ROWS` | `260` | `MAX_DAILY_KLINE_ROWS` | Per-symbol and adjustment-mode daily K-line cap; must cover `ASHARE_RADAR_MARKET_SCAN_KLINE_LIMIT`. |
| `ASHARE_RADAR_MAX_MINUTE_KLINE_ROWS` | `20000` | `MAX_MINUTE_KLINE_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_STOCK_CONCEPT_ROWS` | `20000` | `MAX_STOCK_CONCEPT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_TASK_RUN_ROWS` | `2000` | `MAX_TASK_RUN_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_MARKET_SCAN_RUNS` | `30` | - | Target count for unprotected historical scan runs. Active runs and ancestors referenced by retained retries are temporary safety exceptions; expired chains converge leaf-to-root and child results cascade with removed runs. |
| `ASHARE_RADAR_MAX_MONITOR_EVENT_ROWS` | `3000` | `MAX_MONITOR_EVENT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_CACHE_EVENT_ROWS` | `5000` | `MAX_CACHE_EVENT_ROWS` | Runtime retention cap for cache/provider events. |
| `ASHARE_RADAR_MAX_ALERT_EVENT_ROWS` | `5000` | `MAX_ALERT_EVENT_ROWS` | Runtime retention cap for alert events. |
| `ASHARE_RADAR_MAX_ADVICE_HISTORY_ROWS` | `20000` | `MAX_ADVICE_HISTORY_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_DATABASE_SIZE_MB` | `512` | - | Local SQLite capacity budget in MiB; minimum `16`. Diagnostics warn at 80%. |
| `ASHARE_RADAR_RUNTIME_MAINTENANCE_INTERVAL_SECONDS` | `3600` | - | Minimum interval between automatic regenerable-data maintenance passes; range 60-604800 seconds. |
| `ASHARE_RADAR_MAX_RUNTIME_BACKUPS` | `10` | - | Managed runtime backup bundles retained per database; range 2-100. API/CLI backup and restore operations pass this limit explicitly. |
| `ASHARE_RADAR_ADVICE_HISTORY_DEDUPE_SECONDS` | `180` | `ADVICE_HISTORY_DEDUPE_SECONDS` | Advice-history de-duplication window. |
| `ASHARE_RADAR_QUOTE_STALE_WARNING_SECONDS` | `900` | `QUOTE_STALE_WARNING_SECONDS` | Quote freshness warning threshold. |
| `ASHARE_RADAR_QUOTE_CONSISTENCY_WARNING_PCT` | `1.0` | `QUOTE_CONSISTENCY_WARNING_PCT` | Multi-source price-difference warning threshold. |
| `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH` | `0` | `TRADE_CALENDAR_AUTO_FETCH` | Non-blocking single-flight background refresh when runtime is missing, invalid, stale for the current date, or cannot cover a target. The triggering call uses the current bundle/closed decision; later calls see a successful atomic runtime update. |

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
- Full-market scanning distinguishes a stock-specific no-data outcome from an unavailable provider chain. The former may become `missing`/`skipped`; a system-wide quote or daily-K outage keeps affected rows pending, applies the bounded batch retry/wait policy, and leaves a failed run available for explicit retry.
- Client request-shape errors remain `422`. A Pydantic `ValidationError` raised while constructing an internal response/model is treated as unavailable internal data: the server logs the traceback and returns generic `503` detail without the rejected value. SQLite `DatabaseError` and provider/runtime failures also return `503`, with their public text sanitized.
- On mobile, the source DOM and focus order remain query, workspace, then local controls; tab/tabpanel, validation, and chart-filter ARIA state must stay synchronized. Watchlist, alert, and note persistence has an independent request scope per write: navigation or another write may suppress stale UI/readback work but must not abort a server commit already in flight. Advice timeline and minute chart loads have independent abort controllers and sequence counters; timeline ownership immediately renders the requested symbol's loading state and rejects stale A-B-A completions. Minute 204/205, empty, `null`, non-object, wrong-symbol, and wrong-interval responses clear minute state and show unavailable rather than leaving a loading or mismatched chart. Daily 20/60/120/240 switching redraws the existing 240-row payload, while only a new minute interval makes one minute request.
