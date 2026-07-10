# AShareRadar

AShareRadar is a local A-share single-stock research workbench. It combines quotes, trend and risk analysis, data-quality checks, research panels, watchlists, notes, alerts, and system diagnostics in a Chinese FastAPI web application backed by SQLite.

It is a research assistant, not an automated trading system. It does not connect to brokerage accounts, place orders, or provide investment advice.

## Quick Start

Python 3.12 is tested and recommended. Use a project-local virtual environment so installed dependencies remain isolated and visible when user site-packages are disabled.

```bash
python3 -m venv .venv
source .venv/bin/activate
export PYTHON=python
export PYTHONNOUSERSITE=1
$PYTHON -m pip install -r requirements.txt
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

Open `http://127.0.0.1:8010`.

Useful checks require Node.js and npm; they are development tools and are not required to run the web application:

```bash
npm run check
npm test
npm run clean:caches
```

`npm run check` runs Python compilation, pyflakes, JavaScript syntax checks, and the full test suite without leaving routine cache artifacts in the worktree.

## Documentation

- [Requirements Specification](docs/REQUIREMENTS.md)
- [Software Design Description](docs/DESIGN.md)
- [API Reference](docs/API_REFERENCE.md)
- [Test Plan and Test Report](docs/TEST_PLAN.md)
- [Operations Guide](docs/OPERATIONS.md)
- [Maintenance and Refactor Guide](docs/MAINTENANCE.md)
- [Function Inventory](docs/FUNCTION_INVENTORY.md)

Regenerate generated references after moving routes or Python functions:

```bash
$PYTHON tools/architecture_inventory.py
$PYTHON tools/api_inventory.py
```

## Configuration

All settings can be supplied through process environment variables. For local convenience, the LLM variables shown below are also read directly from `~/.zshrc`; non-LLM settings must be exported into the process environment. Invalid optional numeric or boolean values fall back to safe defaults.

```bash
export ASHARE_RADAR_LLM_API_KEY="your OpenAI-compatible key"
export ASHARE_RADAR_LLM_BASE_URL="your OpenAI-compatible endpoint"
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

Key runtime files:

- `app/main.py`: application startup, routes, static files, and lifecycle.
- `app/config.py`: environment-backed settings.
- `app/api/routes/`: HTTP and SSE endpoints; `app/api/errors.py` owns API error mapping.
- `app/workflows/`: stock lookup, analysis, workbench, and market-overview orchestration.
- `app/services/datahub.py` and `app/services/datahub_*.py`: provider selection, caching, fallback, and status coordination.
- `app/services/analysis.py` and `app/services/research_*.py`: analysis and research rules.
- `app/repositories/` and `app/db/`: persistence, schema, migrations, and row mapping.
- `app/models/`: domain and API response models.
- `static/app.js` and `static/js/`: browser orchestration, rendering, charts, and user interactions.
- `data/ashare_radar.sqlite3`: local runtime cache and user data.

See [Software Design Description](docs/DESIGN.md) and [Maintenance and Refactor Guide](docs/MAINTENANCE.md) for the detailed module map.

## Runtime Boundaries

- The app is local, single-user software.
- SQLite is the local persistence layer.
- Public and optional providers can fail or change fields; degradation must remain visible.
- LLM output is explanatory only, with deterministic rule-based fallback and error redaction.
- Runtime files under `data/` are ignored by Git except `data/.gitkeep`.

## License

MIT. See [LICENSE](LICENSE).
