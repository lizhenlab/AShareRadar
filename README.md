# AShareRadar

AShareRadar is a local A-share single-stock research workbench. It combines code/name lookup, adjustment-aware daily K-lines and inspectable intraday charts, trend and risk analysis, versioned advice history with as-of reviews, fixed-condition current/custom watchlist scans, notes, alerts, browser notifications, local data portability, and system diagnostics in a Chinese FastAPI web application backed by SQLite.

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
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1
```

Open `http://127.0.0.1:8010`.

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
```

Optional provider settings:

```bash
export ASHARE_RADAR_TUSHARE_TOKEN="your token"
export ASHARE_RADAR_FUTU_ENABLED=1
export ASHARE_RADAR_FUTU_HOST=127.0.0.1
export ASHARE_RADAR_FUTU_PORT=11111
```

Legacy variables such as `TUSHARE_TOKEN`, `FUTU_ENABLED`, and `SCHEDULER_*` remain accepted as aliases. New configuration should use the `ASHARE_RADAR_*` namespace.

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
- SQLite is the local persistence layer. The scheduler uses a database-adjacent process lock, so the supported Uvicorn topology is one worker. Scheduler status distinguishes degraded fallback results from failures and reports a non-owner process as standby while another instance holds the lock.
- Daily research uses an explicit `qfq` K-line contract with adjustment, as-of, data-version, and contract-version provenance. Cache keys isolate other adjustment modes and legacy `unknown` rows.
- Provider failures and cache fallback stay visible, and provider errors are sanitized before persistence or response rendering.
- Code/name autocomplete calls the existing stock-search endpoint only for a debounced, uncached, non-complete query. Chart inspection and local research-activity filtering use data already loaded in the browser and issue no requests.
- LLM wording is optional and explanatory. Failure falls back to deterministic rule text, and local watchlists, notes, alerts, advice history, and provider credentials are excluded from its prompt.
- User-owned records can be exported and transactionally imported as versioned JSON. Import commits require a matching server preview and verified rollback backup; scheduled cleanup removes only regenerable/runtime rows. Deleting an advice-review plan is irreversible and also deletes its evaluation history, while the source advice snapshot remains intact. Full backup/restore and manual retention cleanup are documented in the [Operations Guide](docs/OPERATIONS.md).
- Browser notifications can be enabled or disabled explicitly, and that preference survives page reloads. Alert-event pagination uses the event ID as its authoritative cursor. Enabling or re-enabling establishes a fresh baseline, so historical events and events created while notifications were disabled are not replayed; a failed delivery remains behind the cursor for ordered retry.
- Diagnostics apply freshness checks to quote and K-line data as well as stock-pool and plate metadata, including non-empty metadata caches that are missing a usable update timestamp.
- Files under `data/`, including the scheduler lock, are local runtime state and must not be committed; `data/.gitkeep` only preserves the directory.

## License

MIT. See [LICENSE](LICENSE).
