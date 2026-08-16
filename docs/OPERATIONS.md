# Operations Guide

## 1. Local Runtime

Use Python 3.12 and a virtual environment owned by the checkout. Set `PROJECT_ROOT` to the repository location; the default below assumes a checkout directly under `$HOME`. The web process does not require Node.js; development and CI support Node.js 22.x or 24.x with npm 10.x or 11.x, and `.node-version` selects Node 22 by default.

```bash
export PROJECT_ROOT="${PROJECT_ROOT:-$HOME/AShareRadar}"
cd "$PROJECT_ROOT"
python3.12 -m venv "$PROJECT_ROOT/.venv"
source "$PROJECT_ROOT/.venv/bin/activate"
export PYTHON="$PROJECT_ROOT/.venv/bin/python"
export PYTHONNOUSERSITE=1
$PYTHON -m pip install --require-hashes -r requirements-lock.txt
```

After installing development dependencies and `npm ci`, verify the complete runtime declaration rather than relying on the current shell's version manager:

```bash
$PYTHON tools/runtime_contract.py
```

The command checks Python 3.12.x, Node 22.x/24.x, npm 10.x/11.x, and declaration consistency across `.python-version`, `.node-version`, and `package.json`.

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

Time semantics do not follow the host timezone. Trading sessions, market dates, and quote/K-line event interpretation use `Asia/Shanghai`; audit fields are written as UTC ISO 8601 text ending in `Z`; TTLs, retry deadlines, throttles, and measured durations use monotonic/performance clocks. During schema initialization, one idempotent migration interprets legacy naive audit text as Shanghai time and converts only allowlisted audit columns to UTC. It does not rewrite market-event fields, and existing aware ISO values remain readable. Invalid legacy audit text aborts and rolls back the migration instead of recording a false success marker.

Treat the first UTC audit-time migration as an offline schema upgrade. Do not perform a rolling upgrade: stop every old AShareRadar process that can access the SQLite file, not only the HTTP listener, before starting the new version. The initializer refuses migration while the runtime-leader lease is held, requires an explicit `Settings` object for an existing legacy database so `ASHARE_RADAR_LEGACY_AUDIT_TIMEZONE` cannot be bound after conversion, and checks for free space equal to the larger of 64 MB or twice the database size. Before shutdown, create and verify a full runtime backup with `tools/runtime_data.py`; keep that backup until the migrated database has passed `quick_check`, foreign-key validation, and application smoke tests. The migration logs its elapsed time but never logs the database path.

All later schema migrations belong to that same stopped-service window. Startup ordering is fixed:

1. If `market_scan_run` does not yet have all three seal columns, remove any stale market-scan immutability triggers before base DDL so a trigger can never reference a missing column.
2. Create the base and Discovery schema, then run compatibility columns, legacy data migrations and indexes inside one rollback-capable `BEGIN IMMEDIATE`. `20260813_market_scan_snapshot_digest_v3` is the last compatibility migration: it resets every pre-existing `success|degraded` graph to `snapshot_seal_origin=legacy_backfill`, creates `market-scan-snapshot-digest-v2`, verifies every graph, installs five immutability triggers, and writes the migration marker last.
3. Apply the advice-review immutable-ledger and Paper schemas. The review upgrade adds plan tombstone/digest columns, creates and backfills canonical current revision rows, and rebuilds the former mutable result key as append-only attempts while preserving result IDs.
4. Apply Strategy Lab last. `20260813_strategy_execution_source_digest_v2` first verifies surviving source run seals, then adds/backfills `source_snapshot_digest`, `source_snapshot_seal_origin`, and `source_snapshot_verification_status`. A legacy execution with no surviving source run remains null/null/`legacy_unverified`; it is not silently upgraded. The migration checks the exact verified-versus-orphan state, restores append-only/source-insert triggers and records its marker last in its own transaction. Even with the marker present, startup repeats the safe-state and trigger checks.

If any phase fails, keep the service stopped and restore the verified pre-upgrade backup rather than manually completing half the schema, dropping triggers or editing a digest/status. The 2026-08-13 real-copy smoke sequence exposed both ordering hazards before production restart: a first attempt had created seal triggers before the seal columns, and a later attempt found legacy execution IDs 1–3 referring to deleted run 16. The current ordering and `legacy_unverified` state are the recovery contract for those cases. The pre-migration copy passed `quick_check`; its `/tmp` location was ephemeral evidence, not a durable retention promise.

Synchronous container construction, route-level repository/diagnostic calls, workflow cache access, market-sampling and stock-confirmation event writes, SSE watchlist fallback reads, and scheduler SQLite work are offloaded from the asyncio event-loop thread. Blocking provider SDK work uses a separate four-worker `DaemonThreadPoolExecutor` owned by each `ProviderRuntime`, rather than the default SQLite executor. Provider calls carry a result-defining request key: identical concurrent requests share one task, while different requests use a bounded two-slot admission queue per provider capability before executor submission. A caller timeout or cancellation does not stop an already-running shared SDK task; once that task has no foreground waiter it is treated as orphaned, and different requests fail over until it exits. Admission pressure is not recorded as a provider outage or cooldown.

On normal shutdown and startup failure, the lifespan cancels shared workbench builds before calling `DataHub.aclose()`: the runtime rejects new calls, cancels tracked async waiters and executor items that have not started, and waits only for its bounded timeout. A non-quiescent close returns `False` and leaves one tracked deferred close task; provider clients stay open while a worker is active, then close automatically after runtime quiescence without requiring a second shutdown call. Python cannot forcibly terminate a thread inside an uncooperative SDK call. These workers are daemon threads, so a call that never returns does not keep the Python process alive after shutdown reaches interpreter exit. This prevents process-exit hangs, but does not guarantee SDK cleanup, transaction completion, or an immediate in-process worker stop; use provider-level timeouts whenever the SDK supports them.

Browser mutation requests are same-origin protected before route side effects. For `POST`, `PUT`, `PATCH`, `DELETE`, and `GET` with truthy `refresh`, a request carrying browser origin metadata is accepted only when the Host-derived request origin is configured as allowed and any supplied `Origin` or `Referer` is also allowed; when both source headers are absent, `Sec-Fetch-Site: cross-site` is rejected. This rejects Host-header/DNS-rebinding attempts even when an attacker supplies an Origin matching that hostile Host. Ordinary read-only `GET`/`HEAD` requests are unaffected, and CLI/health tooling without browser origin metadata remains supported. The same configured origins are used for CORS and mutation trust; adding an origin therefore grants browser write access and should be done narrowly.

Check status:

```bash
screen -ls
lsof -nP -iTCP:8010 -sTCP:LISTEN
curl -sS http://127.0.0.1:8010/api/health
curl -sS http://127.0.0.1:8010/api/health/live
curl -sS http://127.0.0.1:8010/api/health/ready
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

The Tools view exports a versioned JSON bundle containing only the allowlisted local watchlist, alert rules/events, stock notes, advice history, review plan projections/revision ledgers/evaluation attempts, watchlist-scan history, Discovery state, paper account/strategies/runs/results/trades/equity/events, and saved StrategySpec roots/revisions that exist in the current schema. Market caches, provider status, scheduler/monitor records, settings, credentials, and full-market runtime provenance remain excluded. The browser rejects files larger than 50 MB.

Import supports `merge` and `replace`. In merge mode, supported stable keys identify logical mutable rows and the source bundle wins when a stable key already exists. Immutable `advice_review_plan_revision` and `advice_review_result` rows are different: an exact match is idempotent, while the same stable key with different content rejects the whole import. A review-plan bundle must include its revision ledger. Preflight checks canonical revision JSON/SHA-256, the current plan projection's revision/digest, each result's plan/advice/revision binding, and paper-plan references. An older incoming plan revision cannot roll back the target. For surrogate-key collisions, a new target ID is allocated and every bundled child foreign key follows it; review imports additionally rewrite the canonical revision payload's embedded advice ID, recompute its digest, and propagate that digest to the plan, results, and paper strategies. Related parents and children therefore travel as a group. Re-run dry-run after any file, mode, timezone, or target-state change.

Column order may differ between the bundle and target database, but column sets, declared types, and primary-key definitions must be compatible. New exports declare `utc-fixed` audit-time semantics and reject non-fixed audit values. Older version-1 bundles without metadata remain readable when audit values are aware; naive values require an explicit `legacy_audit_timezone`, and the preview claim is bound to it. `legacy-naive` bundles use their declared IANA source timezone. Pre-provenance review rows receive only registered conservative compatibility defaults; legacy-unverified results remain audit-readable but cannot become verified metrics. Both modes perform normalized-shape, stable-key, relationship, digest, and `foreign_key_check` validation inside one transaction and recheck review relationships after remapping/writes. Replace still requires every available user-data table and removes absent target rows. The UI enables commit only for the exact successful dry run; create a current export or full runtime backup before replace.

The equivalent endpoints are `POST /api/local-data/export` and `POST /api/local-data/import?mode=merge|replace&dry_run=true|false`.

Advice-review plans can be archived from the Replay workspace or through `DELETE /api/reviews/plans/{plan_id}?expected_revision=<revision>`. This is a revision-CAS soft delete: it removes the plan from active/detail/due/summary views but retains the source advice snapshot, current projection, every plan revision, and every evaluation attempt. There is currently no public unarchive endpoint, so export or back up before an intentional archive even though its audit ledger remains in SQLite.

### Runtime Backup and Restore

Use the runtime-data tool for a consistent full-database snapshot. It uses SQLite's backup API, writes `runtime.sqlite3` plus `manifest.json`, and records SHA-256, schema/user versions, table counts, user-table counts, and verified `PRAGMA integrity_check`; verification also requires an empty `PRAGMA foreign_key_check`:

```bash
$PYTHON tools/runtime_data.py backup
$PYTHON tools/runtime_data.py verify data/backups/ashare_radar_TIMESTAMP
```

The backup command prints the created path as JSON and, unless `--destination` is supplied, creates a new timestamped directory under `data/backups/`. Managed bundles are rotated to `ASHARE_RADAR_MAX_RUNTIME_BACKUPS` (default and minimum 2); an explicit destination outside that managed directory is not rotated. Creation, verification, rotation, restore, and rollback acquire per-database/per-directory thread locks plus cross-process file leases in a fixed order. Lease acquisition has one 30-second deadline; a timeout reports that backup operations are busy without exposing a local path. This prevents concurrent rotation from deleting a bundle being verified or restored and keeps the final managed count within the configured limit. Verification accepts either the bundle directory or its `manifest.json`. New manifests record only the source database filename, not an absolute machine path; existing version-1 manifests containing an absolute `source_path` remain readable because that display field is not used to locate or verify backup content. API and CLI entrypoints pass the configured backup limit explicitly to creation/restore, keeping rotation consistent with the active application settings.

Restore a backup while the service is stopped. Restore verifies the bundle first, refuses a held unified `runtime-leader` lock, either held legacy `.scheduler.lock`/`.market-scan.lock`, or an active database connection, creates a pre-restore rollback snapshot when a target database exists, replaces the database atomically, and verifies the result again. It also takes the target database's market-scan artifact lease, including when the target is initially absent. Because a runtime backup contains SQLite but not side artifacts, restore fails before replacement if any sibling `market-scan-probability`, probability source/outcome/fit, future-range or individual-probability managed directory is populated, if the fixed future-range summary exists, or—for the canonical project database with an empty runtime-local individual directory—if the tracked docs fallback is populated. A custom database never inherits that project fallback. Restore the database and these artifacts from one separately audited bundle, or explicitly archive/remove the artifacts before retrying; do not bypass this by renaming through a symlink or alternate database path. Rotation protects the selected source bundle and new rollback bundle during that operation:

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

Opening the Tools view loads `GET /api/local-data/cleanup-preview`. Cleanup removes only rows above configured retention targets for quote history, daily/minute K-lines, stock concepts, cache events, UTC-hour reliability buckets, full-market scans/results, task runs, monitor events, alert events, and advice history. Quote rows are limited per symbol, daily K-lines per symbol and adjustment mode, minute K-lines per symbol and interval, and concepts per symbol; the remaining limits are global. Ordinary candidate selection uses SQLite window functions and set-based deletes. A sealed full-market run is different: it is one parent/result graph and goes through the verified whole-graph deletion gateway described below. Cleanup does not directly delete watchlist rows, alert rules, stock notes, advice-review plans, or research artifact files. The preview reports per-table and total counts; when advice or alert history is included it sets `requires_user_backup=true`, and the UI asks for backup confirmation.

After reviewing the preview, the UI calls `POST /api/local-data/cleanup?confirm=retention-cleanup`. The preview is advisory: concurrent scheduler activity can change the committed count, so use the returned result as the deletion record. Export user data or create a full runtime backup before cleanup when user-history rows are listed. Also create and verify a full runtime backup whenever the preview includes `market_scan_run`: a snapshot digest detects change but is not an archive and cannot reconstruct an authorized deleted graph.

Automatic health maintenance calls only the regenerable subset and is throttled by `ASHARE_RADAR_RUNTIME_MAINTENANCE_INTERVAL_SECONDS` (3600 seconds by default), even when health checks run more often. Explicit cleanup applies `ASHARE_RADAR_MAX_MARKET_SCAN_RUNS` as the newest-run keep window. It is not an unconditional physical-row ceiling: every unreferenced sealed graph outside the window is eligible, which strictly bounds unreferenced published history, but active work and genuine references can keep the database above target.

Before choosing candidates, cleanup takes a fail-closed snapshot of a closed catalog relative to the database sibling: probability result/source/outcome/fit, future-range artifacts, the fixed future-range summary, and the runtime-local individual-probability assessment. Each directory is strictly and stably enumerated. Only for the canonical project database, and only when its primary individual directory contains no files, does cleanup strictly enumerate the tracked `docs/research/artifacts` fallback; a custom database never borrows that evidence. Every directory entry must be attributable and regular, match its applicable filename/content/integrity contract, and expose exact source IDs through explicit recursive `*run_id`/`*run_ids` fields. Bounded artifacts are deeply verified. A probability artifact larger than 64 MiB instead uses the bounded-memory stream verifier: it checks the canonical envelope, `sha256`/notice/filename digest, legacy `payload` or current `generated_at+payload` scope, nested generation-time encoding, singleton fallback, scalar run IDs, and sorted unique manifests even when a long manifest crosses read boundaries. It rejects equal-length semantic tamper and file-size drift; it does not trust the filename alone. A fit artifact pins only its explicit member IDs, not every integer below `through_run_id`. The future-range summary must be a regular bounded object with schema `market-scan-future-range-evaluation-summary-v1`, an exact `artifact_count`, and valid per-item run ID, integrity digest and `offline_replay_verified=true`; only those explicit runs pin. An unknown/unsafe/malformed/tampered artifact, invalid summary, or catalog fingerprint change aborts cleanup; this path never deletes an artifact.

Cleanup and managed artifact writers serialize through one re-entrant thread plus cross-process lease keyed by the canonical non-link, single-link database path. Cleanup acquires it before the first catalog scan and keeps it through commit. A managed writer acquires it before its final read-only shared action-source recheck and keeps it through atomic publication. The sidecar lives under fixed owner-only `/tmp/ashare-radar-artifact-locks-<uid>` and database-parent/sidecar identities are rechecked while held; one thread cannot nest different database leases. This assumes a trusted same-UID filesystem account. It is not a security boundary against another same-UID process deliberately renaming or replacing that root.

The six managed writer families must bind their exact project directory to canonical `data/ashare_radar.sqlite3`; aliases, symlinked parents, hard-linked databases, alternate databases, and a batch mixing managed/external paths fail closed. External paths remain offline/self-contained and do not gain project-managed semantics. Multi-file publication is all-or-nothing: newly linked files alone are journaled by path, byte size and SHA-256. If any writer step or final namespace check fails, rollback visits every journal item in reverse, stream-verifies the exact regular single-link file, unlinks it, and fsyncs the directory. A pre-existing identical immutable file is not journaled and must remain. If rollback itself cannot prove an exact identity, the command reports incomplete rollback and operators must inspect the named managed directory before retrying.

Inside one `BEGIN IMMEDIATE`, database reachability protects every dynamically discovered foreign key into `market_scan_run` and every declared Discovery/strategy semantic reference. A probability-capture outbox row is a live pin only while `status IN ('pending','processing')`; `succeeded` and `skipped` terminal rows cascade with their run and do not replace the separately verified artifact-file pin. Retry lineage uses every retry child outside the current deletion candidates—including newest-window, `queued`, `running`, `cancelling`, database-referenced and file-referenced rows—and every directly protected candidate as a root. Parent traversal crosses candidate and noncandidate rows, pins every reachable candidate ancestor, and uses a visited set to terminate a legacy cycle; this prevents `ON DELETE SET NULL` from detaching a retained lineage. The enforced self-FK prevents new missing parents, while a pre-existing missing parent ends only that traversal branch. A run's own `task_run_id` points from the run to its owner and does not pin it. Immediately before published deletion and again before commit, the file snapshot must still match. Every published candidate must pass its v2 whole-graph seal. The gateway verifies all five immutability triggers, temporarily drops them, deletes child results and the parent, recreates and verifies the exact trigger set in `finally`, and requires the exact parent count. Tamper, reference drift, missing explicit transaction, injected deletion failure, partial count, or trigger-restoration failure rolls back the whole cleanup. Scan cleanup precedes task cleanup, so an old owner task may become eligible in the same pass. Never reproduce this sequence manually with ad-hoc SQL or leave triggers disabled.

After a cleanup commits, the repository checks SQLite free-page pressure. It runs best-effort `VACUUM` only when at least 8 MiB and 25% of allocated pages are reclaimable, using a short busy timeout so a competing reader or writer is not held up. Retention remains committed if compaction is unavailable and a later maintenance pass can retry it.

Trading-calendar reads validate both the writable runtime cache at `data/trading_calendar.json` and the read-only baseline at `app/resources/trading_calendar.json`. A trusted snapshot must contain a non-empty, sorted, unique `YYYY-MM-DD` date list plus matching source, `updated_at`, minimum/maximum date, and count metadata. For a target date or complete interval covered by both snapshots, newer `updated_at` wins and runtime wins an exact timestamp tie. A refreshed runtime therefore governs its overlap even when its maximum date is shorter, while the bundle fills dates outside runtime coverage; a damaged runtime is ignored and an older runtime may still yield to a newer bundle. A successful refresh writes atomically to `data/` only and never mutates the bundle.

The refresh API (`POST /api/data/trading-calendar/refresh`) keeps the explicit synchronous AKShare call off the event loop with `asyncio.to_thread` and reports `ok=false` plus an `error` when fetch or atomic persistence fails. Each synchronous fetch wait is bounded at 15 seconds, and only one underlying fetch may remain in flight per process; retries fail promptly until a timed-out worker actually exits. `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH` remains disabled by default. When enabled, a missing, invalid, or non-empty runtime cache that does not cover the current/target date starts one daemon background refresh and immediately returns the currently trusted bundle decision or a conservative closed result. Background success atomically replaces only the runtime file and clears the in-process calendar cache for later calls. If neither trusted snapshot covers a target, weekday inference is not used: trading checks close conservatively, concrete expected/previous/gap date derivation fails explicitly, and system diagnostics direct the operator to refresh the runtime calendar or update the bundled annual baseline.

Cache and quality checks use Shanghai market time rather than file age alone. Quote and minute data share one session policy: live morning/afternoon rows have a bounded delay; the lunch break requires an 11:25-11:30 snapshot; 13:00-13:15 accepts that morning-close snapshot or a fresh afternoon row; after the grace period, afternoon data is required; and after close, same-trading-day events at or after 14:55 are accepted, including provider-stamped after-hours updates that are not later than the check time. Daily research accepts the explicit `qfq` contract; incompatible adjustment modes and migrated legacy `unknown` rows remain isolated and trigger another provider/cache path rather than entering analysis. Daily K-line cache reuse and quality both continue to require the previous trading day through 15:14:59 and switch to the current trading day at 15:15:00. Weekend and holiday checks use the prior trading day's closing snapshot only when the selected trusted calendar covers the requested date.

SQLite persistence has two ordering guarantees worth preserving during recovery or concurrency testing:

- Quote event timestamps are stored as fixed `Asia/Shanghai` `YYYY-MM-DD HH:MM:SS` text. Snapshot and daily-history upserts use SQLite `ashare_market_epoch()`: a newer event always wins. At an equal event time, a non-fallback quote wins first, then the row with more populated optional quality fields, then a non-older `fetched_at`. The parser also accepts existing UTC ISO/offset cache values, so a fallback, sparse, or late provider completion cannot erase a cleaner/richer quote at the same market instant.
- Advice snapshot de-duplication uses `BEGIN IMMEDIATE` around latest-row lookup and update/insert. A new advice row and its watchlist unread increment commit or roll back together; a repeated identical conclusion increments only `repeat_count`, while a changed/new snapshot increments unread once. Timeline rows retain snapshot/rule/model version, conclusion basis, market time, and data-quality source so legacy or version-changed rows can be shown as non-comparable instead of false changes.
- An empty SSE symbol query reads the watchlist selection off the event loop. Active non-excluded symbols are preferred. Configured seed symbols are used only when the watchlist table has no rows; a table containing only excluded rows returns `422` and requires an explicit symbol instead of silently reactivating seeds.

### Individual Workbench Cohort and Advice Publication

Inspect the core cohort boundary with:

```bash
curl -sS 'http://127.0.0.1:8010/api/stock/workbench?symbol=600519' \
  | "$PYTHON" -m json.tool
```

A valid response has schema `stock-workbench-v2`, top-level
`research_mode=interactive_shadow`, `production_effect=none`,
`diagnosis_production_effect=none`, and cohort
`advice_persistence=disabled`. The requested/observed symbol, quote identity and
timestamp, all symbol-owning panels and strategy cards, signal date, daily-bar
cutoff and context decision time must agree. Every research child `updated_at`
must be no later than that decision time and on the same Shanghai signal date.
Independent child endpoints apply the same owner/time check. Workbench responses
are `Cache-Control: no-store`;
the internal 8-second computation cache still exists, but it is bound to symbol,
market phase, expected quote date and expected completed-daily-bar date. A server
identity/cohort failure returns the generic research-integrity `409`; a browser
contract failure clears the new load rather than keeping mixed-symbol content.

An ordinary `GET /api/stock/workbench` does not fetch concept-board membership
online. It may show only fresh non-static rows already present in the local
provider cache. If that cache is missing, stale, static-only, or unreadable, the
concept panel degrades explicitly to unavailable and theme/leadership scoring
does not consume those rows. This is optional non-authorizing background without
a receipt or integrity digest; it cannot authorize probability, filtering,
actions, publication, or full-market scanning. There is no new public refresh
route or scheduler in this boundary; internal `refresh=True` compatibility is
not part of the ordinary interactive GET contract.

Opening or refreshing the page must not create an advice-history row. Pre-open
and intraday requests exclude an unpublished current daily bar and keep partial-
session volume confirmation neutral. Formal advice publication is performed only
by the active-research task at or after 15:15. It accepts an official quote event
only from 14:55:00 through 15:00:00 inclusive, requires at least 60 valid completed
daily bars, and then applies trading-day, quote/K-line provenance, fallback,
quality and contract gates. To
audit this boundary, compare the advice timeline before and after repeated
workbench reads; it must be unchanged. Then run the normal after-close scheduled
task in an eligible fixture/environment and confirm that only its accepted items
are saved. The displayed “个股诊断” is a non-persisted research diagnosis and must
not be treated as a second formal recommendation.

Optional context is also cohort-bound. Different-date or fallback breadth quotes,
a wrong-symbol/wrong-date order book, and wrong-symbol/wrong-date/fallback concept
rows are excluded with sanitized warnings. Missing volume, structure/chip,
valuation, order-pressure, fund-flow, risk or calibration evidence is shown with
a typed unavailable/data-nature state, cannot enter a directional/current score,
and must clear stale numeric/directional DOM content on redraw. Missing resistance/
support/MA20/ATR evidence leaves target, stop and reward/risk ratio unavailable.
Factor calibration reports `execution_evidence_unavailable` unless its entire
window is a continuous unique exchange-session grid with same-day PIT `qfq`,
supported data/execution versions, known suspension/open-execution state and
known corporate-action/adjustment evidence. Do not repair these states by writing
`1` for volume ratio or fixed percentages for target/stop.

`/api/analyze`, `/api/stock/workbench`, and
`/api/stock/upside-probability` are sensitive read surfaces. Verify
`Cache-Control: no-store` on success, validation failures, 4xx/5xx, dependency
failures and malformed response-model paths. Safe 4xx details may remain; any
unexpected/5xx dependency, transport or response-validation detail must be the
generic individual-research unavailable message, never a provider exception,
local path or model payload.

### Individual D+2/D+3/D+4 Probability Research

Inspect the independent, read-only individual probability projection with:

```bash
curl -sS 'http://127.0.0.1:8010/api/stock/upside-probability?symbol=600519' \
  | "$PYTHON" -m json.tool
```

The response must contain exactly the D+2/D+3/D+4 display horizons with one,
two, and three holding sessions. The target observes the completed signal-day
close, uses the fixed D+1 official daily-K `open` as a no-shift price proxy, and
exits at each fixed target `close`. The label asks whether the daily-bar proxy net
return after declared round-trip costs is greater than zero; it does not prove or
guarantee an executable fill. Locked-limit queues, order-book priority, impact
and actual exit tradeability remain limitations, not V1 label claims.
`production_effect` must be `none`. This request is a companion to the ordinary
workbench load; it does not train a model, call a provider, change SQLite,
rebuild advice, rescore a stock, or change a full-market rank.

Treat the statuses as evidence states:

- `not_generated`: no compatible research evidence has been generated; all
  probabilities and intervals must be null.
- `insufficient_data`: evidence exists but an official-date, coverage, split,
  calibration, stability, interval, or selection gate failed; null is the
  required result, not a bearish 0% forecast.
- `calibrated_shadow`: a percentage may appear only when the report and horizon
  both have that status, report-level `evidence.selection_qualified` is true,
  and the point estimate plus interval are present. The artifact verifies each
  horizon's gates independently and maps them into the child status; the public
  horizon object has no separate `selection_qualified` field. The interval, base
  rate, model/feature version, training cutoff, counts, digest and limitations
  must remain visible.

For any future `calibrated_shadow` payload, verify 288 official and official-
replay sessions plus complete report digests. H1/H2/H3 independently require
284/286/288 sessions, at least two folds and 60 OOS sessions per fold. The
selection-gate version, at least two calibration bins, at least 20 sessions in
the sparsest bin, positive Brier skill in every fold, monotonic/highest-bin-lift
gates, and the exact `brier_skill = 1 - brier/reference_brier` identity are
mandatory. Signal and training-cutoff dates must both be bundled exchange
sessions, with the cutoff strictly earlier than the 15:15-mature signal date.
Any missing/mismatched version, digest, count or date keeps the value null.

Never replace a null value with 0%, 50%, the trend score, a scenario weight, a
different horizon, an older stock response, or a full-market 1/5/20-session
estimate. An integrity digest identifies replayed content but is not a signature
or proof of predictive validity.

Build a compact assessment only as an explicit offline research action:

```bash
env PYTHONNOUSERSITE=1 .venv/bin/python tools/evaluate_individual_probability.py \
  --history-manifest '<ATTESTED_HISTORY_MANIFEST>' \
  --database data/ashare_radar.sqlite3 \
  --official-source '<OFFICIAL_PIT_SOURCE_1>' \
  --official-source '<OFFICIAL_PIT_SOURCE_2>' \
  --output-directory data/research/individual_probability
```

The history manifest is deeply verified and must resolve to a sidecar-free,
non-link regular SQLite file no larger than 512 MiB. Adjacent `<db>-wal`,
`<db>-shm`, or `<db>-journal` is rejected. The evaluator
reads the whole file, verifies size, SHA-256 and content identity, then
deserializes the verified byte snapshot into an in-memory SQLite database and
uses one `query_only` transaction; it does not reopen the source path after
verification. Identity and hash must remain stable across the complete read;
after that point model queries consume only the isolated verified bytes. Before
publication the CLI repeats sidecar, identity, size, SHA-256 and complete-byte
verification; a replacement or modification during evaluation fails closed. The
evaluator itself has no source-SQLite write path. Repeat
`--official-source` only for verified canonical official source artifacts. The
command writes one exclusive, content-addressed compact JSON and prints its path,
digest, size and horizon diagnostics; the artifact cap is 2 MiB and the command
does not write the source SQLite. The
history remains `official=false` even when official source dates are counted,
and the artifact contains no current-stock model coefficients or point estimate.
Current builder schema
`individual-upside-probability-assessment-v4-source-intake-bound` exact-matches
the frozen estimator/target/split contract; v3 is superseded. During an explicit
build, v4 loads a current official input only from complete source-artifact-v3 /
snapshot-v3 records with estimator feature-v3, PIT-evidence-v4, current
`full-market-score-v5`, an original `publication` seal and its canonical replay
receipt. Source v2 and score v4 remain historical audit-readable inputs only. It
requires the aware sequence
`quote event <= quote_observed_at <= run.as_of (decision) <= captured_at <=
assessment.generated_at`, validates every bar's date/aware-`as_of` boundary and
nondecreasing observation order, and requires the quote/bars to stay on the
source session. Current source v3 resolves `run.as_of` through the official
temporal contract: a weekend or exchange-holiday decision may bind the latest
completed session even though its natural date is later. A trading-day decision
before 15:15, a stale source after another session has matured, a future/reversed
timestamp, or calendar coverage failure is rejected. `captured_at` is an
availability time and may be later, but cannot precede the decision or exceed
the assessment time. Source v1/v2 and previous-v2 assessment identities retain
their frozen same-date rule. Every row must pass PIT context plus
registered production-score rule/spec replay. The exact ALL/SH/SZ/BJ envelope
must reconstruct from all three markets, satisfy population floors
4,000/1,800/2,500/200, at least 90% eligibility and 95% success coverage per
scope, complete records, and the assessment's 98% success-to-total intake floor.
Cross-date run IDs and source/score contracts cannot conflict. Assessment v1/v3
are rejected. Assessment v2 remains audit-readable under its exact legacy shape,
but its run/date/digest-only identities contribute zero current official
sessions. No compact assessment contains a replayable current-stock predictor;
do not derive a point from `base_rate` or fit one in HTTP. Compact horizon metrics
remain diagnostics and are not independently replayable.

Each current-v4 `official_pit.sources` row persists source identity, time
envelope, run/score versions, counts and full-market coverage. V4 compact
verification rechecks those exact fields, but the compact assessment does not
retain source locators/records for runtime replay. Runtime loading therefore does
not independently locate or authenticate the named source artifact. The public
projection forces `official_pit_session_count=0`, `signal_date=null`,
`selection_qualified=false`, three `insufficient_data` children and null
percentage/interval values; a non-empty current declaration exposes
`official_pit_source_artifacts_not_runtime_replayed`. A resolvable source manifest
and full replay are still required before any future count/signal date can
authorize a percentage. The retained baseline is
legacy v2 and keeps 2026-08-11 run 71 /
`150cc48d…` plus 2026-08-12 run 77 / `c085b6fa…` only for audit. Its complete
integrity digest is
`517691b101dcb2142693a74f6e5ac9ef10f386c545572b6bacfe161f186ba677`
and its canonical JSON is 8,733 bytes. The history's qfq OHLC is an
adjustment-normalized research series rather than historical cash transaction
prices. The 10万元 notional rounded down to 100-share lots is also a deterministic
label-sizing proxy, not evidence of buying power, orders, capacity, or fills.
Do not use `--generated-at` except for an intentional deterministic replay whose
timestamp is recorded in the review evidence.

At runtime, verified files in `data/research/individual_probability/` take
precedence over the source-tracked baseline in `docs/research/artifacts/`. An
empty local directory uses that baseline so a clean checkout exposes the same
reviewed `insufficient_data` evidence. If any matching local candidate exists but
is corrupt, unsafe, or incompatible, the endpoint returns `409`; it must not
fall back to the tracked baseline or an older local artifact. Review and move an
invalid local file out of the candidate directory only after preserving it for
diagnosis. Do not edit a content-addressed artifact in place. The store binds
device/inode/mode/size/mtime/ctime and content SHA-256, requires a stable directory
snapshot, rejects two different newest artifacts with the same `generated_at`,
and returns a deep copy. A caller mutation or same-size/timestamp content swap
must therefore be detected or isolated rather than poisoning the cached latest
assessment.

The route validates and normalizes the six-digit stock symbol before reading the
artifact store. An invalid symbol is therefore a `400` even when a corrupt local
candidate also exists; a valid symbol with corrupt evidence is `409`. This keeps
the input boundary deterministic without weakening evidence fail closure.

As of this worktree, the tracked projection contains zero declared
current-contract signal dates, and there is no independently source-replayed
current PIT corpus. The registered H1/H2/H3 two-fold floors are
284/286/288; the longest D+4 horizon uses entry offset 1 and target/purge gap
`H+1=4`, so the public required count is 288. The tracked legacy v2 projection is
`insufficient_data` with `official_pit_session_count=0`, `signal_date=null`, and
null D+2/D+3/D+4 probabilities/intervals. It discloses
`legacy_official_pit_sources_audit_only_not_current_evidence`; run 71/run 77 are
not silently upgraded. The
separately attested historical replay is `official=false` and failed
its actual selection gates. Do not copy its estimates into the official report,
lower the date/fold/bin/skill gates, duplicate cross-sectional rows as independent
dates, or claim that pipeline execution is model validation.

There is no request-time fit or automatic promotion command. Before treating any
future count as accumulated official PIT, add a compact-to-source locator/manifest
contract and independently replay every named source. Then evaluate each horizon
separately through the reviewed offline artifact/replay workflow, retain complete
grouped-date train/gap/calibration/gap/test evidence, and publish a new compatible
report only after Ruff, mypy, Python/JavaScript contract tests and browser
regression pass.
The [dated research plan](research/INDIVIDUAL_SHORT_HORIZON_PROBABILITY_2026.md)
defines the adopted papers, target, model candidates and promotion boundary.

### Full-Market Scan

The top-level **全市场选股** workspace is the normal manual entrypoint. It creates a background run and returns immediately; keep the page open to see progress, or inspect the same state from a shell:

```bash
curl -sS -X POST http://127.0.0.1:8010/api/market-scans \
  -H 'Content-Type: application/json' -d '{"mode":"official"}'
curl -sS -X POST http://127.0.0.1:8010/api/market-scans \
  -H 'Content-Type: application/json' -d '{"mode":"preopen"}'
curl -sS 'http://127.0.0.1:8010/api/market-scans/latest?mode=official'
curl -sS 'http://127.0.0.1:8010/api/market-scans/latest-published?mode=intraday'
curl -sS 'http://127.0.0.1:8010/api/market-scans/latest-published?mode=preopen'
curl -sS 'http://127.0.0.1:8010/api/market-scans?page=1&page_size=100&mode=official&status=published&data_date=2026-07-29'
curl -sS 'http://127.0.0.1:8010/api/market-scans/1/results?page=1&page_size=100&status=success&market=SH&market=BJ&industry=%E9%93%B6%E8%A1%8C&min_score=80&max_score=100&sort=score&sort=trend_score&order=desc&order=desc'
curl -fL -o market-scan.xlsx 'http://127.0.0.1:8010/api/market-scans/1/export.xlsx?status=success&market=SH&market=BJ&min_score=80&sort=score&order=desc'
curl -sS -X POST http://127.0.0.1:8010/api/market-scans/1/cancel
curl -sS -X POST http://127.0.0.1:8010/api/market-scans/1/retry
curl -sS -X POST http://127.0.0.1:8010/api/market-scans/1/refresh-top100
```

The TOP100 refresh endpoint accepts only a shared-action-gate-eligible, current-date run whose scoring fingerprint still matches the active configuration: the digest-valid original publication, exact full-market scope and internally consistent diagnostics-v1 score gate are all required. It creates a linked derived snapshot and never overwrites the source leaderboard. A `legacy_backfill`, wrong-scope publication or contradictory/missing score diagnostic cannot authorize refresh; use a new full-market scan when the source is unsuitable.

The exact full-market scope string is `沪市 + 深市 + 北交所当前上市A股`. Do not substitute a display label or a semantically similar string in lifecycle, Discovery, probability, delta, alert, strategy or automation queries; scope equality is part of the frozen cohort identity.

Every successful/degraded publication carries `snapshot_digest`, `snapshot_seal_origin`, and `snapshot_sealed_at`. `market-scan-snapshot-digest-v2` covers every persisted public run field except its own digest and every persisted public result field, including public `metrics` and `score_details`, under strict finite canonical JSON and symbol ordering. Verification now streams the ordered SQLite result cursor into the same SHA-256 byte contract one canonical row at a time; it must not fetch or retain the complete result set. Peak additional verifier memory therefore follows the largest canonical row, not the number of stocks. Publication verifies `finished_at <= updated_at <= snapshot_sealed_at` and every child `updated_at <= run.updated_at`. Five SQLite triggers then reject sealed-run update/delete and sealed-child update/delete/insert. Never troubleshoot by dropping these triggers, editing result JSON, changing the digest/origin, or resealing a published run.

The streaming change requires no schema migration, new migration marker, digest-version bump, stored-digest update or artifact rewrite. Existing v2 seals must verify byte-for-byte as they are; a mismatch remains corruption, not a reason to migrate or reseal. The frozen production probe observed peak RSS fall from approximately 1.067 GiB to 22 MiB and wall time from approximately 3.65 seconds to 2.63 seconds for the same digest. Treat those figures as a one-machine diagnostic baseline rather than an SLA. If memory regresses, confirm the result query is still cursor-iterated in `symbol ASC` order and no wrapper calls `fetchall()` before investigating SQLite/provider state.

`publication` means the seal was committed with the original publication. `legacy_backfill` means only that the retained graph verified when the upgrade ran. History/detail/latest/results, publication summaries, breadth/screen evaluation, offline evaluation and backup verification may audit either origin and expose it. Discovery, delta, screen-alert writes, strategy execution/simulation/executable Shadow/evidence/automation, probability capture/filter/managed publication, future-range research, degraded retry-copy and TOP100 refresh require the complete shared action-source gate—not origin alone—and two-run operations require both inputs to pass it. Run `#80` is the concrete 2026-08-13 intraday degraded, `data_date=2026-08-12`, `legacy_backfill` example: it completed under the pre-seal backend and remains useful for audit, but it cannot authorize any of those operations.

**盘前复盘** is the independent `preopen` mode. The server admits it only on a Shanghai trading day in `[00:00, 09:15)`, so an 08:00 start uses the previous completed trading session. Both `quote_date` and `data_date` equal that prior session, and each completed-session quote close must agree with the same session's daily-K close. Weekend/holiday requests and requests at or after 09:15 are rejected; a run still active when call auction begins is stopped before publication. The browser's weekday time-based default is only a convenience—the server-side trading calendar remains authoritative. The 09:15-09:30 interval admits neither `preopen` nor `intraday`.

`preopen` has its own persisted mode, rule fingerprint, latest/history selection, rank-comparison boundary, and retry cohort. A retry inherits its source mode, dates, and fingerprint rather than becoming `official`. It is not captured as an official upside-probability source and the official-only future-range endpoint returns `422` for it. Automatic scheduling remains after-close and creates/retries `official` runs only.

### Trustworthy Screening Workbench

Select a published full-market run, then expand **可信筛选工作台**. Expansion is the request boundary: the closed panel makes no screening request. The browser concurrently reads breadth, evaluates the current filters and requests a same-cohort delta; each result has independent loading/error state and `Cache-Control: no-store`. The server does not fully deserialize the roughly 5,500-row result population: breadth reads five required columns, evaluation reads executable scalars, and only the selected page plus near misses are hydrated into full rows, with a hard 300-unique-symbol ceiling. Use **刷新工作台** after changing filters while the panel is open. The five **榜单列视图** controls only change which existing cells are emphasized; they do not request, filter or rerank data.

The equivalent read-only API calls are:

```bash
curl -sS 'http://127.0.0.1:8010/api/market-scans/71/breadth'
curl -sS 'http://127.0.0.1:8010/api/market-scans/71/delta'
curl -sS -X POST 'http://127.0.0.1:8010/api/market-scans/71/screen/evaluate' \
  -H 'Content-Type: application/json' \
  -d '{
    "spec": {
      "schema_version": "screen-spec-v2",
      "status": "success",
      "markets": ["SH", "SZ", "BJ"],
      "industries": [],
      "is_st": false,
      "is_new": null,
      "ranges": {
        "score": {"min": 80.0, "max": null},
        "amount": {"min": 100000000.0, "max": null},
        "data_quality_score": {"min": 90.0, "max": null}
      },
      "keyword": null,
      "sort": [{"field": "rank", "order": "asc"}]
    },
    "page": 1,
    "page_size": 100,
    "near_miss_limit": 20,
    "near_miss_max_failures": 1
  }'
```

Breadth and evaluation accept only a digest-valid final `success` or `degraded` run whose scope is the exact complete full market; they return `422` for an active, failed or TOP100-derived run. They may audit either disclosed seal origin and any `official`, `intraday`, or `preopen` cohort because the response binds origin, mode and rule version rather than collapsing them. Breadth must conserve population/status/market/score/change/industry counts. Evaluation verifies the canonical spec digest, funnel/page/match/near-miss counts, passed/missing condition codes and disjoint exclusion reasons before returning its canonical semantic digest. Delta is stricter: current and immediate previous `(mode, scope, rule_version)` cohorts are read in one transaction, both must pass the shared original-publication/exact-scope/diagnostics action gate, and decision/date/cohort/Top-N/exposure/reason/count identities must replay. It returns typed `status=unavailable` when those gates or a compatible predecessor are absent. Do not treat that state as a zero-change comparison.

`ScreenSpecV2` is strict and has no compatibility bag for unknown conditions. Status, markets, industries, ST/new flags, score/trend/change/turnover/amount/quality, frozen confidence/risk/tradability dimensions, keyword and one to three unique sorts are executable. A null source value stays missing; a condition that requires it emits a missing reason and excludes the row rather than substituting zero. SQL values are parameterized, and `%`/`_` in a keyword are literal characters rather than wildcard injection. The response `spec_digest` identifies the normalized condition contract, while `canonical_digest` binds the complete returned semantic evidence. These SHA-256 values detect deterministic drift but are not signatures.

An upside-probability threshold is not part of `ScreenSpecV2`. If one is active, the browser still permits the independent breadth/delta reads but refuses the funnel with a message asking the user to clear the probability condition. Do not add the key manually or assume it was ignored. The ordinary probability filter requires the stronger `probability_filter_qualified` authorization; retained runs 71 and 77 are source-v1 `legacy_unbound`, so neither can supply it.

Excel export uses the same normalized row filter/order and writes `筛选合同=screen-spec-v2`, the full canonical JSON, and its digest into `导出信息`. The export still labels any legacy probability request separately because it is outside the v2 contract. Comparing that JSON/digest with the evaluation response is the quickest way to investigate filter drift; neither path requests a provider or recomputes the selected run's v4/v5 score contract.

Saved discovery presets created or fully updated by this release use schema version 2 and persist keyword, confidence/risk/tradability ranges plus `column_view`. Startup migrates a v1 table to add `column_view=overview` while keeping existing row revisions/schema versions and research-queue provenance. A v1 archive remains importable only under its historical checksum surface; adding v2-only fields to that archive is rejected. Create and verify a runtime backup before any production schema upgrade as described under **Runtime Backup and Restore**.

After selecting a saved plan, **更多管理 → 记录变化提醒** calls the following explicit write endpoint:

```bash
curl -sS -X POST \
  'http://127.0.0.1:8010/api/discovery/presets/3/screen-alerts' \
  -H 'Content-Type: application/json' \
  -d '{"current_run_id":71,"expected_preset_revision":2}'
```

The service rechecks the plan revision and the complete shared action-source gate for both snapshots, compiles the same `ScreenSpecV2`, and compares the current and prior compatible run. `entered_symbols` and genuine `exited_symbols` are separate from `suppressed_unrankable_symbols`; a symbol still present as `pending`, `missing` or `skipped` is not reported as a confirmed exit. `status=unavailable` creates no event. A ready semantic event is content-addressed and unique for the plan revision/current/prior run/digest: the first response has `created=true`; an identical retry returns the same `event_digest` with `created=false`. Under the shared domain-input error contract, a revision conflict returns `400` rather than recording against changed conditions.

The retained real-data performance check used a verified static runtime-backup bundle, not the live database. Its `runtime.sqlite3` SHA-256 was `3a77a2ad0885aae10f37dfd26c239b64047d7ecf7de52c6380052e096d23015b`. Published `official` full-market run `#71` (`data_date=2026-08-11`, `degraded`) contained 5,542 result rows and 5,499 successes. With the example spec above, the optimized and legacy full-hydration paths both produced evaluation digest `30868975646c26e368cf1043f72a420c3409f1f4c1296cdf8fd03ba3be705cd8`; the breadth digest remained `6eb5a0d8d9587b59fd2af88db99cb98c2cd262734cfa6759a541c40a4b13cd5f`. Both reported population 5,542, 691 matches, a 100-row page, and 20 near misses. Running optimized breadth then evaluation sequentially took 0.2378 seconds with 70.23 MiB peak RSS, 37.41 MiB above that process's baseline. Running the same two reads concurrently took 0.2526 seconds with 66.27 MiB peak RSS, 32.80 MiB above baseline. The previous concurrent full-population hydration/JSON-deserialization implementation measured about 3.17 seconds and 1,110.5 MiB peak RSS, so the projection boundary addresses both latency and memory without changing output semantics. The backup database SHA was unchanged before and after every mode. These are separate-process, one-machine observations, not latency or memory SLAs and not a reason to issue workbench reads eagerly.

### Strategy laboratory operations

Use the browser editor for the ordinary flow. The API is also inspectable without a provider call:

```bash
curl -sS http://127.0.0.1:8010/api/strategy-lab/templates \
  | "$PYTHON" -m json.tool
curl -sS -X POST http://127.0.0.1:8010/api/strategy-lab/parse \
  -H 'Content-Type: application/json' \
  -d '{"text":"排除 ST 和上市不足 120 天，选择沪深 A 股中近 20 日趋势较强、成交额超过 1 亿、风险较低的股票，行业最多 3 只，持有 5 天。"}'
curl -sS http://127.0.0.1:8010/api/strategy-lab/metrics
curl -sS 'http://127.0.0.1:8010/api/strategy-lab/strategies?page=1&page_size=20'
curl -sS 'http://127.0.0.1:8010/api/strategy-lab/executions/1/candidates?page=1&page_size=50&sort_by=utility_score&descending=true'
curl -sS 'http://127.0.0.1:8010/api/strategy-lab/executions/compare?left_execution_id=1&right_execution_id=2'
curl -sS 'http://127.0.0.1:8010/api/strategy-lab/executions/1/simulation-plan'
```

The template read must return HTTP 200 with `Cache-Control: no-store`, schema
`full-market-strategy-template-catalog-v1`, `selection_mode=exclusive`,
`production_rule_version=full-market-score-v4`, `production_effect=none`,
`official_session_count=2`, 14 deterministically sorted version-1 templates and
catalog digest
`0038e66d4ce6c13bafb51e3fddf990c11f5f8f38c001343f5f4523ff255a9d1f`.
That v4/two-session identity is the frozen historical catalog baseline, not the
current v5 production rule and not current-v5 promotion evidence.
Treat a different digest as a semantic catalog change that requires code, tests
and documentation to move together; the digest is an integrity identity, not a
publisher signature. The Pydantic models recompute and verify the template and
catalog digests and require globally unique template IDs; a payload with a
well-formed but stale digest must fail. This endpoint reads neither providers nor
SQLite and does not create a saved strategy or execution.

Verify the state partition rather than collapsing it into a generic “ready”
label:

- six `available_for_draft` entries have `contract_status=verified`,
  `efficacy_status=not_generated`, an embedded custom `strategy_spec`, no
  `missing_fields`, and a 61-session formation horizon. Its 16–18
  `required_fields` must equal the complete dry-run compiler execution plan,
  not just the authored hard filters: expect default board/listing-age/
  suspension/ST/status/industry/price/amount/data-quality inputs, the 61-bar PIT
  contract and score dimensions, plus any required 1/20/60-day return inputs;
- every available spec has `profile=custom` and explicit objective weights. Do
  not switch it to a named profile, which would allow profile normalization to
  overwrite the catalog's fixed weights;
- `balanced_multi_horizon`, `capacity_first` and `pullback_continuation` use
  5/5 holding/rebalance sessions, `daily_continuation` uses 1/1, and
  `bounded_medium_trend` plus `defensive_liquidity` use 10/10. The last two
  values are intentionally equal because the current editor has no independent
  rebalance control; changing one on load would otherwise create semantic drift;
- three `shadow_only` entries—`industry_relative_strength`, `medium_momentum`
  and `short_reversal`—all use a 61-session formation contract and have
  `verified/insufficient_data` plus `strategy_spec=null`;
- five `unavailable` entries—`crowding_risk`, `dividend_low_vol`,
  `event_revision`, `quality_growth` and `value_garp`—have unavailable
  contract/efficacy, `strategy_spec=null` and explicit `missing_fields`.
  `crowding_risk` may list present amount/tradability inputs, but remains blocked
  until its PIT holdings, flow, crowding and capacity fields exist.

Every template limitation must continue to state that suspension and one-price
states use frozen daily-K amount plus persisted reason-text proxies. Those fields
do not prove exchange event-time status, order-book priority or a fill at a limit
price. If that warning disappears, treat the catalog as semantically incomplete
even when the response shape still validates.

The catalog is fixed to full-market research and accepts no symbol, custom
universe or scope parameter. In the browser it is activated from the
full-market scan workspace, remains mutually exclusive, and can load only an
`available_for_draft` entry. Loading must first pass
`POST /api/strategy-lab/compile` as a dry run and must leave the strategy unsaved, start
no scan, write no evidence and leave the production leaderboard unchanged.
Shadow and unavailable entries are diagnostic cards, not buttons to bypass their
gates. A malformed or partially incompatible catalog must fail closed instead of
displaying whichever cards happened to parse.

`official_session_count=2` means only official PIT run 71 on 2026-08-11 and run
77 on 2026-08-12. It is not a live count and does not authorize strategy
efficacy, calibration, regime selection, FDR, PBO/DSR, automatic promotion or
production use. When investigating the research rationale or planning more data,
use the dated
[full-market strategy-archetype report](research/FULL_MARKET_STRATEGY_ARCHETYPES_2026.md),
not the availability label alone.

The executable-candidate Shadow is a separate, explicitly requested read:

```bash
curl -sS 'http://127.0.0.1:8010/api/strategy-lab/executable-candidate-shadow?run_id=77&notional_cash_cny=1000000'
```

Use only a current shared-action-gate-eligible `official` exact-full-market run ID; a valid publication origin without the required diagnostics-v1 score evidence is still rejected. Opening the market
workspace, expanding Strategy Lab, or filling the current-run field must issue no
request; submit the form to start one call, and cancel or leave the page to abort
it. The response is `no-store`, `research_shadow/not_generated`, performs no
database write and preserves the selected run's production ranks (current v5 or
historical read-only v4). Review filter/risk,
conservative cost, frozen-session-amount capacity, exposure, turnover, original
production rank and Shadow order together. `adv_evidence_status=unavailable` is
expected: current capacity is not historical ADV, daily suspension/one-price
flags are proxies, costs omit live spread/impact/depth, industry taxonomy is
mixed, and the candidate order is not verified Alpha. One on-demand run-77
observation took 9.2 seconds on a single machine; treat it as a reason to preserve
the lazy interaction, not as an SLO. A stale or aborted response must not repaint
the panel, and a panel error must not remove the leaderboard or Strategy Lab.

Saving or updating requires the complete structured `spec`, `confirmed: true`, and the expected revision for an update. Execution requires a saved `strategy_id`; `latest_scan` resolves one shared-action-gate-eligible frozen run, while `historical_replay` requires an exact persisted action-eligible historical date/run and never contacts a provider. Before `latest_scan` writes, `strategy-execution-freshness-v2` resolves the current quote session for intraday or the latest completed exchange session otherwise, computes age in trusted exchange sessions and rejects calendar-unavailable, future-dated or over-limit data. Historical replay retains its original decision-time semantics instead of being compared with today's clock, but it does not bypass shared action authority. A new row freezes the source snapshot digest/origin with `source_snapshot_verification_status=verified` and rechecks the same source inside the write transaction. `verified` is a binding state, not proof of action authority; evidence and automation also recheck the original publication, exact full scope and diagnostics-v1 score gate. A migrated row with null source digest/origin and `legacy_unverified` is forensic-only and must fail research/action reads rather than be repaired in place. Equal, `risk_adjusted`, and `custom` weighting are executable contracts; custom weights must use canonical `000000.SH|SZ|BJ` symbols, stay under the stock/count caps, sum to at most one, and never acquire an unrequested replacement. Equal/risk-adjusted drafts iteratively try the deterministic remaining pool after a constrained candidate is removed and recompute allocation; inspect `replacement_attempt_count`, `pool_exhausted`, and `underinvested_reason` rather than assuming requested count means filled count. Buy and hold utility thresholds are separate deterministic admission gates. A valid response can be `no_trade`; do not weaken evidence, source provenance, price-limit, suspension, liquidity, or portfolio guards just to fill the requested stock count. Candidate pages are capped at 200 and the browser uses 50.

Evidence refresh is intentionally cheap and synchronous:

```bash
curl -sS -X POST http://127.0.0.1:8010/api/strategy-lab/strategies/1/evidence/refresh \
  -H 'Content-Type: application/json' -d '{"revision":1,"mode":"official"}'
```

It first requires a verified execution binding and rechecks that the current source still passes the complete shared action-source gate; `legacy_unverified`, `legacy_backfill`, wrong-scope or score-diagnostic-ineligible sources cannot create or refresh evidence. It then reads `docs/research/FULL_MARKET_SELECTION_SHADOW_V55_2026.json`, verifies the pinned compact-v2 exact schema and digest, reconstructs the latest matching execution result digest from its summary/candidates, and recomputes promotion from candidate gates before combining the typed research boundary, coverage, Top20/50/100, rank-delta, constraint/exposure/robustness and promotion evidence. Persisted evidence uses strict finite JSON and exact database/payload identity binding; corruption in the newest matching row returns the generic artifact integrity failure and never falls back to an older row. An old or incompatible artifact is returned as unavailable; it is never interpreted as an empty successful evaluation or proof of custom-strategy efficacy. The HTTP request does not launch the cross-date evaluator. To deliberately recompute the baseline, first create and verify a static `runtime_data.py` backup, then run the following offline and review the generated diff before replacing the retained report:

```bash
PYTHONNOUSERSITE=1 .venv/bin/python tools/evaluate_market_scan_shadow.py \
  --database '<VERIFIED_STATIC_BACKUP>/runtime.sqlite3' \
  --run-id 71 \
  --mode official \
  --variant v5_4_skip5_multilevel_residual \
  --variant v5_4_skip5_multilevel_residual_volume_lifecycle \
  --variant v5_5_bounded_nonlinear_stability \
  --bootstrap-samples 1000 \
  --compact \
  --output docs/research/FULL_MARKET_SELECTION_SHADOW_V55_2026.json
```

Do not point this long-running comparison at the live SQLite file. `--compact` changes only the retained projection: it preserves aggregate cohorts, Top-N, rank differences, constraints, regime/cost/capacity robustness, exposure and promotion gates while omitting giant per-stock records and the source machine's database path. It does not reduce the 1,000-sample inference computation. The verified 2026-08-12 run used backup SHA-256 `d4d005b9515e05abc54688642cd241b1066df054a9d55ac22af0e081aa3db546`, size `2,129,387,520` bytes, and passed backup integrity before and after the query; it finished in about 2m50s. The retained report was generated at `2026-08-12T09:07:04Z`, is 231,983 bytes, and has SHA-256 `29884e99744a3001b156c338dff77d9e4f00d0bf455f7d1c1b2d7c8f2b0859ad`.

The retained v5.5 comparison is a dated historical `full-market-score-v4`
baseline. It does not authorize a current v5 execution; that remains
contract-incompatible until a separately reviewed v5 evaluation artifact exists.
Run #71 contains one independent signal date and 5,499 replay-verified v5.5
candidate scores at 100% item coverage. It has no complete five-day paired
sessions, so net excess, IC, p-values and BH-FDR decisions remain null; PBO and
DSR are `not_computed`, and every decision remains `remain-shadow`.

Keep production services single-worker while the existing runtime leader owns schedules. A strategy schedule is pinned to the revision present at creation, can be disabled with `PATCH /api/strategy-lab/schedules/{id}` and `{"enabled":false}`, and processes each exact action-eligible full-market scan at most once. Latest lookup skips a newer digest-invalid, `legacy_backfill`, wrong-scope or score-diagnostic-ineligible published row instead of using it for action; every draft/action boundary rechecks the shared gate, not just origin `publication`. Failed schedule attempts are isolated and persisted in schedule-run state; alerts retain all strategy/data/rule/cost fingerprints. Simulation plans are sealed against their source execution result digest, strategy/run identities and cost rule; tampering or resealing a plan must produce an integrity `409`, not a newly trusted plan. Archiving prevents new latest executions and automatically disables a still-enabled schedule before another claim; historical audit reads remain available only when their source-binding state verifies. The manual `/api/strategy-lab/automation/evaluate` endpoint is an operational diagnostic, not an order trigger.

Local user-data JSON export includes strategy roots and immutable versions. It intentionally excludes execution candidates, evidence snapshots, schedules, and alert runtime provenance because those records depend on frozen scan rows; use the verified full runtime backup/restore workflow for that complete lineage. A replace import that would violate an existing runtime reference fails and rolls back atomically.

Replace `1` with the returned run ID. `queued`, `running`, and `cancelling` are active states. `success` means every seeded stock produced a clean ranked row from a current stock pool. `degraded` also covers a locally cached `stale-fallback` stock pool even when every per-stock score succeeds; run diagnostics and `stock_pool_source` retain that provenance. Per-result decisions use structured `quote_fallback_used`, `kline_fallback_used`, `metadata_degraded`, and `degradation_reasons` fields; Chinese display tags are derived and do not control retry or terminal status. `failed` means no usable ranking or a run-level prerequisite failed; `cancelled` and `interrupted` can be retried. Repeated starts return the existing active run rather than creating overlapping work. Runtime-leader startup or takeover changes orphaned active rows to `interrupted` before mutation.

Omitting `mode` on `latest`, `latest-published`, and the run list preserves the original global behavior. The browser always shows two identities: the globally active/background task and the selected leaderboard mode. If an `official`, `intraday`, or `preopen` request reuses a task active in another mode, the task mode is displayed explicitly and the selected-mode published leaderboard stays intact. Selecting a history run binds filters, paging, export, snapshot evidence, and discovery/research provenance to that run until **最近发布** is selected again.

Result and export queries accept repeated `market` values, repeated substring-matched `industry` values, `min_`/`max_` pairs for `score`, `trend_score`, `change_pct`, `turnover_rate`, `amount`, and `data_quality_score`, plus up to three aligned repeated `sort`/`order` values. Duplicate sort fields, unmatched order counts, invalid ranges, more than three sorts/markets, or more than 20 industries return `422`. SQL remains parameterized and result pages remain capped.

Excel export accepts the exact same normalized filter and multi-sort parameters as the browser leaderboard, without pagination. Only `success` or `degraded` runs are exportable. The download contains `榜单`, `评分明细`, and `导出信息` sheets; it reads the persisted snapshot and does not contact providers or recompute ranks. Stock codes stay text and provider/user-derived cells are protected against formula injection. The API returns `Cache-Control: no-store`, an attachment filename, and the standard XLSX media type.

The frozen shared action-source gate requires a digest-valid original
`publication`, exact full-market scope, strict diagnostics, exact immutable rule
contract and repository-generated canonical replay receipt. Status, digest,
origin, scope or a green label alone is insufficient. API reads enforce this in
one request-local `market_scan_verified_read` query-only transaction: the whole
graph is seal-hashed once, canonical score observations are fused into one replay
receipt calculation, outbox/source identities and the result page are read from
that same snapshot, and no second SQL score read occurs. The capability cannot be
reused after close, across threads or transaction restart; DML, DDL, schema or
`query_only` mutation fails closed. Discovery, strategy, probability capture,
future range, TOP100 and retry paths cannot weaken that authority. Read-only
audit views remain available for action-ineligible evidence and must not imply
authority. Snapshot failure returns the generic market-snapshot `409`; research
artifact failure returns the generic artifact `409`. Every related response is
`Cache-Control: no-store`, which is cache policy rather than integrity proof.

Every run exposes `current_stage`, `stage_metrics`, `market_progress`, `elapsed_seconds`, effective `throughput_per_second`, and optional `eta_seconds`. The six stages are stock pool, bulk quotes, K-line acquisition, scoring, persistence, and publication. In v6, bulk quotes means the complete all-chunk capture phase: K-line acquisition cannot begin until its exact-count envelope is sealed. Wall duration and accumulated work duration can differ because concurrent K-line and scoring work overlaps after that boundary. ETA is intentionally `null` until at least 20 rows and five seconds are observed; clients must display `估算中`, not invent precision. SH/SZ/BJ progress reports total/processed/success/missing/skipped and coverage separately. The terminal UI separates **发布阻断**, **已通过门禁**, and **数据源告警**; operators should restore the source or start a valid retry instead of lowering publication guards.

For an idle open browser, `GET /api/market-scans/polling-identity?mode=official|intraday|preopen` is the expected high-frequency request. It returns only opaque change-detection tokens and `Cache-Control: no-store`; an unchanged response must not be followed by `latest`, `latest-published`, results, probability, export, or action requests. Initial load, online/manual force, or a changed token intentionally performs the existing fully verified global `latest` and mode-scoped `latest-published` reads before loading results. During an active scan, ordinary progress continues through `GET /api/market-scans/{run_id}` every two seconds; result batches may update the run timestamp without changing the lightweight token. If browser polling produces sustained full-snapshot hashing, inspect the client request sequence rather than weakening snapshot verification: no lightweight response is valid authority for a leaderboard or downstream action.

If a deterministic contract/integrity/HTTP failure or a dispatched timeout occurs for one stable fingerprint, subsequent ordinary ticks must remain identity-only. A token change or explicit refresh permits one new trusted synchronization. The exact message-bearing admission `503` is the only automatic busy retry and carries `Retry-After: 2`; it must not be rewritten by the sensitive-error handler or remembered as permanent fingerprint failure. `latest`, `latest-published`, run detail, and results share one process-local capacity-one verifier admission. Busy calls dispatch nothing and return no-store `503`; there is no queue, single-flight result reuse, or cross-request authorization cache. After caller cancellation, expect at most the already accepted verifier to finish; its lease remains held and every competing heavy dashboard read stays busy until that worker really exits. Application shutdown first stops admission, drains every owned worker and consumes late exceptions, then closes the runtime, workbench cache, and DataHub. The deployed single-worker Uvicorn process makes this a service-wide dashboard bound; a future multi-worker deployment would require an explicit cross-process admission design rather than assuming this lock is global.

Per-market `coverage_pct` is derived as `success_count / (total_count - skipped_count)` and is zero when the denominator is zero. Eligibility is reported separately. A frozen pre-canonical-receipt run such as `#85` may be normalized at the read boundary to this projection; current runs and arbitrary persisted percentage/count disagreements fail closed. Never edit the legacy run to make the UI accept it.

The lightweight endpoint detects selected run-header/seal and database file/schema changes, but not a child-result-only mutation. That narrower boundary is intentional. Results, probability, export, and action endpoints always reverify the full graph, and the scheduler independently performs a full audit within five minutes, so tamper still fails closed. Never use polling tokens as a health certificate, publication receipt, incident-repair mechanism, or reason to bypass those reads. A tab leaving the scan surface aborts stale rendering work but keeps one bounded background identity timer while the controller remains active and visible; hiding/deactivating the page cancels it without creating detached request queues.

Run the provider-free, repeatable cache-path benchmark against the configured database:

```bash
python tools/benchmark_market_scan.py \
  --database data/ashare_radar.sqlite3 \
  --iterations 3 \
  --output docs/research/FULL_MARKET_SELECTION_PERFORMANCE_2026.json
```

The source database is checked for size/mtime mutation. The cold case uses an empty temporary cache of the same symbol cardinality; the warm case reads persisted K-lines. It compares the legacy connection-per-symbol path with production batch prefetch and reports historical complete-run median separately. The recorded 5,532-symbol evidence improved the cold local cache-read median from 3.043701 s to 0.078691 s (97.41%) and the warm median from 13.828884 s to 11.129911 s (19.52%). Ten historical complete scans had a 599.633 s median, so the safe local change contributes an estimated 0.45% end-to-end ceiling; provider refresh remains the measured limiting factor. No provider concurrency, date/coverage gate, incomplete-bar rule, or 30-minute deadline was changed.

The frozen release audit separately exercised the current request-local action
read over a synthetic/exclusive 5,382-row publication three times. Wall/CPU
seconds were 3.585/3.558, 4.509/4.476 and 3.942/3.915; each run performed one
full snapshot hash and one canonical replay and passed the test bounds of CPU
under 5 seconds and wall under 12 seconds. These are regression observations,
not an SLA and not evidence that frozen run `#82` was rewritten or repaired.

Production has two ordered phases. First, every quote chunk is fetched, frozen with its provider-response observation time, and sealed into one capture envelope; no K-line work overlaps this phase. Second, only the next batch's SQLite preservation-cache read may overlap the current batch's provider K-line work. K-line look-ahead depth is one, scoring and writes keep batch ownership, and the speculative task is drained on cancellation or failure. Tencent quote and daily-K calls also share one provider-scoped HTTP client and keep-alive pool until orderly DataHub shutdown. This removes repeated client/TLS setup but does not reduce the required provider responses or raise the provider admission limit. A 5,540-symbol read-only A/B rejected a proposed window-function cache query because it was 0.44%–1.99% slower and required temporary sorts; the indexed single-connection batch query remains the production path.

Produce a read-only effectiveness research report with:

```bash
python tools/evaluate_market_scan.py \
  --database data/ashare_radar.sqlite3 \
  --output market-scan-evaluation.json
```

Optional repeated `--run-id`, `--mode`, `--minimum-sample`, and `--complete-coverage` flags narrow the study. The evaluator opens SQLite in URI read-only/query-only mode, starts from frozen published ranks, and uses only later complete persisted trading days. Every Top-N/horizon/stratum cohort is either `ok` or `insufficient_data`; missing later days are not backfilled from current providers. This report is research evidence only. Do not change production weights without a reviewed new `rule_version`.

Generate the separate 1/5/20-session上涨概率 Shadow artifact with:

```bash
python tools/evaluate_market_scan_probability.py \
  --database data/ashare_radar.sqlite3 \
  --output-dir data/market-scan-probability \
  --report data/market-scan-probability-summary.json
```

The command opens SQLite through the same `mode=ro` plus `PRAGMA query_only=ON` evaluation path and has no database write path; only the ignored JSON artifact directory and optional machine-report path are written. Stop other database-writing services or evaluate a copied database when independently checking byte-for-byte invariance, because a concurrently running service may legitimately change the live file. It publishes one immutable content-addressed artifact per run so the API can validate and load only the requested history batch, while collectively retaining every run/symbol/target/horizon result. This includes `probability=null` and its limitations when the independent-date, split, 95% label-coverage, point-in-time evidence, per-bin, calibration, Top100 cost-aware outcome, temporal/major-strata or deterministic-replay gates fail.

Probability research is never pooled across `(mode, scope, rule_version)`. For a given cohort and quote date, the read-only query orders published runs by `as_of` then `id` and keeps the final run; a conflicting second run that reaches the research builder is a contract violation and stops evaluation. Each cohort independently owns its equal-weight benchmark, labels, train/calibration/test folds, model, metrics and review gates. When the report contains several cohorts, use `cohorts[].cohort_contract` and its horizon evidence. The top-level horizon summary has `probability=null` and is only an index: do not infer a pooled base rate, benchmark, model, calibrated percentage or promotion decision from it.

The current identity is core schema v4, model v2, feature v3, label v3, split v3, result `market-scan-probability-result-v4-explicit-intervals`, source artifact-v3/snapshot-v3, and coverage `market-scan-probability-source-full-market-coverage-v2`. Readers preserve source v1/v2/v3 and score v4/v5; current source writes only v5. H1/H5/H20 mean D+1-open entry and D+2/D+6/D+21 close exit. Because the target consumes `H+1` sessions from signal D, purge gaps and circular bootstrap blocks remain `H+1`; formal fit floors remain 224/232/262 dates. Result v2/v3 and all other superseded contracts stay replay/read-only. Feature v3 uses the `medium` liquidity bucket; v5 continuous trend maps to stable `rank_refinement` with `final_rank_discount=0`. Current API/UI interval semantics are unchanged and none is an individual-stock outcome interval.

Artifacts use canonical finite JSON, exact versions and a SHA-256 integrity seal, not a signature. `GET /api/market-scans/{run_id}/probability-research` and result/export paths never fit or write SQLite. A threshold requires an opaque token returned by strict verification of `market-scan-probability-filter-authorization-artifact-v1` with payload version `market-scan-probability-filter-authorization-v3-raw-drift-joint-execution`; passing an ordinary JSON mapping is never sufficient. Verification binds exact evidence/metrics/input, horizon/target, official run/cohort/scan-rule and the fully covered unique production score pair, then recomputes raw OOS predictions, proper-score intervals/ECE, candidate-family BH-FDR, temporal drift and raw session economics. `generated_at` must be aware, content-bound, non-future and mature; an ambiguous set of unsealed legacy latest artifacts is rejected. Missing/mixed contracts or any legacy source are `legacy_unbound` and return `422` for filtering. UI and Excel label H1/H5/H20 as holding 1/5/20 days at D+2/D+6/D+21; when probability is null, probability-only export cells remain blank rather than zero.

The separate `market-scan-probability-deployment-refit-v1` implementation refits outside the final OOS evaluation fold with its own train/calibration purge, replay and freshness gates; new predictions require an opaque verified `market-scan-probability-deployment-estimator-artifact-v1`. Do not use the final OOS fold as a live predictor. No verified deployment artifact currently exists. Current v4 labels also condition on already executable rows and do not identify the required all-decisions joint event, so authorization, filtering, deployment fit and current/new prediction are deliberately fail-closed and typed null.

`decision-time-joint-execution-probability-v2` is only a skeleton for one matured label/OOS prediction sample, never a signal-D feature or real-time action report. It binds production sample ID, signal/entry/exit sessions, all-decisions estimand and future evidence requirements, but v2 always emits `observed_joint_outcome_components_unavailable` plus `strict_joint_assessment_replay_not_verified`, forces entry/exit/net/joint/action probabilities to null and returns action qualification false. Opening a future version requires official unadjusted entry/exit OHLCV and amount, effective-dated exchange/ST/listing/delisting and corporate-action reference rules, both-side same-session capacity, a fixed signal-date full-market cohort with leave-one-out or predeclared external benchmark, observed entry-fill/exit-executable/net-positive labels and strict OOS assessment replay.

Probability data accumulation has two durable, deliberately separate artifact stages:

- Publishing an action-eligible current `official` exact-full-market run inserts its capture request into `market_scan_probability_capture_outbox` in the same SQLite transaction. The typed authoritative states are `pending`, `processing`, `succeeded` and `skipped`; inspect `archive_digest` and `last_error` rather than guessing from artifact files. Reconciliation, claim and retry re-run the full action gate. Query order is action eligibility, outbox, succeeded archive digest, source, then probability result. On success, outbox `archive_digest` must equal source `run_binding.source_integrity_digest`, and any calibrated result must bind that same digest; a mismatch fails closed. Current source v3/snapshot v3 records exact run skipped count plus ALL/SH/SZ/BJ population/eligible/success/missing/skipped conservation, PIT v4, registered v5 score contract and deterministic row replay. Source v1/v2 and score v4 remain exact audit-readable compatibility evidence only. Capture failure cannot downgrade the published rank.
- The scheduler task `maintain_market_scan_probability` runs only in the normal trading-day after-close daily-K window and inherits the scheduler's single-leader guard. It reads canonical source archives plus bounded local `qfq` rows and writes compact outcome artifacts under `data/research/market_scan_probability_outcomes/`; source feature vectors are referenced by digest instead of copied. The trusted exchange calendar fixes entry D+1 and exits D+2/D+6/D+21 for H1/H5/H20. Before a target close the result is `not_mature`. After maturity, a missing quote, entry, prior-exit, or exit bar remains explicit `data_unavailable` and is never shifted to a later session. Semantic-state comparison suppresses unchanged republishes; a later daily-K repair may produce a new verified content-addressed state. Scheduler task runs and monitor events retain maintenance counts and failures.
- A historical outcome-v1 artifact that is intact but carries the exact pre-fix `entry_rule_profile_degraded`/`exit_rule_profile_degraded` shape is not current replay evidence. After all mechanical, digest, sibling-record, quality and limitation checks pass, the loader raises typed `ProbabilityOutcomeSemanticDriftError` carrying its run/source/digest/as-of/generated identity. Maintenance retains that typed entry in the stable outcome catalog. If it matches the source digest and is at least as new as the latest valid outcome, the run is terminal for that maintenance pass and across restart: summary `due_count=0`, `skipped_count+=1`, no K-line cache read, no outcome publication and no fit publication. A strictly newer valid outcome recovers ordinary due evaluation. Source-research leaves the content-addressed file untouched, excludes its run and every cumulative dependent fit, and reports `outcome_evidence_status=legacy_semantic_drift_excluded`, `pipeline_stage=source_archived`, `fit_status=not_started`, and `selection_qualified=false`. Do not delete, edit, or re-sign the artifact. A source-digest mismatch, missing mechanical identity, same-order conflicting drift, digest mismatch, ordinary semantic mismatch, altered label/return/price/date/limitation, or invalid sibling is not terminal compatibility evidence and must still fail closed or remain non-authorizing.
- H1/H5/H20 formal split-v3 floors are 224/232/262 sessions at 95% coverage. The bounded sampled-assessment trigger remains 260 available sessions per horizon; at exactly 260 its H20 evidence remains formally insufficient until 262. The assessment keeps the newest 300 sessions, at most 90 SH/SZ/BJ-balanced rows per date and 27,000 rows, `include_records=false`, and exact source/outcome pair plus research digests. It never produces per-symbol forecasts.

Source, outcome and fit-assessment archives are evidence, not authorization to display or filter on a probability. Before assessment, the read projection reports `pipeline_stage`, `next_maturity_date`, `maintenance_status`, `fit_status`, `selection_status`, and per-horizon archived/mature/available/eligible observations and sessions. A bounded assessment exposes `fit_status=sampled_oos_assessment`; because its sampled benchmark is not the registered full-market benchmark, `fit_selection_qualified=false` and `fit_selection_qualification.reason=sampled_market_benchmark_not_full_market_contract` are invariant. Since no all-decisions joint projection or verified deployment artifact is published, the read projection still reports `status=insufficient_data`, `probability=null`, `selection_qualified=false`, `pipeline_stage=sampled_fit_assessed`, and `selection_status=projection_pending`. Do not lower the date or coverage floors, duplicate one date, infer a label from archive time, replace an unavailable fixed date, or describe the sampled assessment as a selectable `calibrated_shadow` projection.

When the UI appears to say that research is unavailable, inspect the typed reason:

- `source_scan_action_ineligible`: the frozen publication cannot authorize capture; do not add or edit an outbox row.
- `source_capture_pending`: the authoritative outbox is pending/processing. The browser polls only that run for at most 60 attempts, including failed requests, and stops on terminal state or run change.
- `source_capture_skipped` or `source_capture_outbox_missing`: capture ended without an archive or no authoritative request exists; inspect the persisted outbox reason.
- source archived / `insufficient_data`: capture succeeded and fixed-session samples are accumulating; this is not a zero probability.

A fresh failed/cancelled/interrupted batch with no publication, including
`#86`, intentionally makes zero results requests: the card must end non-busy as
**批次未发布·未进入研究归档**, with blank horizons and filtering disabled.
Queued/running batches remain busy, and a previously selected published result
is retained without mixing run identities. If a results request fails, the
card must show **证据读取失败·等待重试**, clear probability/filter state, and
allow a later successful retry to render normally. Direct API
`availability=ineligible_run_contract` uses the generic **来源批次不符合研究归档合同·未进入归档** because published intraday, TOP100, or legacy-seal runs can
also receive it; this terminal state never polls. The related limitation
`probability_requires_published_official_full_market_run` describes the required
contract and must not falsely assert that every ineligible input is unpublished.

Run `#82` is intentionally immutable. Its original publication lacks the
authorizing score-distribution/canonical replay evidence, so it must remain
`source_scan_action_ineligible`; never rewrite its diagnostics/results, reseal
it, fabricate an outbox row, or attach another run's archive. Start a new
after-close official v6/v5 run. A valid run should move from pending capture to
source-v3 accumulation, but the unchanged long-sample, OOS, joint-estimand and
deployment gates still keep percentages null until genuinely satisfied.

Retained real official evidence covers only two dates. Run 71 (2026-08-11) has 5,499 successful rows of 5,542 total; run 77 (2026-08-12) has 5,494 of 5,543. Both retained archives are source v1 and lack the production score hash, so both project as `legacy_unbound`, have zero filter-qualified horizons and leave H1/H5/H20 `probability=null`. Compatibility readers preserve source v1/v2 and score v4 exactly for audit; current source v3 strictly requires PIT v4, canonical action authority and the current v5 score contract. Do not rewrite legacy evidence to make it load, and do not interpret successful replay as an upgrade or authorization. These two runs prove historical capture availability, not current binding or predictive validity.

Probability artifact and source-research stores use non-blocking single-flight and atomic publication of a complete verified in-memory snapshot. One refresher may deep-verify a changed directory while warm readers continue using the prior last-good snapshot; no reader observes a partially rebuilt index. A cold caller without a prior snapshot may wait for that one verification. Corrupt, future, premature or ambiguous newest evidence fails closed rather than falling back to a convenient older candidate.

When the live cache is too short for the registered split, first create and verify a consistent runtime-data backup, then acquire an isolated longer-history input:

```bash
export PROBABILITY_SOURCE_BACKUP="/path/to/verified-runtime-backup"
env PYTHONNOUSERSITE=1 .venv/bin/python tools/runtime_data.py backup \
  --destination "$PROBABILITY_SOURCE_BACKUP"
env PYTHONNOUSERSITE=1 .venv/bin/python tools/runtime_data.py verify \
  "$PROBABILITY_SOURCE_BACKUP"
env PYTHONNOUSERSITE=1 .venv/bin/python tools/backfill_market_scan_probability_history.py \
  --source-database "$PROBABILITY_SOURCE_BACKUP/runtime.sqlite3" \
  --target-database data/research/market_scan_probability_history/tencent-qfq-360-20260811-attested.sqlite3 \
  --output-dir data/research/market_scan_probability_history \
  --symbol-limit 120 \
  --timeout 15
```

The source supplies only the current active `qfq` universe and anchor date and is opened read-only/query-only. The history command accepts only `runtime.sqlite3` next to the matching verified `manifest.json`, rejects any adjacent SQLite `-wal`, `-shm`, or `-journal` sidecar, and repeats full backup verification before and after the network fetch. Do not copy a live SQLite main file by itself: unchanged size/mtime cannot prove that WAL state was captured. The target must be a brand-new path outside the backup directory. The command selects a deterministic SH/SZ/BJ-balanced sample (90 default, 120 maximum), issues at most two concurrent Tencent requests with at most two retries, and accepts only exact, non-fallback 360-trusted-session qfq series. Any unrecovered provider request rejects the whole batch; new/short histories and suspension gaps may be excluded only while at least 60 symbols and 20 per market still pass. Publication stages and `quick_check`s a replay-compatible SQLite file, verifies every per-symbol digest, then publishes a content-addressed manifest. Its JSON stdout includes `source_backup_manifest`, `source_backup_sha256`, and `source_backup_verified_before_and_after_fetch` so downstream audit records can retain this proof chain. `status=ready` here means only that the history bundle is complete; it is not a probability fit.

Use that history only as an isolated research cohort:

```bash
env PYTHONNOUSERSITE=1 .venv/bin/python tools/backfill_market_scan_probability_replay.py \
  --database data/research/market_scan_probability_history/tencent-qfq-360-20260811-attested.sqlite3 \
  --output-dir data/research/market_scan_probability_replay \
  --start-date 2025-05-21 \
  --end-date 2026-07-13 \
  --symbol-limit 96 \
  --report data/research/market_scan_probability_replay/summary-20260811-attested-v2.json
```

This CLI rejects any adjacent SQLite `-wal`, `-shm`, or `-journal` sidecar, fingerprints the main file before and after evaluation, and uses one `mode=ro&immutable=1`/`query_only` transaction. Do not point it at a live or WAL-backed database; create a new static copy through the verified history workflow. It selects a deterministic balanced SH/SZ/BJ sample, uses bars no later than D for 11 common OHLCV features, and preserves exact fixed-session exits. Copy `start_date`, `end_date`, and the generated replay command from the history CLI's JSON summary rather than guessing them. Its source contract and stdout record `sqlite_immutable=true` plus `sqlite_sidecar_policy=reject_db_wal_shm_journal_v1`. It emits `historical_replay_v1`, `official=false`, and `live_cohort_compatible=false`; its target is absolute `net_return_positive`, not the official primary `net_excess_positive` target. Current-universe survivorship, unavailable historical listing/delisting and ST membership, missing amount/turnover/capacity, unmodelled historical price-limit tradeability, public-provider vintage, and possible later qfq rebasing remain declared limitations. The CLI is capped at 500 symbols, 100,000 requested date-symbol candidates, and a 256 MiB artifact; narrow the interval or symbol count when a bound is reached. Its artifact can be `ready` because modelled rows exist while the probability fit is still `insufficient_data`; only the horizon's own `status=calibrated_shadow` can authorize a non-null research forecast.

The commands above record retained 2026-08-11 evidence and must not overwrite the immutable target. The accepted 96-symbol/279-date replay and its metrics were generated under the now-superseded feature/label/split-v2 probability contract. It remains attested deterministic replay evidence and negative historical context, but is replay-only, never filter-qualified and cannot populate a current probability. A current-contract study must use new content-addressed output and the v3 H+1 semantics. The existing manifest/database/digest measurements remain provenance for the old study, not an authorization to relabel it.

The superseded replay's historical report was:

| Horizon | Required / available dates | OOS observations | AUC | Brier Skill | ECE | Bin evidence | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| H1 | 222 / 279 | 5,760 | 0.4993 | -0.00075 | 0.0130 | monotonic, highest bin not above base | `insufficient_data`, `probability=null` |
| H5 | 230 / 279 | 5,760 | 0.4938 | -0.00978 | 0.0980 | non-monotonic, highest bin not above base | `insufficient_data`, `probability=null` |
| H20 | 260 / 279 | 5,760 | 0.4520 | -0.00507 | 0.1592 | non-monotonic, highest bin not above base | `insufficient_data`, `probability=null` |

Those 222/230/260 values and metrics are frozen v2 evidence, not current floors. Current split v3 uses 224/232/262 because both gaps are H+1. Although 279 dates exceed those counts numerically, a new v3 evaluation is still required; the old near-random/worse metrics and failed bins cannot be reused as a current fit or filter authorization.

Source, outcome, fit-assessment, history, and replay archives are not automatically pruned. Supported valid artifacts recursively pin their explicit runtime run IDs, so ordinary runtime cleanup cannot delete a referenced SQLite graph and leave a dangling research chain. That protection is retention reachability, not an archival claim: back up the files and their referenced SQLite graphs together before any manual deletion, and never use the graph digest as a substitute. Outcomes and fit summaries remain integrity-bound to their exact source/outcome digests. History/replay files are reproducible only relative to the exact recorded provider and qfq vintage, so keep each SQLite file, content-addressed manifest, replay artifact, and source fingerprint together. Review `du -sh data/research/market_scan_probability_source data/research/market_scan_probability_outcomes data/research/market_scan_probability_fit data/research/market_scan_probability_history data/research/market_scan_probability_replay` periodically and place large exploratory output on a separately monitored volume when necessary. Never alias source/target/output paths, point a manifest/report at SQLite or an existing artifact, or overwrite a published bundle.

Startup preloads source research through cache I/O. Candidate source/outcome/fit files are decompressed and deep-verified outside the projection-state lock; only a complete candidate index is atomically published, so concurrent readers retain the prior complete warm snapshot. A representative fresh-process preload took 8.158412 seconds versus the prior 13.217-second cold observation; 100 warm projections averaged 0.000756715 seconds. A 201,055,106-byte run-62 legacy artifact returned typed unavailable in 0.00087925 seconds without deep interactive loading. Use these as regression budgets on that machine, not SLAs. Keep cold preload during startup/maintenance, preserve atomic swap semantics, and do not move deep archive verification back into an interactive request.

Generate the official-only D+1/D+2/D+3 future-range evidence separately:

```bash
python tools/evaluate_market_scan_future_range.py \
  --database data/ashare_radar.sqlite3 \
  --output-dir data/research/market_scan_future_range \
  --report data/research/market-scan-future-range-summary.json
```

Optional repeated `--run-id` narrows artifact targets, while repeated `--probability-artifact` may attach only previously persisted OOS `calibrated_shadow` context. `--minimum-sample-size`, `--minimum-session-count`, `--complete-run-coverage`, and `--bootstrap-samples` change research gates but cannot change the fixed `(1,2,3)` exchange-session offsets, HLC3 proxy contract, three-session moving-block length, production rank, or existing probability labels. The artifact also declares `validation_gap_sessions=3` as the minimum for any future train/test split; the current descriptive cohort has no fitted split and is not thinned by that setting. The command opens SQLite with `mode=ro`, `PRAGMA query_only=ON`, and one explicit transaction snapshot, then writes one immutable `market-scan-future-range-run-<id>-<digest>.json` per selected canonical action-eligible `official` run. A digest-valid `legacy_backfill`, wrong-scope publication or score-diagnostic-ineligible source is audit-only and is rejected as a new future-range source. The summary records SHA-256 before and after: equality is the static-copy byte-invariance proof; a difference on a running service is reported as `database_concurrent_external_change_detected=true`, not misattributed to this query-only process. Stop writers or use a static database copy when byte equality itself is required.

D is reconstructed only from digest-verified point-in-time scan evidence. Future dates come from the trusted exchange calendar; missing, suspended, zero-volume, not-yet-ingested, version-conflicting, or adjustment-rebased fixed dates remain unavailable and are never silently replaced by a later bar. HLC3 is a typical-price proxy, not VWAP. The D+1-open low/HLC3/high/close path and MFE/MAE are exploratory because a daily bar cannot reveal intraday order. The separate executable result marks D+1 same-day exit unavailable under T+1, uses D+2 as the existing H=1 close exit and D+3 as H=2, and records gross/net/cost/same-run benchmark/net-excess values only when the rule and fill model is valid. Future daily bars have no amount field, so capacity is only an explicit frozen signal-day amount proxy, not a future fill-capacity measurement. Evidence below the coverage or independent-date floor is persisted as `insufficient_data`; available descriptive mean/median/positive-rate observations may remain visible with that warning, but CI/pass/effectiveness fields stay blank and missing values are never replaced by zero.

Inspect one generated artifact through the read-only server path:

```bash
curl -sS 'http://127.0.0.1:8010/api/market-scans/1/future-range-research?page=1&page_size=20&session_offset=2'
curl -sS 'http://127.0.0.1:8010/api/market-scans/1/future-range-research?page=2&page_size=20&session_offset=2&include_research=false'
curl -sS 'http://127.0.0.1:8010/api/market-scans/1/future-range-research?page=1&page_size=20&symbol=600519.SH'
```

The wrapper always contains `generation_status`, `artifact`, `research`, and `record_page`. Missing artifacts return `not_generated` with empty records; a verified underpowered artifact returns `insufficient_data`; non-published/non-official/action-ineligible runs—including `preopen`, `legacy_backfill`, wrong scope and contradictory/missing score diagnostics—return `422`; a current-schema integrity failure returns generic `409`. The browser panel loads lazily only for the selected eligible official batch and displays an unavailable explanation for other modes/origins. Ordinary XLSX export appends **未来区间验证** from the same verified artifact, including fixed-session status, exploratory range/path evidence, executable gross/net/net-excess evidence, versions/digests, and blank cells for unavailable values. Neither API nor export invokes providers, recomputes outcomes, writes SQLite, or promotes a model.

Retry creates a new run whose `retry_of_run_id` points to the frozen original. The repository returns one `MarketScanRetryPlan`, and both manager validation and atomic creation use that same plan; a concurrent change aborts retry rather than mixing decisions. Any published `degraded` retry path that copies preserved success rows must first pass the complete shared action-source gate; run `#80` and a publication with missing/conflicting `score_distribution.pass` therefore cannot authorize reuse. Unsealed `failed`, `cancelled`, or `interrupted` sources follow their distinct lifecycle and ownership gates rather than pretending to have a publication seal. For `full-market-scan-v6`, every admitted retry resets the full universe, acquires a new all-quote capture envelope, and recomputes every row, so it does not reuse action-ineligible source rows. Clean-success copying and pending-only metadata refresh remain legacy pre-v6 compatibility paths only. `rule_version` is a stable hash over score, K-line/history, universe, metadata, snapshot, and publication rules. Retry requires an exact contract match; after a contract change, create a new scan instead of mixing scores.

The completed `data_date` is the scan's frozen end-of-day boundary, while `quote_date` identifies the required quote session. A quote revision timestamped after run creation remains eligible only when its Shanghai market date still equals `quote_date`; a K-line after `data_date`, or evidence from another required session, is rejected. `official` and `preopen` require `quote_date == data_date` and close-to-close alignment, while intraday keeps the current quote date separate from the previous completed K-line date. Retry that still needs market data is accepted only while the source mode's temporal contract resolves to exactly the same two dates. Because providers expose only the current snapshot, an explicit historical `as_of` cannot create a new run; historical rankings are read only from already persisted snapshots. The `task_run` row is created and attached to its queued scan in one transaction. Scan terminal state, ranking/count validation, and the linked task terminal update also commit together. Terminal writes retry only SQLite `BUSY`/`LOCKED` errors, at most three attempts with bounded backoff. If all attempts fail, the owning process records the run; once its local worker is gone and it still holds unified leadership, a later status or scan operation converges the row to `interrupted`. Another instance cannot perform that local recovery, while a crash/takeover still uses the startup reconciliation path.

The scoring minimum is exactly 61 complete bars. Current official v6 permits only two justified skips: `new_listing_insufficient_history`, when trusted PIT listing/session evidence proves the stock cannot yet have 61 complete sessions, and `official_session_gap`, when the trusted calendar and complete PIT bar dates prove a required exchange-session gap. Both require `market-scan-skip-evidence-v1` plus `market-scan-skip-pit-v1` and exact run/decision/rule/quote/bar binding. Every other unavailable or incomplete row is `missing`, never a skip, zero score or synthesized bar. For 1/5/20/60 windows, `official`/`preopen` reference `horizon+1`, while `intraday` references `horizon`. Without same-progress intraday volume, its volume contributions remain zero with an explicit alignment reason.

For skip-PIT parity only, a K-line snapshot `as_of` that is exactly canonical
`YYYY-MM-DD` is normalized to Shanghai-local midnight. An aware or full
timestamp keeps its ordinary absolute-time meaning. A future date-only value,
non-canonical spelling such as slash-separated text, decreasing observation
time, or value outside `[bar date 00:00, run decision]` is rejected. Do not
generalize this provider-compatibility rule to another timestamp boundary.

Production run `#86` is intentionally retained as failed/unpublished negative
evidence. Its terminal graph records 5,389 success / 154 missing / 0 skipped;
the immediate cause was rejection of canonical date-only provider K-line
`as_of` values during skip-PIT parity, which converted otherwise justified
exclusions to `missing` and prevented publication. A read-only immutable replay
found 146 typed skip candidates—38 new-listing and 108 session-gap—with 7,673
bound bars. The corrected new-run expectation is 5,389 success / 8 missing /
146 skipped. Operators must not edit, publish, reseal, or attach research to
`#86`; start a new official scan and verify that conservation instead.

Current research dimensions use `full-market-dimensions-v4-session-coverage` and `market-scan-point-in-time-feature-evidence-v4-bar-as-of-bound`. The evidence binds symbol/code/market/name/industry, source/date/metadata, persisted score fields, all 61 bar inputs and each bar's aware observation `as_of`. Verify absolute epoch order `quote event <= quote_observed_at <= run.as_of (decision)`; each bar's Shanghai-local date midnight must be `<= bar.as_of <= decision`, and bar observation times must be nondecreasing. `market-scan-session-coverage-v1` freezes the trusted expected exchange sessions and 5/20/60 windows; inspect `missing_session_dates`, `max_gap_sessions`, `confidence_penalty`, and `action_eligible`. Never synthesize a bar or edit the expected-session digest. For current official classification, only a replayable gap becomes the justified skip above; an unproved gap remains `missing`. PIT evidence v1/v2/v3 may be audited as legacy but cannot authorize current context, probability capture or action.

The stock pool has no fixed 5,000-row cap. Provider rows are canonicalized, required identity/provenance fields are validated, and symbols are de-duplicated before both coverage calculation and persistence. It must satisfy configured total and per-market SH/SZ/BJ minimums; a provider that lacks a required market is skipped. When a recent authoritative snapshot exists, a candidate must retain at least 98% of its total and each market count once the comparison floor is reached. Shanghai/Shenzhen B shares, delisted names, and future listing dates are excluded. AKShare industry/list-date aliases and placeholder values are normalized before persistence; total and per-market metadata completeness is logged separately and does not block price-only ranking. If a market has at least 100 rows but less than 80% industry coverage, DataHub may make one BaoStock bulk-industry request, fill only missing values, persist combined provenance such as `AKShare + BaoStock(行业)`, and keep the original pool when that isolated capability fails. `stock_industry` health/cooldown is separate from `stock`. ST recognition uses supported risk prefixes, while unknown industry/list date produces exact structured degradation. An authoritative three-market response uses one atomic delete/write/count-verification transaction. Inspect provider capability status, metadata events, and cached counts rather than lowering guards casually.

On trading days, mode-specific admission prevents unfinished or cross-session evidence from being relabelled. `preopen` is allowed only before 09:15 and reads the previous completed quote/K-line session; intraday is allowed only from 09:30 through 15:14:59 and reads today's quote against the previous completed K-line; `official` is rejected before 15:15 because today's daily bar is incomplete. A fully processed interrupted run may still be finalized when it performs no provider reads. Every score uses bars no later than the run's completed cutoff and a quote whose event date matches the required `quote_date`. Suspended, stale, non-`qfq`, missing-liquidity, low-quality, malformed and ordinary coverage failures are `missing` unless they satisfy one of the two exact justified-skip contracts; none receives a zero score. A `degraded` run is therefore usable but must be read together with coverage, issue counts, and per-result structured fallback/metadata provenance.

Each formal scan may make thousands of daily-K refresh requests. A compatible cache is an overlap-verified incremental base or explicit fallback, but the scan requires a real provider response before treating a row as freshly verified. The executor has a fixed 30-minute whole-run wall-clock budget and rechecks the completed trading date throughout execution. Keep one Uvicorn worker, begin with the defaults, and change batch/concurrency only after observing provider latency and error rates. The default daily-K chain uses Tencent's current `newfqkline` endpoint for SH/SZ/BJ, then AKShare, optional token-backed Tushare, and BaoStock. Every accepted sequence must satisfy the shared `daily-kline.v1`/`qfq` contract.

Before ranks are assigned, publication requires at least 95% eligible success coverage for ALL/SH/SZ/BJ, at least 90% eligible rows per market, and the v6 capture/time guards. Current `full-market-score-v5` computes `continuous_trend_adjustment=(2*continuous_trend_score-1)*4`, adds that bounded ±4 term to `leader_score-quality_penalty`, clamps base to `[0,100]`, and sets `raw_score=base`; only the display score is rounded. Distribution policy `score-layer-distribution-v4` still requires complete base/integer/final and component evidence. Inspect distinct counts, entropy, effective precision and variance; raw-only, constant-base and decimal-jitter pseudo-diversity evidence fails or degrades exactly as typed. Every new run must match its immutable `market_scan_rule_contract` full JSON and production rule/hash. Publication must also reproduce repository receipt `publication.canonical_replay.v1` / `market-scan-publication-replay-v1:<sha256>` from its fixed thresholds and full publication summary before probability source v3 can bind the run.

Quote capture fetches one bounded chunk at a time until every chunk is frozen and the envelope is sealed; only then is K-line work admitted through its semaphore, per-symbol timeout, retries, and backoff. Per-symbol retries apply only to symbol-specific K-line failures. A quote-capture failure or unavailable complete daily-K chain is raised to the shared recovery loop. Affected rows remain `pending` during the current run, and waits consume `ASHARE_RADAR_MARKET_SCAN_PROVIDER_WAIT_BUDGET_SECONDS`. A chain-wide cooldown-only signal is rechecked after at most 5 seconds instead of blocking one interactive scan for the full provider cooldown. If recovery is exhausted, the run becomes `failed` without bulk-inflating `missing_count`; the next v6 retry discards partial capture state and recomputes the full universe. Set the wait budget to `0` to fail immediately without sleeping.

Quote snapshots/history and daily K-lines persist `fallback_used`, so cache cannot silently turn degraded data into clean success. Ordinary fresh-cache origin is neutral in the full-market quality score because event time and anomaly checks already govern freshness; fallback and stale states still reduce quality. Overlap-verified incremental refresh preserves provenance, corporate-action differences force a full refresh, and repository vintage checks prevent an older sequence from replacing a newer one.

Set both `ASHARE_RADAR_SCHEDULER_ENABLED=1` and `ASHARE_RADAR_MARKET_SCAN_AUTO_ENABLED=1` to enable after-close scanning. The effective start time is the later of the configured schedule and 15:15, and the scheduler creates and retries `official` runs only; enabling it does not schedule an 08:00 `preopen` run. `ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_ENABLED` defaults to true; `ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS` bounds one SH/SZ/BJ stock-pool, quote, and completed-daily-K preflight. A failed preflight is persisted as a task run and monitor event without creating a formal scan. `ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS` defaults to `600,1800,3600`, and `ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS` defaults to three. Only same-date scheduled `official` runs and their automatic retries ending as retryable `failed` or `interrupted` are resumed; `preopen`, intraday, `cancelled`, `degraded`, manual, exhausted, and prior-date runs are not restarted. Preflight and formal retries are idempotently delayed rather than recreated on every scheduler tick.

Once a tick has fully verified a same-day terminal publication and decided there is no automatic work, later one-second ticks use only its lightweight market-scan header, database file/schema identity, leadership epoch and last-audit time. Unrelated monitor-event inserts do not force another whole-graph digest pass. A changed market-scan header/database identity or leadership epoch, and the fixed five-minute audit deadline, force full verification before another no-action decision. This is CPU suppression, not a trust relaxation: API reads never consume the shortcut and immediately run the full snapshot verifier. Thus result-only tamper may remain invisible to the scheduler until the next five-minute audit, but an API read fails closed immediately and no publication or action is authorized by the cached identity.

Provider priority is capability-filtered and can be overridden exactly with `ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY`, `ASHARE_RADAR_KLINE_PROVIDER_PRIORITY`, `ASHARE_RADAR_MINUTE_PROVIDER_PRIORITY`, `ASHARE_RADAR_STOCK_PROVIDER_PRIORITY`, and `ASHARE_RADAR_PLATE_PROVIDER_PRIORITY`. Names are comma-separated, normalized, de-duplicated, and unknown environment values fail configuration. Defaults include Futu in the quote order and Tushare in the daily-K order, but those entries are skipped unless the SDK and required local service/token are available.

The AKShare plate capability deliberately does not call the AKShare industry-board SDK pagination. It requests one Eastmoney HTTPS page for at most 100 rows with redirects disabled, requires exact HTTP `200`, enforces 2-second connect/read timeouts plus a 4-second internal deadline and a 512 KiB streamed response ceiling, and accepts the result only when `total == len(diff) <= 100`, every row is structured, `BK` codes/names are unique, numeric values are finite/nonnegative where required, and ranking is descending. Redirects, oversized or partial/paginated replies, malformed rows and deadline expiry are provider failures; inspect the capability error/monitor event, and expect the last good plate cache to remain optional context rather than trying to raise the row/page limit.

During a formal scan, batch-level pressure control treats provider busy, timeout, retry-after, and systemic chain-unavailable signals as shared pressure. It halves effective K-line concurrency immediately, never below one, and restores one slot after each healthy batch up to `ASHARE_RADAR_MARKET_SCAN_CONCURRENCY`. Its jittered delay remains bounded, honors provider retry-after, and consumes the existing scan-wide wait budget. Per-symbol no-coverage, suspension, short history, and other ordinary missing/skipped outcomes do not trigger global throttling. A low-cardinality pressure summary is retained in terminal diagnostics.

Active/manual work suppresses automatic overlap, and a same-day manual cancellation is not overridden automatically. A run snapshot and its results are retained or deleted as one graph; the configured keep window is a convergence target with active/database/artifact/retry-reference exceptions, as described under Retention Cleanup. Full-market scoring never calls the LLM.

The browser revalidates all static assets with `Cache-Control: no-cache`, and the discovery/scan entrypoints plus contracts/controller/polling/view modules share one import-map version token. The scan controller validates API shapes before replacing state and runs only one poll timer: an active run refreshes every two seconds even after the user switches tabs, while a compact global strip keeps progress plus return/cancel actions visible. A visible idle scan workspace checks `/latest` every 30 seconds so work started elsewhere can be discovered. It applies bounded exponential backoff, falls back to `/latest` after a run `404` or repeated refresh failures, retries immediately on `online`, and pauses when the document is hidden. Discovering a new run resets the old page/result snapshot. One polite live region announces scan and discovery milestones; progress keeps its ARIA busy/value/label state synchronized. If the UI appears stale after a deployment, inspect the network panel for one consistent module version rather than disabling browser cache globally.

Saved discovery presets and screen-alert events are local SQLite user data. Applying one queries only a completed persisted run and does not contact providers. The supported v2 criteria are market, industry, ST/new-stock state, quality, trend, change, turnover, amount, score, confidence, risk, tradability and keyword; the plan also owns its selected column view. Research enqueue records source run/preset/revision/name; that database reference pins the source graph during scan retention, so cleanup cannot silently invalidate the provenance. Import/export uses a versioned checksum. Applying/enqueuing and both sides of rank movement or plan-membership events require the complete shared action-source gate plus the same mode, exact scope and rule version; current unrankable evidence is not a false exit.

After 15:15 on a trading day, scheduler tasks refresh a bounded active research queue and evaluate bounded due reviews. They never call the LLM, skip excluded watchlist rows, isolate one-symbol errors, and require current high-quality `qfq`/rule provenance before writing advice. Repeated scheduler ticks do not duplicate a current unchanged conclusion. Review evaluation exposes `GET /api/reviews/summary`; its aggregate is local research evidence, not a performance guarantee. Investigate warning monitor events for stale data, quality/contract rejection, or per-symbol failures rather than forcing snapshots into history.

### Review and Paper-Simulation Checks

Treat the entire **复盘工具** as local Research Shadow. The advice-review card's `production_effect=none` and the paper account's no-broker boundary are semantic controls, not disclaimers that may be removed for space. All `/api/reviews...` and `/api/paper-trading...` responses must remain `Cache-Control: no-store`; an unexpected 5xx should contain only `个股研究暂不可用，请稍后重试`. A visible low-level SQLite/provider/validation message on a 5xx is a regression. Expected client input remains `400`/`409`/`422` with the non-cacheable header.

For a review inconsistency, compare the active plan's `revision` and `plan_payload_digest` with its `advice_review_plan_revision` row. Do not edit either digest by hand. A missing or mismatched revision should make the active read fail; a legacy/tampered evaluation should remain stored but project as `insufficient_data` with null metrics and disappear from favorable statistics. Current writes use `advice-review-evidence.v2`, which binds `attempt` and the server-owned `evaluated_at` in the input digest; v1 is audit-readable only through the conservative projection. Re-evaluating the exact same semantic input/result should return the existing attempt; changed evidence at the same `as_of` should append the next attempt. Current detail and summary select the latest `as_of`, then attempt/trusted audit time/row ID, only for the current revision—not the row with the latest write time across all revisions.

If a due review is pending or insufficient, check the trusted calendar coverage and every fixed session from the frozen snapshot through the requested cutoff. Each source row needs strict ISO date, `qfq`, supported data contract/version, non-fallback PIT time at or after 15:15, and explicit execution/suspension/corporate-action metadata. A suspended session must appear explicitly and can satisfy coverage, but it cannot touch target/stop. Do not repair a missing session by shifting to the next available bar or by inserting a carried trading row.

Paper strategy creation must send both `expected_plan_revision` and `expected_plan_payload_digest`; a conflict means refresh and reconfirm. Strategy reads and run admission must continue to match the immutable plan-revision payload; a mismatch is corruption, not an instruction to trust the mutable strategy row. A strategy that has not appeared in any run may be removed and then recreated from the same still-current plan revision; after the first run its row is protected by the immutable result history and deletion or source-wins semantic update is rejected. Account principal can change only while both the strategy and run tables are empty. During a replay, `data_unavailable` is the correct outcome for incomplete PIT/execution evidence. A blocked T+1 or suspension exit stays visible in the event ledger and must not be manually relabelled as a fill. Benchmark-unavailable can coexist with unchanged strategy trades; do not substitute a zero benchmark. Compare or export immutable runs instead of overwriting one. Before returning either view, the repository recomputes `20260813_paper_trading_output_digest_v3`; a mismatch means the complete stored run projection failed verification and must fail closed. A child strategy must belong to the same run's `paper_strategy_result` and symbol, and the four declared run counts must match stored results/trades/outcomes; an orphan child is corruption, not a row to hide with a join. Historical performance uses the selected run's `configuration.initial_cash`, so even direct corruption of the current account projection cannot silently change an old return denominator. Portability preflight rejects a current partial run bundle whose commitment cannot be reconstructed, any account update after Paper history exists, and any changed strategy already referenced by a run. Only a complete semantic-preserving identity remap may recompute the target commitment; do not use rehashing to bless changed historical allocation or execution semantics.

For listing-day price-limit diagnosis, `trading_session_count(listing_date, trade_date)` counts both ends of the trusted interval. The calendar's minimum date is therefore valid as listing session 1; a listing date one day earlier is truly outside coverage and must leave the price-limit profile unavailable with `trading_calendar_out_of_coverage`. Do not subtract one from this count or treat an exact left-boundary listing as missing history.

Troubleshooting order:

1. Check `/api/market-scans/latest` for `last_error`, counts, and the current message.
2. Check `/api/data/status`, `/api/system/diagnostics`, recent task runs, and provider capability failures.
3. Query results with `status=missing`, then `status=skipped`, to group concrete per-symbol reasons.
4. For a review failure, compare the plan revision/digest, source-window session coverage, PIT/execution metadata and requested `as_of`; never bypass an unavailable state by editing the ledger or moving the date.
5. For a screening failure, confirm the selected run is a published complete full-market run, clear any probability threshold, and compare the response `evidence.run_id`, `spec_digest`, and `canonical_digest` with the selected history row and Excel audit sheet. An unavailable delta or reminder with no compatible predecessor is expected, not provider degradation.
6. Retry after correcting a same-data-date source/network issue. A new linked v6 run freezes a new all-market quote envelope and recomputes every row; do not expect clean rows from the prior capture to be retained. If the completed trading date has advanced, start a new scan instead.
7. If a run remains active after an unclean process exit, let one process acquire the unified runtime-leader lock; startup or standby takeover reconciles it to `interrupted`. Do not edit status rows manually.

## 3. Diagnostics and Browser Notifications

### Health and Reliability

Use the three health endpoints for different decisions:

| Endpoint | Meaning | Dependencies | Failure handling |
| --- | --- | --- | --- |
| `GET /api/health` | Backward-compatible summary | Captured settings | Returns the existing `status/app/provider` payload. |
| `GET /api/health/live` | Process liveness | Application state only | Does not resolve the container, SQLite, scheduler, scanner, or providers. |
| `GET /api/health/ready` | Request admission readiness | Completed lifespan startup plus read-only SQLite `SELECT 1` | SQLite uses a 250 ms busy timeout inside a one-second outer bound; failure returns generic `503` state. |

All three responses use `Cache-Control: no-store`. Provider reachability is deliberately not a readiness condition: a provider outage should degrade research data without causing a local process restart loop. A standby process can also be ready; the readiness payload reports runtime role as `leader`, `standby`, or `single`. During startup and before shutdown cleanup, `accepting_requests=false` makes readiness return `503`.

`GET /api/system/reliability` returns local SLI/SLO evidence. Workbench usability, quality, freshness, non-fallback operation, provider attempts, and ordinary scheduler tasks use a seven-day rolling window with a 20-sample floor. Hourly aggregate queries round the seven-day start down to the containing UTC hour so the boundary bucket is not silently lost. Full-market scan success and ranked-symbol coverage use 30 days with a three-run floor; only runs with a non-empty universe count toward the coverage sample floor. Non-retry scan duration uses the same window and requires p95 at or below 90 minutes. The current ratio targets are:

| Indicator | Target |
| --- | ---: |
| Workbench usable | 99% |
| Workbench data quality at least 50 | 95% |
| Workbench quote/K-line fresh | 95% |
| Workbench without cache or fallback data | 80% |
| Provider attempt successful | 95% |
| Ordinary task successful or degraded | 95% |
| Full-market run successful or degraded | 90% |
| Full-market ranked / eligible-symbol coverage | 95% |

Each response carries the actual window, target, minimum, samples, good count, ratio, and status. `insufficient_data` means the sample floor was not met and must not be treated as a passing SLO. Cancelled work is excluded; interrupted scans count as unsuccessful, and retry-run durations are excluded from the duration percentile. Observations are aggregated into UTC-hour `reliability_bucket` rows rather than retaining request-level identifiers. `ASHARE_RADAR_MAX_RELIABILITY_BUCKET_ROWS` bounds those regenerable rows.

### System Diagnostics

The data-source/monitoring panel reads `GET /api/system/diagnostics`. The response separates cache fetch activity from market-data freshness and includes storage budget, scheduler state, provider status, table counts, bounded warnings, and remediation suggestions. Storage reports `sqlite_size_bytes` separately from managed `backup_size_bytes`/bundle count, with their sum as the backward-compatible database budget total. A separate read-only research-artifact catalog reports known probability/source/history/replay/future-range/report directories, regular-file count/bytes, ignored links/non-regular entries, and a total-managed reference value. It opens directories without following symlinks and exposes only a `preview_only` retention assessment: automatic deletion is always false and no file is declared safe until references and integrity manifests are independently proven. Row details separate quotes, daily/minute K-lines, full-market scan runs/results, other cache, other runtime, and user data instead of exposing only broad totals. Freshness covers quotes, daily/minute K-lines, the stock pool, and plate metadata; a non-empty stock-pool or plate cache without a usable update timestamp is reported as missing freshness metadata rather than healthy. The original SQLite budget warns at 80% of `ASHARE_RADAR_MAX_DATABASE_SIZE_MB`; the total-managed reference and material research occupation produce separate warnings without silently changing that historical budget denominator. The monitoring surface also reads data-source status, recent task runs, and monitor events; its normal refresh interval is 15 seconds.

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
| `ASHARE_RADAR_LEGACY_AUDIT_TIMEZONE` | `Asia/Shanghai` | - | IANA timezone used only to interpret legacy naive audit timestamps during the first UTC migration and user-data import. Set it before first startup when an old database was written in another host timezone; new audit timestamps are fixed-width UTC `Z`. |
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
| `ASHARE_RADAR_MAX_RELIABILITY_BUCKET_ROWS` | `10000` | - | Global retention cap for low-cardinality UTC-hour reliability aggregates; minimum `1`. |
| `ASHARE_RADAR_MAX_MARKET_SCAN_RUNS` | `30` | - | Newest-run keep-window target. Every unreferenced sealed graph outside the window is eligible and deleted as a verified result/run graph; active work and genuine database/file references may keep the physical count above target, and every noncandidate/directly protected retry root pins all reachable candidate ancestors. A run's outward `task_run_id` is not a pin. |
| `ASHARE_RADAR_MAX_MONITOR_EVENT_ROWS` | `3000` | `MAX_MONITOR_EVENT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_CACHE_EVENT_ROWS` | `5000` | `MAX_CACHE_EVENT_ROWS` | Runtime retention cap for cache/provider events. |
| `ASHARE_RADAR_MAX_ALERT_EVENT_ROWS` | `5000` | `MAX_ALERT_EVENT_ROWS` | Runtime retention cap for alert events. |
| `ASHARE_RADAR_MAX_ADVICE_HISTORY_ROWS` | `20000` | `MAX_ADVICE_HISTORY_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_DATABASE_SIZE_MB` | `2048` | - | Local SQLite and managed-backup capacity budget in MiB; sized for one full-market daily cache plus two backups, minimum `16`. Diagnostics warn at 80%. |
| `ASHARE_RADAR_RUNTIME_MAINTENANCE_INTERVAL_SECONDS` | `3600` | - | Minimum interval between automatic regenerable-data maintenance passes; range 60-604800 seconds. |
| `ASHARE_RADAR_MAX_RUNTIME_BACKUPS` | `2` | - | Managed runtime backup bundles retained per database; range 2-100. The default preserves two recovery points without multiplying the full-market cache footprint. API/CLI backup and restore operations pass this limit explicitly. |
| `ASHARE_RADAR_ADVICE_HISTORY_DEDUPE_SECONDS` | `180` | `ADVICE_HISTORY_DEDUPE_SECONDS` | Advice-history de-duplication window. |
| `ASHARE_RADAR_QUOTE_STALE_WARNING_SECONDS` | `900` | `QUOTE_STALE_WARNING_SECONDS` | Quote freshness warning threshold. |
| `ASHARE_RADAR_QUOTE_CONSISTENCY_WARNING_PCT` | `1.0` | `QUOTE_CONSISTENCY_WARNING_PCT` | Multi-source price-difference warning threshold. |
| `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH` | `0` | `TRADE_CALENDAR_AUTO_FETCH` | Non-blocking single-flight background refresh when runtime is missing, invalid, stale for the current date, or cannot cover a target. The triggering call uses the current bundle/closed decision; later calls see a successful atomic runtime update. |

Missing optional values use documented defaults. Present but malformed boolean, numeric, path, or LLM endpoint values fail configuration at startup instead of silently changing behavior. Restart after changing process variables or the allowlisted LLM assignments in `$HOME/.zshrc`.

### Provider Canary Variables

`tools/provider_canary.py` owns three tool-only environment variables. They are intentionally not `Settings` fields and are outside the application's configuration-document coverage contract:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASHARE_RADAR_CANARY_SH_SYMBOL` | `600519.SH` | Representative Shanghai symbol. |
| `ASHARE_RADAR_CANARY_SZ_SYMBOL` | `000001.SZ` | Representative Shenzhen symbol. |
| `ASHARE_RADAR_CANARY_BJ_SYMBOL` | `920066.BJ` | Representative Beijing symbol. |

Each value is normalized and must belong to the named market. Equivalent `--sh-symbol`, `--sz-symbol`, and `--bj-symbol` flags override the defaults. `--request-timeout` controls each representative quote/K-line probe and defaults to `provider_call_timeout_seconds`; `--stock-pool-timeout` independently controls the larger stock-pool refresh and defaults to `stock_pool_provider_timeout_seconds`. `--overall-timeout` remains the final deadline for the concurrent set.

The CLI creates and removes a temporary SQLite database, disables the scheduler for that isolated DataHub, and concurrently checks:

- one direct non-cached quote for each SH/SZ/BJ representative;
- one direct five-row completed daily-K request per representative; the response must contain exactly five ordered usable rows and pass finite OHLCV, cache/fallback, future-date, and staleness validation;
- a refreshed stock pool with valid unique identity rows and at least one SH, SZ, and BJ member.

Output is one sanitized JSON object. Exit `0` means every market and the stock-pool contract were available, `2` means at least one market remained available but the full contract was partial, and `1` means no market was available or provider cleanup failed. This is a live-provider diagnostic, not a required CI test:

```bash
$PYTHON tools/provider_canary.py
```

### LLM Remote Data Boundary

When LLM enhancement is enabled, the app sends an OpenAI-compatible chat-completion request to the configured remote endpoint. Chat messages contain the current question and topic; symbol and stock name; the deterministic rule answer; authoritative conclusion, confidence, support, resistance, actions, and invalidations; selected current quote/MA/trend/risk facts; data-quality score, level, source, and at most four notes; and at most six rule-evidence items. Local watchlists, stock notes, alert rules/events, advice history, provider credentials, full workbench payloads, and other local collections are not sent.

The transport also sends the configured model name, generation parameters, and `ASHARE_RADAR_LLM_API_KEY` as authentication to that endpoint. The API key is not inserted into chat messages, but the remote service necessarily receives it as a request credential. Use only an endpoint whose data-handling policy is acceptable, or set `ASHARE_RADAR_LLM_ENABLED=0` to keep Q&A rule-only.

The first model response is validated locally. If and only if that output fails local validation, the app may send one format-correction request with the same bounded context and a stricter instruction that the explanation contain no numbers or action words; it does not resend the previous raw model output. One outer timeout covers the first request, local validation, and correction together, so correction receives only the remaining `ASHARE_RADAR_LLM_TIMEOUT_SECONDS` budget rather than a new full timeout. The SDK's automatic retries are disabled. A request error, total-budget expiry, or second validation failure returns the deterministic rule answer without another remote attempt.

## 5. Verification Gates

Run before delivery:

```bash
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
npm ci
$PYTHON tools/runtime_contract.py
$PYTHON -m ruff check app tests tools
$PYTHON -m mypy
npm run check:js
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing
npx --no-install playwright install chromium firefox webkit
npm run test:e2e
```

`requirements-dev-lock.txt` is installed directly because it includes both runtime and engineering dependencies. Coverage fails below 90%. Tests run with `PYTHONNOUSERSITE=1` and must resolve packages from the active Python 3.12 runtime, never from a user-level or machine-specific interpreter path. Repository/database tests use temporary SQLite state and provider/network behavior is replaced with fakes at unit-test boundaries; live provider access belongs only in the optional canary.

The Security workflow is an additional required gate. Its local-equivalent dependency and reproducible-SBOM checks are:

```bash
$PYTHON -m pip install --only-binary=:all: --require-hashes -r requirements-security-lock.txt
$PYTHON -m pip check
npm ci --ignore-scripts
$PYTHON -m pip_audit --require-hashes --disable-pip --strict --progress-spinner off --requirement requirements-lock.txt
$PYTHON -m pip_audit --require-hashes --disable-pip --strict --progress-spinner off --requirement requirements-dev-lock.txt
$PYTHON -m pip_audit --require-hashes --disable-pip --strict --progress-spinner off --requirement requirements-security-lock.txt
npm audit --audit-level=high
first_dir="$(mktemp -d)"
second_dir="$(mktemp -d)"
$PYTHON tools/generate_sbom.py --output-dir "$first_dir"
$PYTHON tools/generate_sbom.py --output-dir "$second_dir"
diff -ru "$first_dir" "$second_dir"
rm -rf "$first_dir" "$second_dir"
```

CI additionally installs a checksum-verified Gitleaks binary, scans the current source and complete Git history with `--redact=100`, and uploads the normalized CycloneDX artifacts. Do not replace that history scan with a latest-tree grep. `tests/test_supply_chain.py` guards SHA-pinned actions, disabled checkout credential persistence, both Python lock audits, npm audit, redacted current/history scans, Dependabot ecosystems, and two-run SBOM comparison.

Smoke checks:

```bash
curl -sS http://127.0.0.1:8010/api/health
curl -sS http://127.0.0.1:8010/api/health/live
curl -sS http://127.0.0.1:8010/api/health/ready
curl -sS http://127.0.0.1:8010/api/system/reliability
curl -sS 'http://127.0.0.1:8010/api/stocks?keyword=600519&limit=5'
curl -sS 'http://127.0.0.1:8010/api/stock/workbench?symbol=600519'
```

## 6. Dependency and Documentation Maintenance

Keep direct dependencies in the appropriate input file. Runtime libraries belong in `requirements.txt`; test, lint, type, and lock-compilation tools belong in `requirements-dev.txt`; only vulnerability-audit and SBOM generators belong in `requirements-security.txt`. These inputs are for lock generation: reproducible installs use `--require-hashes` and the generated locks. A runtime-input change requires rebuilding the runtime and development locks because `requirements-dev.txt` includes `requirements.txt`; a development-only or security-tool change requires rebuilding only its matching lock. Do not edit generated locks by hand. Verify the locks in clean Python 3.12 environments:

```bash
$PYTHON -m piptools compile --generate-hashes \
  --output-file=requirements-lock.txt requirements.txt
$PYTHON -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements-dev-lock.txt requirements-dev.txt
$PYTHON -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements-security-lock.txt requirements-security.txt
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
$PYTHON -m pip install --only-binary=:all: --require-hashes -r requirements-security-lock.txt
$PYTHON -m pip check
```

After any dependency lock changes, audit all three Python locks, run `npm audit`, and regenerate both SBOMs. `tools/generate_sbom.py` consumes `requirements-lock.txt` and `package-lock.json`, validates CycloneDX JSON, removes volatile serial/timestamp fields, imposes deterministic ordering, and writes `python.cdx.json` plus `npm.cdx.json` atomically. The Security workflow generates twice and compares bytes before artifact upload. Its separate tool lock prevents an audit-only Linux runner from building optional provider source distributions. A reproducible SBOM is an inventory aid; it is not a signed release or provenance attestation.

Dependabot runs weekly for pip, npm, and GitHub Actions. Review generated changes through the same tests instead of merging solely because a version is newer. Keep every `uses:` reference pinned to a reviewed 40-character commit SHA and preserve `persist-credentials: false` for checkout.

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
