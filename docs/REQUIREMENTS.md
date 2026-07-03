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

| ID | Requirement | Evidence / Primary Entry Point |
| --- | --- | --- |
| FR-001 | The user can query a stock by symbol and load the main workbench. | `GET /api/stock/workbench`, `static/app.js::loadAll` |
| FR-002 | The app validates stock symbols and returns Chinese error messages for invalid input or unavailable data. Six-digit all-zero codes and conflicting market prefix/suffix forms are rejected as malformed in the UI and again before provider or stock-pool lookup. | `static/js/symbols.js`, `app/utils/symbols.py`, `app/api/errors.py` |
| FR-003 | The app retrieves quotes from provider priority order, uses cache when valid, preserves requested symbol order, limits public batch quote requests, and falls back to backup providers or cache when appropriate. | `app/api/routes/quotes.py`, `app/services/datahub.py`, `app/services/eastmoney_client.py` |
| FR-004 | The app retrieves daily K-lines and minute K-lines with cache and data quality metadata, and K-line analysis must ignore internally inconsistent, non-finite, or negative-volume rows from providers or local cache. Minute analysis must confirm the stock exists before degrading source failures into an unavailable report. | `DataHub.kline`, `DataHub.minute_kline`, `app/workflows/stock_analysis.py`, `app/services/eastmoney_client.py`, `app/services/minute_analysis.py` |
| FR-005 | The app computes trend score, support/resistance, risk level, buy/sell points, T-plan, action advice, beginner summary, and intraday minute-line trend/volume/T-plan references. Optional plate-rank context failure must not fail quote/K-line analysis. | `app/services/analysis.py`, `app/workflows/stock_analysis.py`, `app/services/analysis_signal_*.py`, `app/services/analysis_signals.py`, `app/services/minute_analysis.py`, `app/services/indicators.py`, `app/services/indicator_trend_components.py` |
| FR-006 | The app computes data quality score and conservative downgrade notes when quote or K-line quality is weak. | `app/services/data_quality.py`, `app/services/data_quality_components.py`, `app/services/data_quality_time.py`, `app/services/data_quality_kline.py` |
| FR-007 | The app exposes single-stock insight reports, including feature snapshot, factor lab, market regime, evidence chain, QA report, peer comparison, theme context, replay, risk radar, valuation, events, and rules. | `app/api/routes/stock.py`, `app/workflows/individual.py`, `app/workflows/workbench_pipeline.py` |
| FR-008 | The app supports local alerts with create, update, delete, evaluate, cooldown, trigger, and recovery event behavior. Unknown update fields must be rejected rather than silently ignored. | `app/api/routes/alerts.py`, `app/services/alerts.py`, `app/repositories/alerts.py` |
| FR-009 | The app supports watchlist add/delete/list and advice history. | `app/api/routes/watchlist.py`, `app/repositories/watchlist.py`, `app/repositories/advice.py` |
| FR-010 | The app supports stock notes and chart marks derived from notes, events, and rule matches. Note dates must be normalized before storage, and invalid historical note dates must not align to K-line marks. | `app/api/routes/notes.py`, `app/repositories/notes.py`, `app/services/chart_marks.py` |
| FR-011 | The app exposes provider status, per-capability source plan, scheduler status, task runs, monitor events, and diagnostics. Provider failures must be reported by capability, including plate and order-book failures; manual task failures must not be reported as successful. | `app/api/routes/data.py`, `app/api/routes/monitoring.py`, `app/services/system_diagnostics.py` |
| FR-012 | The app can optionally use an OpenAI-compatible LLM only after `ASHARE_RADAR_LLM_API_KEY`, `ASHARE_RADAR_LLM_BASE_URL`, and `ASHARE_RADAR_LLM_MODEL` are explicitly configured. | `app/services/llm_explainer.py`, `app/config.py` |
| FR-013 | The UI refreshes quotes through SSE while preserving last successful data when a new request fails, ignoring stale async responses, and keeping user-data inputs synchronized with the active stock. | `GET /api/stream/quotes`, `static/app.js::markLoadFailure` |

## 5. Non-Functional Requirements

| ID | Requirement | Design Response |
| --- | --- | --- |
| NFR-001 | Local-first operation. | SQLite cache at `data/ashare_radar.sqlite3`; no remote account storage. |
| NFR-002 | Transparent degradation. | Provider status, cache freshness, quality score, warning notes, and diagnostics. |
| NFR-003 | Bounded storage growth. | Maintenance repository cleanup limits for quote history, minute K-lines, task runs, monitor events, and advice history. |
| NFR-004 | API resilience. | Provider timeouts, failure cooldowns, fallback providers, cached snapshots, and stable Chinese error responses for domain/database failures. |
| NFR-005 | Security. | API keys are environment variables; project files must not contain real keys. |
| NFR-006 | Testability. | Domain logic is mostly services and repositories; tests use temporary SQLite files and factories. |
| NFR-007 | Maintainability. | Route, workflow, service, repository, DB, and UI modules are separated; large modules are tracked in maintenance docs. |

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
- README and `docs/` describe startup, requirements, design, tests, and function inventory.
