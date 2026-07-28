# AShareRadar

AShareRadar is a local A-share research workbench. It combines trustworthy full-market SH/SZ/BJ scanning, replayable deterministic ranking, saved discovery presets, and a local research/review loop with code/name lookup, adjustment-aware daily K-lines, inspectable intraday charts, trend and risk analysis, versioned advice history, fixed-condition watchlist scans, notes, alerts, local data portability, and system diagnostics in a Chinese FastAPI web application backed by SQLite.

It is a research assistant, not an automated trading system. It does not connect to brokerage accounts, place orders, or provide investment advice.

## Quick Start

Python 3.12 is the supported runtime. Set `PROJECT_ROOT` to the checkout and use a project-local virtual environment so dependencies remain isolated when user site-packages are disabled.

```bash
export PROJECT_ROOT="$(pwd)"
python3.12 -m venv "$PROJECT_ROOT/.venv"
source "$PROJECT_ROOT/.venv/bin/activate"
export PYTHON="$PROJECT_ROOT/.venv/bin/python"
export PYTHONNOUSERSITE=1
$PYTHON -m pip install --require-hashes -r requirements-lock.txt
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --timeout-graceful-shutdown 5
```

Open `http://127.0.0.1:8010`.

The five-second Uvicorn graceful-shutdown bound prevents an open SSE stream or in-flight data request from keeping request draining pending indefinitely. Application/provider cleanup follows with its own bounded waits; the daemon provider-worker boundary described below handles an SDK call that cannot be interrupted.

For development, install `requirements-dev-lock.txt` directly in the virtual environment; it already contains the complete runtime resolution plus the engineering tools. Node.js and npm are development tools and are not required to run the web application. The checked contract is Python 3.12.x, Node.js 22.x or 24.x, and npm 10.x or 11.x; `.node-version` selects Node 22 as the local default while CI keeps a separate Node 24 compatibility smoke job:

```bash
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
npm ci
$PYTHON tools/runtime_contract.py
npm run check
$PYTHON -m ruff check app tests tools
$PYTHON -m mypy
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
npx --no-install playwright install chromium
npm run test:e2e
```

The three `requirements*.txt` inputs define runtime, development, and isolated security-tool profiles; their matching `*-lock.txt` files are the reproducible installation entrypoints. Install the application from `requirements-lock.txt` and development/CI from `requirements-dev-lock.txt`. The development lock includes runtime dependencies, while `requirements-security-lock.txt` deliberately contains only vulnerability-audit and SBOM tooling so the Linux Security runner never builds optional market-provider SDKs. JavaScript development dependencies are pinned by `package-lock.json` and installed with `npm ci`. `npm run check` remains the convenient local regression command; CI additionally enforces the runtime declaration, Ruff, ratcheted mypy, 90% branch coverage, `pip check`, JavaScript syntax, browser regression, and generated-document drift.

## Documentation

- [Requirements Specification](docs/REQUIREMENTS.md)
- [Software Design Description](docs/DESIGN.md)
- [API Reference](docs/API_REFERENCE.md)
- [Test Plan and Test Report](docs/TEST_PLAN.md)
- [Operations Guide](docs/OPERATIONS.md)
- [Maintenance and Refactor Guide](docs/MAINTENANCE.md)
- [Function Inventory](docs/FUNCTION_INVENTORY.md)
- [Engineering Quality Audit (verified 2026-07-24)](docs/research/ENGINEERING_QUALITY_AUDIT_2026.md)
- Research: [2026 Core Feature Study](docs/research/COMPETITOR_CORE_FEATURES_2026.md), [Current Capability Audit](docs/research/CURRENT_CAPABILITY_AUDIT.md), and [Product Gap and Roadmap](docs/research/PRODUCT_GAP_AND_ROADMAP.md)

Competitor, capability, gap, and roadmap research dated July 15-16, 2026 is retained as historical decision context. It is not a current implementation contract; use the design, operations, test plan, and dated engineering-quality audit for the current worktree.

Regenerate generated references after moving routes or Python functions:

```bash
$PYTHON tools/architecture_inventory.py
$PYTHON tools/api_inventory.py
```

Verify generated references without rewriting them:

```bash
$PYTHON tools/architecture_inventory.py --check
$PYTHON tools/api_inventory.py --check
```

## Configuration

Settings are read from process environment variables at startup. For the five `ASHARE_RADAR_LLM_*` variables only, a simple top-level assignment in `$HOME/.zshrc` is also accepted when the process environment does not define it; the profile is parsed, never executed. Project files do not store LLM credentials. Invalid boolean, numeric, path, or LLM endpoint values stop startup with a readable configuration error.

```bash
export ASHARE_RADAR_LLM_API_KEY="your OpenAI-compatible key"
export ASHARE_RADAR_LLM_BASE_URL="https://your-openai-compatible-endpoint/v1"
export ASHARE_RADAR_LLM_MODEL="your model name"
export ASHARE_RADAR_LLM_ENABLED=1
chmod 600 "$HOME/.zshrc"
```

Optional provider settings:

```bash
export ASHARE_RADAR_TUSHARE_TOKEN="your token"
export ASHARE_RADAR_FUTU_ENABLED=1
export ASHARE_RADAR_FUTU_HOST=127.0.0.1
export ASHARE_RADAR_FUTU_PORT=11111
```

Provider order can be overridden with comma-separated names:

```bash
export ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY="tencent,futu,akshare"
export ASHARE_RADAR_KLINE_PROVIDER_PRIORITY="tencent,akshare,tushare,baostock"
```

When configured and available, Futu joins the quote fallback chain and Tushare
joins the daily-K fallback chain. Missing optional SDKs, a disabled Futu OpenD,
or a missing Tushare token are skipped by capability rather than treated as
provider failures.

Legacy variables such as `TUSHARE_TOKEN`, `FUTU_ENABLED`, and `SCHEDULER_*` remain accepted as aliases. New configuration should use the `ASHARE_RADAR_*` namespace.

## Full-Market Ranking

Open **全市场榜单** and click **开始扫描**. The API immediately creates a background run; the page then polls real progress and publishes a stable, pageable snapshot only after that run reaches a terminal state.

- The universe is the current listed A-share pool across SH, SZ, and BJ. It has no fixed 5,000-row truncation, rejects Shanghai/Shenzhen B shares, excludes delisted rows, and retains explicit ST/new-stock tags. Provider rows are normalized and de-duplicated once; coverage checks and the atomic cached-pool replacement use that same candidate set. Guards enforce both a 4,000-name total and configurable minimums for each market, and reject a large total or per-market drop from the latest authoritative snapshot before per-stock work begins.
- Suspended stocks, short histories, stale quotes/K-lines, malformed rows, and genuine per-symbol coverage misses remain visible as `skipped` or `missing`; they never enter the ranking as zero-score stocks. A system-wide quote or daily-K provider-chain outage is different: the affected rows stay `pending`, the batch receives bounded retries within one cumulative provider-wait budget, and an unrecovered run fails in a retryable state instead of bulk-writing false `missing` results.
- Every first-time symbol request asks DataHub for up to 260 completed `qfq` daily bars. The default chain is Tencent's current SH/SZ/BJ `newfqkline` interface, AKShare (with its internal Eastmoney-direct and final Sina-forward-adjusted fallbacks), optional token-backed Tushare, and BaoStock. Unavailable optional providers are skipped by capability. Later runs reuse a compatible cache and perform a small overlap-verified incremental refresh; a detected adjustment rebase triggers a full refresh. Cache persistence retains quote/K-line fallback provenance, and older K-line vintages cannot replace newer equal-length snapshots.
- `full-market-score-v2` combines leader score at 85% and data quality at 15%. Its weights, thresholds, component algorithms, rounding mode, metric precision, tag rules, and tie-break order are serialized into a canonical specification with a stable SHA-256 hash. Every successful row stores its score inputs and component breakdown, so score and rank can be replayed from persisted data without provider access. Retries reject a changed scan contract instead of mixing scoring rules.
- Runs and per-stock results are persisted with as-of/data dates, sources, metrics, structured degradation provenance, status, rank, coverage, and duration. The completed `data_date` is the frozen end-of-day snapshot boundary, so later revisions from that same market date remain eligible while another date does not. Active runs are de-duplicated, can be cancelled, and become `interrupted` rather than falsely remaining active after a restart. One repository-generated retry plan controls validation and atomic derived-run creation: the original stays immutable, clean successful rows are reused, and missing/skipped or degraded rows are recalculated. Task creation/attachment and scan/task terminal changes each commit atomically; transient SQLite lock conflicts are retried, while an owned local terminal failure is later converged to `interrupted` after its worker exits.
- A run is publishable only when global and SH/SZ/BJ success coverage each reaches 95%, quote timestamps are parseable and span no more than 15 minutes, and no systemic same-day K-line lag cluster is detected. A 30-minute whole-run wall-clock budget and repeated completed-trading-date checks stop stale work while leaving pending rows available for retry.
- Automatic after-close scans run a bounded SH/SZ/BJ stock-pool, quote, and five-row completed-daily-K preflight before creating a formal batch. Retryable scheduled failures resume the persisted pending subset after configured 10/30/60-minute delays, stop after the configured limit or trading-date change, and never restart a user-cancelled, manual, or merely degraded batch.
- Batch pressure control starts at the configured concurrency, halves it on provider busy/timeout/retry-after or systemic unavailable signals, and restores one slot per healthy batch. All waits still consume the scan-wide provider budget; ordinary per-symbol coverage misses do not slow the whole market.
- Full-market scoring is local and deterministic. It never calls an LLM per stock; LLM use remains an optional, on-demand explanation path for a selected stock.

The scan workspace validates API response contracts, uses a single bounded exponential-backoff poller, falls back to the latest run after a missing run or repeated refresh failures, resets pagination when it discovers a new run, and resumes immediately when the browser returns online. Static assets are revalidated with `no-cache`, and the scan ES modules share one version mapping.

On a trading day, manual starts and retries that still need market data are accepted only after the 15:15 completed-daily-bar boundary. Optional after-close scheduling, concurrency, timeouts, retention, degraded behavior, and troubleshooting are documented in the [Operations Guide](docs/OPERATIONS.md).

## Discovery and Research Loop

- Saved discovery presets filter only fields already persisted by a completed scan: market, industry, ST/new-stock state, quality, trend, change, turnover, amount, and score. Presets support create, apply, rename, delete, versioned import/export, and optimistic revision checks. Applying a preset never fetches another data source.
- A filtered row can enter the watchlist research queue with immutable source run/preset/revision provenance. Adjacent completed runs are compared only when their rule versions match; otherwise the UI reports that ranking movement is not comparable instead of skipping to an older run.
- Historical watchlist-condition scans render read-only evidence with the requested `as_of`, rule version, condition results, and stored metrics. Opening the current stock workbench is a separate explicit action, so present data is never presented as a historical snapshot.
- After the daily-bar publication boundary, bounded scheduler tasks refresh advice for active, non-excluded watchlist rows without an LLM and evaluate due review plans. Each stock is isolated; stale dates, weak quality, invalid contracts, and unchanged conclusions do not create misleading snapshots. Mark-viewed uses the displayed advice ID as a watermark, preserving changes produced later.
- Review plans merge snapshot-derived structured evidence with user notes. Evidence records carry an ID, value, direction, data date, nature, and rule version. Evaluation records executable trigger/invalidation bases, target/stop outcomes, return, MFE, and MAE, while `GET /api/reviews/summary` exposes a global local-only aggregate.

## Current Architecture

```text
Browser UI
  -> FastAPI routes
  -> workflows
  -> services and provider adapters
  -> repositories and SQLite
```

Key runtime areas:

- `app/`: lifecycle, API, workflows, providers, analysis, models, and local persistence.
- `static/`: browser orchestration, rendering, charts, styles, and interactions.
- `data/ashare_radar.sqlite3`: local runtime cache and user data.

See [Software Design Description](docs/DESIGN.md) and [Maintenance and Refactor Guide](docs/MAINTENANCE.md) for the detailed module map.

## Runtime Boundaries

- The app is local, single-user software. Browser writes and explicit refreshes enforce the configured same-origin boundary; ordinary reads and metadata-free non-browser clients remain supported.
- Time has three explicit meanings: market events and calendar rules use aware `Asia/Shanghai`; audit/persistence timestamps use fixed-width UTC ISO 8601 text ending in `Z`; TTLs, deadlines, and elapsed durations use monotonic/performance clocks. The one-time SQLite migration interprets legacy naive audit text as Shanghai local time by default and converts it to UTC without changing market-event fields; set `ASHARE_RADAR_LEGACY_AUDIT_TIMEZONE` before first startup when an old database came from another host timezone. This is an offline upgrade: create a verified runtime backup, stop every old process, and follow the disk-space and validation procedure in `docs/OPERATIONS.md` before starting the new version.
- `GET /api/health/live` is a process-only liveness probe. `GET /api/health/ready` is an admission probe: it requires completed application startup and a bounded read-only SQLite check, but does not make provider-network availability a readiness dependency. Both probes use `Cache-Control: no-store`; a standby runtime can be ready and reports its role explicitly.
- `GET /api/system/reliability` summarizes low-cardinality hourly observations. Workbench and provider indicators use a seven-day window whose start is aligned to the containing UTC hour; ordinary tasks use an exact seven-day window, while full-market success, coverage, and duration use 30 days. Every objective reports its target, sample floor, ratio or percentile, and `insufficient_data` instead of treating a small sample as success.
- SQLite is the local persistence layer. One database-adjacent `runtime-leader` lock owns the scheduler and full-market scanner as a unit; bounded stop may return before a non-cooperative task, but leadership is released only after both services are truly idle. A standby then takes over both together. The supported Uvicorn topology remains one worker because status and controls are process-local.
- Daily research uses an explicit `qfq` K-line contract with adjustment, as-of, data-version, and contract-version provenance. Cache keys isolate other adjustment modes and legacy `unknown` rows.
- Provider failures and cache fallback stay visible, and provider errors are sanitized before persistence or response rendering.
- Blocking provider SDK work runs in a bounded runtime-owned daemon executor. Shutdown rejects new calls, cancels queued work, and waits only for its configured budget; an already-running uncooperative SDK call cannot be force-stopped, but its daemon worker does not keep Python alive at process exit.
- Code/name autocomplete calls the existing stock-search endpoint only for a debounced, uncached, non-complete query. Chart inspection and local research-activity filtering use data already loaded in the browser and issue no requests.
- LLM wording is optional and explanatory. Failure falls back to deterministic rule text, and local watchlists, notes, alerts, advice history, and provider credentials are excluded from its prompt.
- User-owned records can be exported and transactionally imported as versioned JSON. Import commits require a matching server preview and verified rollback backup; backup creation, verification, rotation, and restore share a bounded cross-process operation lease so an in-use bundle cannot be pruned. Scheduled cleanup removes only regenerable/runtime rows through throttled set-based retention. Active scans and the direct parent of each retained retry are safety exceptions; older retry ancestry can expire in the same pass. Large successful cleanups compact SQLite only when reclaimed pages are material, and compaction failure never rolls back logical retention. Deleting an advice-review plan is irreversible and also deletes its evaluation history, while the source advice snapshot remains intact. Full backup/restore and manual retention cleanup are documented in the [Operations Guide](docs/OPERATIONS.md).
- Browser notifications can be enabled or disabled explicitly, and that preference survives page reloads. Alert-event pagination uses the event ID as its authoritative cursor. Enabling or re-enabling establishes a fresh baseline, so historical events and events created while notifications were disabled are not replayed; a failed delivery remains behind the cursor for ordered retry.
- Diagnostics apply freshness checks to quote and K-line data as well as stock-pool and plate metadata, and report SQLite/managed-backup bytes plus quote, K-line, and full-market-scan row groups.
- Files under `data/`, including runtime-leadership and compatibility lock files, are local runtime state and must not be committed; `data/.gitkeep` only preserves the directory.

## Engineering Verification

Required CI is hermetic and does not call live market providers. The optional canary uses an isolated temporary SQLite database and probes representative SH/SZ/BJ symbols with a non-cached quote request, a five-row completed daily-K request, and a three-market stock-pool request. Market probes and the larger stock-pool refresh use their corresponding independent DataHub timeout settings under one overall deadline:

```bash
$PYTHON tools/provider_canary.py
```

Exit `0` means all three markets and the stock-pool contract are available, `2` means partial availability, and `1` means no market is available or provider cleanup failed. The command may use network/provider credentials and is intentionally outside required pull-request CI.

The separate Security workflow audits both hashed Python runtime/development locks and the npm graph, scans both the current tree and complete Git history for secrets with redacted output, generates Python/npm CycloneDX SBOMs twice and compares them byte-for-byte, and uploads the normalized artifacts. Dependabot covers pip, npm, and GitHub Actions; every third-party workflow action is pinned to a full commit SHA and checkout persistence is disabled. These controls improve traceability but do not claim artifact signing or a SLSA level.

## License

MIT. See [LICENSE](LICENSE).
