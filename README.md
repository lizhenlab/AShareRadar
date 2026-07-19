# AShareRadar

AShareRadar is a local A-share research workbench. It combines full-market SH/SZ/BJ scanning and deterministic ranking with code/name lookup, adjustment-aware daily K-lines, inspectable intraday charts, trend and risk analysis, versioned advice history, fixed-condition watchlist scans, notes, alerts, local data portability, and system diagnostics in a Chinese FastAPI web application backed by SQLite.

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

For development, install `requirements-dev-lock.txt` directly in the virtual environment; it already contains the complete runtime resolution plus the engineering tools. Node.js and npm are development tools and are not required to run the web application:

```bash
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
npm ci
npm run check
$PYTHON -m ruff check app tests tools
$PYTHON -m mypy
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
npx --no-install playwright install chromium
npm run test:e2e
```

`requirements.txt` and `requirements-dev.txt` are dependency inputs, not the reproducible installation entrypoints. Install the application from the hashed `requirements-lock.txt`; install development and CI environments from the hashed `requirements-dev-lock.txt`. The development lock includes runtime dependencies because `requirements-dev.txt` includes `requirements.txt`. JavaScript development dependencies are pinned by `package-lock.json` and installed with `npm ci`. `npm run check` remains the convenient local regression command; CI additionally enforces Ruff, incremental mypy, coverage, `pip check`, JavaScript syntax, browser regression, and generated-document drift.

## Documentation

- [Requirements Specification](docs/REQUIREMENTS.md)
- [Software Design Description](docs/DESIGN.md)
- [API Reference](docs/API_REFERENCE.md)
- [Test Plan and Test Report](docs/TEST_PLAN.md)
- [Operations Guide](docs/OPERATIONS.md)
- [Maintenance and Refactor Guide](docs/MAINTENANCE.md)
- [Function Inventory](docs/FUNCTION_INVENTORY.md)
- Research: [2026 Core Feature Study](docs/research/COMPETITOR_CORE_FEATURES_2026.md), [Current Capability Audit](docs/research/CURRENT_CAPABILITY_AUDIT.md), and [Product Gap and Roadmap](docs/research/PRODUCT_GAP_AND_ROADMAP.md)

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

Legacy variables such as `TUSHARE_TOKEN`, `FUTU_ENABLED`, and `SCHEDULER_*` remain accepted as aliases. New configuration should use the `ASHARE_RADAR_*` namespace.

## Full-Market Ranking

Open **全市场榜单** and click **开始扫描**. The API immediately creates a background run; the page then polls real progress and publishes a stable, pageable snapshot only after that run reaches a terminal state.

- The universe is the current listed A-share pool across SH, SZ, and BJ. It has no fixed 5,000-row truncation, rejects Shanghai/Shenzhen B shares, excludes delisted rows, and retains explicit ST/new-stock tags. Provider rows are normalized and de-duplicated once; coverage checks and the atomic cached-pool replacement use that same candidate set. Guards enforce both a 4,000-name total and configurable minimums for each market, and reject a large total or per-market drop from the latest authoritative snapshot before per-stock work begins.
- Suspended stocks, short histories, stale quotes/K-lines, malformed data, and source failures remain visible as `skipped` or `missing`; they never enter the ranking as zero-score stocks.
- Every first-time symbol request asks DataHub for up to 260 completed `qfq` daily bars. Later runs reuse a compatible cache and perform a small overlap-verified incremental refresh; a detected adjustment rebase triggers a full refresh. Cache persistence retains quote/K-line fallback provenance, and older K-line vintages cannot replace newer equal-length snapshots.
- `full-market-score-v1` combines the existing leader score at 85% and data quality at 15%. Leader inputs include trend, price change, volume ratio, amount, and turnover. Ties use trend score, change percentage, amount, then symbol in that order.
- Runs and per-stock results are persisted with as-of/data dates, sources, metrics, structured degradation provenance, status, rank, coverage, and duration. The completed `data_date` is the frozen end-of-day snapshot boundary, so later revisions from that same market date remain eligible while another date does not. Active runs are de-duplicated, can be cancelled, and become `interrupted` rather than falsely remaining active after a restart. One repository-generated retry plan controls validation and atomic derived-run creation: the original stays immutable, clean successful rows are reused, and missing/skipped or degraded rows are recalculated. Task creation/attachment and scan/task terminal changes each commit atomically; transient SQLite lock conflicts are retried, while an owned local terminal failure is later converged to `interrupted` after its worker exits.
- Full-market scoring is local and deterministic. It never calls an LLM per stock; LLM use remains an optional, on-demand explanation path for a selected stock.

The scan workspace validates API response contracts, uses a single bounded exponential-backoff poller, falls back to the latest run after a missing run or repeated refresh failures, resets pagination when it discovers a new run, and resumes immediately when the browser returns online. Static assets are revalidated with `no-cache`, and the scan ES modules share one version mapping.

On a trading day, manual scans are accepted only after the 15:15 completed-daily-bar boundary. Optional after-close scheduling, concurrency, timeouts, retention, degraded behavior, and troubleshooting are documented in the [Operations Guide](docs/OPERATIONS.md).

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

## License

MIT. See [LICENSE](LICENSE).
