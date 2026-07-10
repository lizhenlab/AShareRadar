# Requirements Specification

## 1. Purpose

AShareRadar provides a local, Chinese-language A-share single-stock research workbench for personal study. It helps a user inspect quote state, trend structure, data quality, risk signals, alerts, notes, and research evidence for one stock at a time.

The product intentionally avoids portfolio optimization, automated trading, account login, and order execution.

## 2. Stakeholders

- Primary user: an individual A-share investor or researcher.
- Maintainer: future developer extending providers, analysis logic, UI panels, tests, and documents.
- External systems: Tencent/Eastmoney public endpoints, AKShare, BaoStock, Tushare, optional Futu OpenAPI, optional OpenAI-compatible LLM API.

## 3. Product Scope

### In Scope

- Query one A-share stock by 6-digit code or standard symbol.
- Load a workbench snapshot containing quote, K-line, data quality, trend score, support/resistance, advice, insights, chart marks, alerts, alert events, and notes.
- Display market overview and sample strong-stock observations.
- Maintain local watchlist, advice history, alerts, alert events, notes, chart marks, monitoring events, task runs, provider state, and cached market data.
- Explain analysis through transparent rule logic, with optional LLM wording enhancement.
- Provide diagnostics for cache freshness, provider health, scheduler state, and storage size.

### Out of Scope

- Brokerage account integration.
- Automated order placement.
- Portfolio-level allocation.
- Guaranteed real-time market data.
- Financial, legal, or investment advice.

## 4. Functional Requirements

| ID | Priority | Requirement | Acceptance / Contract | Primary Evidence | Test References |
| --- | --- | --- | --- | --- | --- |
| FR-001 | Must | The user can query a stock by symbol and load the main workbench. | `GET /api/stock/workbench?symbol=...` returns `StockWorkbench` for a confirmed stock; the first screen can render quote, analysis, K-line, local user state, and research panels without requiring separate provider calls. | `GET /api/stock/workbench`, `static/app.js::loadAll`, `app/workflows/workbench_pipeline.py` | `tests/test_individual_workflow_modules.py`, `tests/test_workbench_pipeline_modules.py`, `tests/test_frontend_app_flow.py` |
| FR-002 | Must | The app validates stock symbols and returns Chinese error messages for invalid input or unavailable data. Six-digit all-zero codes and conflicting market prefix/suffix forms are rejected as malformed in the UI and again before provider or stock-pool lookup. | Invalid symbols fail before provider calls; API errors are normalized to Chinese `detail`; the previous successful UI state is not replaced by misleading data. | `static/js/symbols.js`, `app/utils/symbols.py`, `app/api/errors.py` | `tests/test_symbol_modules.py`, `tests/test_api_error_modules.py`, `tests/test_api_stock_routes.py`, `tests/test_frontend_app_flow.py` |
| FR-003 | Must | The app retrieves quotes from provider priority order, uses cache when valid, preserves requested symbol order, limits public batch quote requests, and falls back to backup providers or cache when appropriate. | Batch quote endpoints reject empty/oversized requests, dedupe normalized symbols, preserve requested order, mark cache/fallback rows, and keep provider status/cooldown accurate. | `app/api/routes/quotes.py`, `app/services/datahub_quotes.py`, `app/services/eastmoney_client.py` | `tests/test_datahub_quotes_modules.py`, `tests/test_quote_stream_modules.py`, `tests/test_market_sampling_modules.py`, `tests/test_data_sources.py` |
| FR-004 | Must | The app retrieves daily K-lines and minute K-lines with cache and data quality metadata, and K-line analysis must ignore internally inconsistent, non-finite, or negative-volume rows from providers or local cache. Minute analysis must confirm the stock exists before degrading source failures into an unavailable report. | Daily/minute K-lines are filtered on write/read, stale minute business timestamps do not suppress provider refresh, daily and minute cache freshness are tracked separately, and nonexistent stocks return not-found before minute-source degradation. | `DataHub.kline`, `DataHub.minute_kline`, `app/services/datahub_klines.py`, `app/workflows/stock_analysis.py`, `app/services/minute_analysis.py` | `tests/test_datahub_cache_modules.py`, `tests/test_datahub_klines_modules.py`, `tests/test_minute_analysis_modules.py`, `tests/test_analysis_research.py` |
| FR-005 | Must | The app computes trend score, support/resistance, risk level, buy/sell points, T-plan, action advice, beginner summary, and intraday minute-line trend/volume/T-plan references. Optional plate-rank context failure must not fail quote/K-line analysis. | Analysis outputs use finite inputs only, keep rule priority stable, degrade optional context failures to notes/logs, and avoid converting local enrichment failures into analysis 503s. | `app/services/analysis.py`, `app/workflows/stock_analysis.py`, `app/services/analysis_signal_*.py`, `app/services/minute_analysis.py`, `app/services/indicators.py` | `tests/test_analysis_signal_modules.py`, `tests/test_indicator_trend_modules.py`, `tests/test_indicator_levels_modules.py`, `tests/test_local_lifecycle.py`, `tests/test_analysis_research.py` |
| FR-006 | Must | The app computes data quality score and conservative downgrade notes when quote or K-line quality is weak. | Weak/stale/fallback/demo/malformed quote or K-line inputs produce explicit notes/anomalies and bounded score penalties instead of silent optimistic signals. | `app/services/data_quality.py`, `app/services/data_quality_components.py`, `app/services/data_quality_time.py`, `app/services/data_quality_kline.py` | `tests/test_data_quality_modules.py`, `tests/test_analysis_research.py` |
| FR-007 | Should | The app exposes single-stock insight reports across research evidence, diagnosis, factor/leadership/alpha analysis, market regime, timeframe/risk-reward, Q&A, peer/theme/chip/replay context, T-strategy, valuation/financial health, fund flow/order pressure, event/LHB/abnormal-event summaries, strategy cards, and rule matches. | Each report endpoint returns a typed response with conservative missing-data text, finite-number sanitation, and stable rule priority; factor calibration does not add overlapping per-factor trading dates; timeframe reports include only fully covered requested windows; validation cannot confirm a signal when a required price/average is missing. | `app/api/routes/stock.py`, `app/workflows/individual.py`, `app/workflows/workbench_pipeline.py`, `app/services/research_*.py` | `tests/test_research_*_modules.py`, `tests/test_frontend_research_panels.py`, `tests/test_static_assets.py`, `tests/test_tool_inventory_modules.py` |
| FR-008 | Must | The app supports local alerts with create, update, delete, evaluate, cooldown, trigger, and recovery event behavior. Unknown update fields must be rejected rather than silently ignored. | Alert CRUD validates finite thresholds/cooldowns, unsupported legacy rows remain readable but disabled, concurrent evaluation uses snapshot-guarded state updates, recoverable provider failures are isolated per rule and reported, event numbers are sanitized, and SQLite failures still map to API 503. | `app/api/routes/alerts.py`, `app/services/alerts.py`, `app/repositories/alerts.py` | `tests/test_api_alert_routes.py`, `tests/test_rules_alerts.py`, `tests/test_local_lifecycle.py`, `tests/test_analysis_research.py` |
| FR-009 | Must | The app supports watchlist add/delete/list and advice history. | Watchlist CRUD uses validated symbols and current quote snapshots; advice history keeps dirty legacy rows displayable without swallowing fresh snapshots; local read failures stay localized. | `app/api/routes/watchlist.py`, `app/repositories/watchlist.py`, `app/repositories/advice.py` | `tests/test_api_watchlist_routes.py`, `tests/test_local_lifecycle.py`, `tests/test_frontend_app_flow.py` |
| FR-010 | Must | The app supports stock notes and chart marks derived from notes, events, and rule matches. Note dates must be normalized before storage, and invalid historical note dates must not align to K-line marks. | Notes reject blank content, non-positive/non-finite manual prices, invalid dates, and unknown fields; chart marks preserve visible limits and avoid aligning malformed historical dates. | `app/api/routes/notes.py`, `app/repositories/notes.py`, `app/services/chart_marks.py` | `tests/test_api_notes_routes.py`, `tests/test_local_lifecycle.py`, `tests/test_static_assets.py` |
| FR-011 | Must | The app exposes provider status, per-capability source plan, scheduler status, task runs, monitor events, and diagnostics. Provider failures must be reported by capability, including plate and order-book failures; manual task failures must not be reported as successful. | Diagnostics show cache/provider/scheduler/storage health; stale provider failures age out consistently; quote refresh compares requested and returned symbols; partial/missing/fallback results are warnings; all-missing refreshes fail; cancelled tasks persist a terminal cancelled state. | `app/api/routes/data.py`, `app/api/routes/monitoring.py`, `app/services/system_diagnostics.py`, `app/services/scheduler.py` | `tests/test_datahub_status_service_modules.py`, `tests/test_datahub_source_plan_modules.py`, `tests/test_provider_failure_status_modules.py`, `tests/test_scheduler_modules.py`, `tests/test_system_diagnostics_modules.py`, `tests/test_api_monitoring_routes.py` |
| FR-012 | Should | The app can optionally use an OpenAI-compatible LLM only after `ASHARE_RADAR_LLM_API_KEY`, `ASHARE_RADAR_LLM_BASE_URL`, and `ASHARE_RADAR_LLM_MODEL` are explicitly configured. | LLM settings come from environment variables, not project config files; missing or malformed configuration falls back to deterministic rule text; secrets are redacted from errors. | `app/services/llm_explainer.py`, `app/config.py`, `/Users/zl/.zshrc` local setup | `tests/test_config_modules.py`, `tests/test_llm_explainer.py` |
| FR-013 | Must | The UI refreshes quotes through SSE while preserving last successful data when a new request fails, ignoring stale async responses, and keeping user-data inputs synchronized with the active stock. | SSE symbol lists are normalized and encoded; each fresh stock session resets reconnect backoff; invalid searches invalidate pending valid loads; delayed redraws and watchlist mutations recheck current state before touching the DOM; malformed/stale frames and companion responses cannot corrupt current panels. | `GET /api/stream/quotes`, `static/app.js`, `static/js/workbench.js`, `static/js/watchlist.js` | `tests/test_quote_stream_modules.py`, `tests/test_frontend_app_flow.py`, `tests/test_static_assets.py` |
| FR-014 | Must | Market overview and strong-stock sampling distinguish complete, partial, unavailable, and unconfigured samples. | Responses expose requested/success/missing counts, degradation state, and bounded warnings; available rows remain visible after partial failure, default-scope all-failure is labelled, explicit custom-list all-failure remains a 503, and K-line failure is not presented as an ordinary empty ranking. | `app/services/market_sampling.py`, `app/workflows/market_overview.py`, `StrongStockWatchResponse`, `MarketOverview`, `static/js/workbench.js` | `tests/test_market_sampling_modules.py`, `tests/test_market_overview_modules.py`, `tests/test_api_stock_routes.py`, `tests/test_frontend_app_flow.py` |
| FR-015 | Must | Optional local and research context failures retain provenance without blocking quote/K-line-backed analysis. | Workbench local chart-mark/alert/event/note/advice failures return component-level sanitized warnings; market breadth distinguishes a true small sample from source degradation and applies conservative risk credit; peer comparison distinguishes unavailable, partial, invalid, and genuinely insufficient samples. | `app/workflows/individual.py`, `app/workflows/workbench_pipeline.py`, `app/workflows/stock_analysis.py`, `app/services/research_breadth.py`, `app/services/research_peer.py` | `tests/test_individual_workflow_modules.py`, `tests/test_workbench_pipeline_modules.py`, `tests/test_stock_analysis_modules.py`, `tests/test_research_breadth_modules.py`, `tests/test_research_peer_modules.py` |

## 5. Non-Functional Requirements

| ID | Requirement | Design Response | Verification |
| --- | --- | --- | --- |
| NFR-001 | Local-first operation. | SQLite cache at `data/ashare_radar.sqlite3`; no remote account storage. | `docs/OPERATIONS.md`, `tests/test_local_lifecycle.py`, `.gitignore` runtime-data boundary. |
| NFR-002 | Transparent degradation. | Provider status, cache freshness, quality score, component/sample status, bounded warning notes, and diagnostics distinguish source failure from valid empty data. | `tests/test_system_diagnostics_modules.py`, `tests/test_data_quality_modules.py`, `tests/test_market_overview_modules.py`, `tests/test_research_breadth_modules.py`, `tests/test_research_peer_modules.py`. |
| NFR-003 | Bounded storage growth. | Maintenance repository cleanup limits for quote history, minute K-lines, task runs, monitor events, and advice history. | `tests/test_local_lifecycle.py`, `tests/test_scheduler_modules.py`, retention settings in `docs/OPERATIONS.md`. |
| NFR-004 | API resilience. | Provider timeouts, failure cooldowns, fallback providers, cached snapshots, and stable Chinese error responses for domain/database failures. | `tests/test_api_error_modules.py`, route-level `tests/test_api_*_routes.py`, `tests/test_datahub_runtime_modules.py`. |
| NFR-005 | Security. | API keys are environment variables; project files must not contain real keys. | `tests/test_config_modules.py`, manual secret scan before delivery, `docs/OPERATIONS.md` environment table. |
| NFR-006 | Testability. | Domain logic is mostly services and repositories; tests use temporary SQLite files, factories, and fake providers. | `npm run check`, `tests/factories.py`, module-level tests listed in `docs/TEST_PLAN.md`. |
| NFR-007 | Maintainability. | Route, workflow, service, repository, DB, and UI modules are separated; production Python function size and branch complexity are guarded by tests and summarized in the function inventory. | `tests/test_tool_inventory_modules.py`, `docs/FUNCTION_INVENTORY.md`, `docs/DESIGN.md`, `docs/MAINTENANCE.md`. |
| NFR-008 | Backward-compatible local storage. | Compatibility columns and row migrations run before indexes that depend on newly added columns; initialization is idempotent for partial legacy databases. | `tests/test_schema_compat.py`, `app/db/schema.py`, `app/db/schema_migrations.py`. |

## 6. Data Requirements

- Quote fields: code, market, name, price, previous close, open, high, low, volume, amount, change, change percent, optional turnover, PE, PB, market cap, timestamp, source.
- K-line fields: date/timestamp, OHLC, volume, optional amount, turnover, interval, source, cache metadata.
- User data: watchlist, advice history, alert rules, alert events, notes.
- Runtime data: provider status, provider capability status, task runs, monitor events, cache events, trading calendar.

## 7. External Provider Requirements

- Tencent/Eastmoney style public quote endpoints are best-effort and can change.
- AKShare, BaoStock, Tushare, and Futu are optional dependencies configured through installed packages and environment variables.
- Tushare requires `ASHARE_RADAR_TUSHARE_TOKEN`; `TUSHARE_TOKEN` is accepted as a legacy alias.
- Futu requires `ASHARE_RADAR_FUTU_ENABLED=1` and local OpenD availability.
- The app must never silently treat demo data as real data unless `ASHARE_RADAR_DEMO_PROVIDER_ENABLED=1`.

## 8. Acceptance Criteria

- `npm run check` passes.
- `GET /api/health` returns app name `AShareRadar`.
- `GET /api/stock/workbench?symbol=600519` returns a structured workbench or a clear provider/data error.
- No real API key appears in project files.
- README and `docs/` describe startup, requirements, design, API contracts, operations, tests, function inventory, and maintenance guidance.
- `docs/API_REFERENCE.md` and `docs/FUNCTION_INVENTORY.md` are regenerated after route or Python function movement.
