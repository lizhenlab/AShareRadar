# AShareRadar

AShareRadar is a local A-share single-stock research workbench. It combines quote retrieval, trend scoring, data-quality checks, strategy hints, intraday T+0 planning aids, watchlists, notes, alerts, and system diagnostics into a Chinese web UI backed by FastAPI and SQLite. Research panels and rule engines apply conservative finite-number guardrails so malformed prices, volumes, forward returns, factor inputs, rule evidence, intraday levels, and local user-state rows become explicit pending, missing-data, disabled, or fallback states instead of leaking `nan`/`inf` into reports. Feature snapshots, leadership evidence, market-regime evidence, and data-quality scoring sanitize malformed provider/cache fields before report text or scores are assembled.

It is a research assistant, not an automated trading system. It does not connect to brokerage accounts, does not place orders, and should not be treated as investment advice.

## Quick Start

Use Python 3.12 or newer. The local development runtime in this workspace is `/opt/anaconda3/bin/python3`.

```bash
export PYTHON=${PYTHON:-/opt/anaconda3/bin/python3}
export PYTHONNOUSERSITE=1
$PYTHON -m pip install -r requirements.txt
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

Open:

```text
http://127.0.0.1:8010
```

`requirements.txt` includes the runtime packages plus the local check tools used by this repository. `PYTHONNOUSERSITE=1` keeps user-level Python packages from shadowing the project runtime, which is important for the pandas/numpy stack used by AKShare and BaoStock.

Useful checks:

```bash
npm run check
npm test
npm run clean:caches
```

`npm run check` routes Python bytecode output to a temporary directory and disables pytest's cache provider, so routine verification should not leave `__pycache__/`, `*.pyc`, or `.pytest_cache/` artifacts in the worktree.

## Documentation

- [Requirements Specification](docs/REQUIREMENTS.md)
- [Software Design Description](docs/DESIGN.md)
- [API Reference](docs/API_REFERENCE.md)
- [Test Plan and Test Report](docs/TEST_PLAN.md)
- [Operations Guide](docs/OPERATIONS.md)
- [Function Inventory](docs/FUNCTION_INVENTORY.md)
- [Maintenance and Refactor Guide](docs/MAINTENANCE.md)

Regenerate generated inventory docs after code or route movement. The function inventory also records Python function-health hotspots so reviews can see the longest and branchiest functions by area.

```bash
$PYTHON tools/architecture_inventory.py
$PYTHON tools/api_inventory.py
```

## Configuration

LLM configuration is read from environment variables. The local shell profile used by this project is `/Users/zl/.zshrc`. Numeric and boolean settings fall back to safe defaults if a local value is malformed.

```bash
export ASHARE_RADAR_LLM_API_KEY="your OpenAI-compatible key"
export ASHARE_RADAR_LLM_BASE_URL="your OpenAI-compatible endpoint"
export ASHARE_RADAR_LLM_MODEL="your model name"
export ASHARE_RADAR_LLM_ENABLED=1
export ASHARE_RADAR_LLM_TIMEOUT_SECONDS=12
```

Tushare is optional:

```bash
export ASHARE_RADAR_TUSHARE_TOKEN="your token"
```

Futu OpenAPI is optional and disabled by default:

```bash
ASHARE_RADAR_FUTU_ENABLED=1 ASHARE_RADAR_FUTU_HOST=127.0.0.1 ASHARE_RADAR_FUTU_PORT=11111 \
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

Legacy variables such as `TUSHARE_TOKEN`, `FUTU_ENABLED`, and `SCHEDULER_*` are still accepted as aliases, but new configuration should use the `ASHARE_RADAR_*` namespace.

## Current Architecture

```text
Browser UI
  -> FastAPI routes in app/api/routes
  -> app/api/container.py and deps.py
  -> app/workflows/*
  -> app/services/*
  -> app/repositories/* and app/db/*
  -> SQLite cache and optional external providers
```

Key runtime files:

- `app/main.py`: app factory, route registration, static files, lifecycle.
- `app/config.py`: environment configuration.
- `app/api/errors.py`: API exception mapping, SQLite/database failure mapping, validation-message rules, and Chinese error responses.
- `app/api/routes/quotes.py`: quote endpoints, batch symbol limits, SSE stream symbol fallback with dirty fallback-symbol skips, canonical symbol validation, safe event formatting, refresh-interval guards, and quote-error events.
- `app/models/market.py`: quote with cache/fallback flags, K-line, stock profile, plate/concept, provider capability, and order-book models.
- `app/models/analysis.py`: analysis result, data quality, review, overview, strategy, finance, rule-match, and insight models.
- `app/models/research.py`: factor lab, regime, validation, risk/reward, diagnosis, Q&A, theme, chip, replay, and minute-analysis models.
- `app/models/user_data.py`: watchlist, alert, note, chart-mark, and advice-history models.
- `app/models/system.py`: provider status, source-plan, cache stats, diagnostics, task, and scheduler models.
- `app/models/workbench.py`: composite workbench/market response models, component-level local-data warnings, and quote-sample status contracts.
- `app/models/schemas.py`: backward-compatible re-export layer for existing imports.
- `app/db/schema.py`: schema initialization facade.
- `app/db/schema_definitions.py`: SQLite table and index definitions.
- `app/db/schema_migrations.py`: guarded compatibility columns, one-time migrations, migration records, and compatibility indexes.
- `app/db/mappers.py`: backward-compatible row-mapper facade.
- `app/db/market_mappers.py`: quote, K-line, stock-pool, plate, and concept row-to-model mapping.
- `app/db/system_mappers.py`: provider status, provider capability, scheduler task, and monitor-event row mapping.
- `app/db/user_mappers.py`: watchlist, advice, alert, note, and alert-condition label row mapping with legacy dirty-row sanitation for advice text/numbers, alert rule/event state, and displayable stock-note fallbacks.
- `app/repositories/market_data.py`: backward-compatible market data repository composition.
- `app/repositories/market_quotes.py`: column-list driven quote snapshot and quote-history persistence.
- `app/repositories/market_klines.py`: daily and minute K-line cache persistence.
- `app/repositories/market_metadata.py`: stock pool, plate rank, and stock concept persistence with explicit columns, stable ordering, de-duplication, and finite optional numeric fields.
- `app/repositories/provider_status.py`: provider/capability status persistence, enabled-state preservation, stable status ordering, explicit-column queries, and upsert operations.
- `app/repositories/provider_status_aggregation.py`: capability-to-provider health aggregation policy, active-capability metric selection, and disabled-capability history guards.
- `app/repositories/update_fields.py`: shared field-cleaning and SQL update-part helpers for user-data repositories.
- `app/repositories/advice.py`: advice-history persistence, dedupe windows, and finite-number comparison guards so dirty legacy advice rows cannot absorb new snapshots.
- `app/repositories/alerts.py`: alert-rule/event persistence with explicit columns, finite thresholds, bounded cooldowns, disabled unsupported legacy conditions, non-negative trigger counts, and sanitized event numeric values.
- `app/utils/symbols.py`: SH/SZ symbol normalization, market inference, all-zero symbol rejection, and provider-specific symbol formatting.
- `app/utils/market_data.py`: shared finite-number, OHLC, K-line, and minute K-line validity helpers used by providers, cache, analysis, factor scoring, and research guardrails.
- `app/services/analysis.py`: single-stock analysis assembly split into trend metrics, gated signal-point sets, copied optional history/peer inputs, and result composition.
- `app/services/analysis_signals.py`: backward-compatible analysis signal facade.
- `app/services/analysis_signal_points.py`: ordered risk-level, buy-point, sell-point, T-style, and strength-tag rules plus T-plan price zones with invalid-zone pending wording instead of misleading `0.00` references.
- `app/services/analysis_signal_quality.py`: low-quality data signal gates, per-signal-kind blocking rules, downgrade mapping, and quality-reason wording.
- `app/services/analysis_signal_snapshot.py`: signal confidence, sanitized score/label/quality views, cleaned contribution/note grouping, and direct-compatible signal summary.
- `app/services/analysis_signal_advice.py`: action advice and beginner summary wording.
- `app/services/leader_scoring.py`: shared leader-score profiles and tag rules used by feature snapshots and strong-stock ranking.
- `app/services/strong_stocks.py`: strong-stock watch list ranking, K-line evidence gate, leader score, reason text, and tags.
- `app/services/indicators.py`: backward-compatible indicator facade.
- `app/services/indicator_trend_components.py`: trend score contribution components, moving-average rule tables, slopes, price, position, turnover, and volume-confirmation rules.
- `app/services/indicator_trend.py`: trend score public entrypoint and compatibility exports.
- `app/services/indicator_levels.py`: support/resistance calculation with valid K-line filtering, breakout/breakdown adjustment, and level-order guardrails.
- `app/services/indicator_volume.py`: volume averages and shared positive-volume ratio helpers.
- `app/services/indicator_math.py`: shared moving-average, ATR, volatility, drawdown, quantile, and percentage-change helpers.
- `app/services/data_quality_components.py`: quote source/cache flags, ordered quote-field rules that reject non-finite/negative/zero critical values before derived diagnostics, K-line requirement wording, freshness, cache-source labels, and non-negative multi-source consistency score components with stable anomaly notes.
- `app/services/data_quality.py`: public data-quality response assembly and backward-compatible quality helper exports.
- `app/services/data_quality_time.py`: quote timestamp parsing, trading-session freshness checks, and expected trade-date helpers.
- `app/services/data_quality_kline.py`: K-line latest-date/source/cache/fallback quality assessment that ignores input-order tail mistakes plus ordered level and penalty rule tables.
- `app/services/datahub.py`: provider routing facade, coordinator wiring, runtime/cache ownership, and data-quality entry points.
- `app/services/datahub_cache.py`: quote/K-line cache tagging, symbol matching, stock-pool freshness, concept normalization, and explicit minute interval alias mapping.
- `app/services/datahub_klines.py`: daily K-line and minute K-line fetching, cache reuse only when fresh cache covers the requested limit, shared provider-attempt fallback, invalid-row filtering with date/time sorting before latest-window selection, best-effort cache persistence after provider success, bounded provider-call limits with malformed max-limit fallback, capability failure recording, cancellation propagation, empty-response guardrails, and fallback cache tagging.
- `app/services/datahub_metadata.py`: stock pool/profile resolution through `StockPoolResolver`, plus plate rank and concept membership fetching with shared provider-attempt fallback, incomplete-stock-pool safeguards, authoritative empty-result handling, stale keyword fallback boundaries, invalid/all-empty metadata-row filtering that preserves previous cache rows, best-effort cache persistence after provider success, AKShare plate capability failure recording, empty metadata-response guardrails, and non-mutating profile enrichment.
- `app/services/datahub_orderbook.py`: optional Futu order-book retrieval, Futu ping checks, provider-error wrapping, and order-book capability state recording.
- `app/services/datahub_quotes.py`: quote fetching, partial cache reuse, machine-readable short/fallback quote-cache tagging, best-effort cache persistence after provider success, quote quality entry, and multi-source consistency checks.
- `app/services/datahub_runtime.py`: provider call timeouts, shared provider-attempt iteration, timed calls, source-name fallback, best-effort capability success/failure recording, and short cooldowns.
- `app/services/datahub_status_service.py`: read-only DataHub status assembly, explicit provider enabled/capability synchronization, capability snapshots, and source-plan delegation.
- `app/services/provider_registry.py`: provider construction, Settings-to-provider injection, priority normalization, capability-kind mapping, fallback capability metadata, and enabled checks.
- `app/services/providers.py`: Tencent quote/K-line adapter, quote URL/HTTP helpers, safe malformed payload-shape extraction that tolerates trailing whitespace and rejects unclosed payloads, stripped field/minimum-count validation, strict quote/K-line price parsing, quote code/name validation, real timestamp validation, open/current price containment inside high/low, finite non-negative core amount/volume guards, malformed-row filtering, demo quotes with local random state plus rounded high/low containment, and demo K-line weekday backfill for small limits.
- `app/services/datahub_source_plan.py`: provider source-plan assembly, cleaned/deduped provider names and statuses, primary-source selection, decision/warning rule order, missing-primary downgrades, and recovery suggestions.
- `app/services/datahub_status.py`: provider source-key rules, capability labels/states, duplicate capability-status ranking, finite success-rate counts, source-plan summaries, recovery-action rules, and cleaned error text.
- `app/services/system_diagnostics.py`: cache freshness, provider/capability diagnostic decisions, scheduler, sanitized storage row counts/table-count keys, de-duplicated warnings/suggestions, and environment diagnostics used by the monitoring API.
- `app/services/scheduler.py`: task-definition driven local refresh tasks, manual/background failure semantics, positive-integer setting sanitation, symbol cleaning/de-duplication for refresh jobs, per-symbol K-line refresh degradation, data-health event rules, finite runtime cleanup summaries, bounded rescheduling, and manual task execution.
- `app/services/eastmoney_client.py`: Eastmoney lightweight quote/K-line HTTP client, endpoint retry, proxy bypass, readable invalid-symbol and malformed-response errors, transparent source labels, deduped fetch with request-order quote mapping, strict quote-field parsing, missing change-percent fallback, and malformed-row/K-line validation used by AKShare fallback.
- `app/services/provider_utils.py`: provider adapter helpers for dependency detection, symbol formatting, positive-limit checks, compatibility OHLC validity export, and strict row-field picking that ignores only expected missing-field errors.
- `app/services/akshare_provider.py`: AKShare quote/K-line/minute/stock-pool/plate/concept adapter, quote bridge fallback, K-line fetch-vs-schema error separation, normalized concept-candidate matching, concept-source failure escalation, and AKShare import sanitization.
- `app/services/akshare_mappers.py`: AKShare quote/minute/stock-pool row-to-model mapping, stock-code extraction, positive-price quote guards, optional numeric-field normalization, and malformed quote/minute-row filtering.
- `app/services/provider_stock_mappers.py`: shared StockInfo row mapping, stock-code validation, market extraction, and list-date normalization for optional providers.
- `app/services/tushare_provider.py`: optional Tushare daily K-line and stock-pool adapter with strict daily OHLC filtering; token is resolved by Settings from `ASHARE_RADAR_TUSHARE_TOKEN` with `TUSHARE_TOKEN` as a legacy alias and injected by the provider registry.
- `app/services/baostock_provider.py`: BaoStock historical K-line and stock-pool backup adapter with separated login/result-row mapping helpers and strict daily OHLC filtering.
- `app/services/futu_provider.py`: optional Futu quote, minute K-line, order-book, OpenD ping adapter, return-code checks, ordered snapshot validation, order-book depth cleaning, and empty-depth rejection.
- `app/services/futu_mappers.py`: Futu snapshot/minute row mapping, strict critical quote-field parsing, A-share code filtering, invalid minute OHLC-row filtering, and Futu symbol formatting.
- `app/services/local_metadata_provider.py`: local stock/plate/concept fallback metadata.
- `app/services/optional_providers.py`: backward-compatible re-export layer for optional provider classes.
- `app/workflows/individual.py`: backward-compatible workflow facade, staged non-blocking advice persistence, normalized-symbol local-state reads with fixed limits, component-level sanitized failure warnings, response assembly, and stock endpoint field accessors.
- `app/workflows/stock_lookup.py`: stock-code confirmation, strict quote-confirmation fallback when the stock pool is unavailable or misses a code, and industry-to-plate matching.
- `app/workflows/optional_data.py`: shared short timeout wrapper for optional workflow enrichment that must degrade instead of blocking core workbench loads.
- `app/workflows/stock_analysis.py`: quote/K-line/profile collection, base analysis assembly with source-aware peer-sample status plus optional plate/history/advice degradation, review, and minute analysis with canonical interval normalization, stock confirmation before source degradation, and best-effort fallback logging.
- `app/services/review.py`: individual review window filtering, review metrics, event rules, key points, and summary wording.
- `app/services/minute_analysis.py`: minute-line analysis using strict shared minute K-line sanity filtering, finite non-negative volume/amount/turnover gates, unavailable-reason mapping, analysis-context assembly, trend/momentum/volume-pulse rules, filtered support/resistance candidates, warning priority, conservative no-price/no-range/insufficient-sample fallbacks, and validated T-plan decisions/zones.
- `app/workflows/workbench_pipeline.py`: full workbench research pipeline plus optional market-breadth, order-book, and concept enrichment; breadth failures retain conservative degradation context.
- `app/workflows/market_overview.py`: market overview and strong-stock workflows with requested/success/missing status, partial/all-failure warnings, explicit invalid/oversized custom-symbol rejection, custom-list all-quote-failure errors, and visible K-line failure exclusion from ranking.
- `app/services/market_sampling.py`: structured breadth/peer quote sampling, seed exclusion, stock-pool consistency filtering, market/industry quotas, ordered partial results, explicit missing counts, batch-to-single fallback accounting, cancellation propagation, and source-degradation warnings.
- `app/services/research.py`: compatibility facade that re-exports research report builders.
- `app/services/research_alpha_points.py`: Alpha evidence point adapters and impact weights for trend, overview, rules, events, factors, regime, timeframe, and risk/reward.
- `app/services/research_alpha.py`: Alpha evidence report assembly, positive/negative evidence buckets that drop non-finite or non-displayable points before strength sorting, non-positive limit guards, cleaned title/reason de-duplication before caps, bounded 0-100 confidence components, finite verdict-context fallbacks, cleaned missing-data/data-quality/summary text, explicit missing-data merge for factor/regime/timeframe/risk-reward gaps and abnormal feature fields, and ordered verdict rules.
- `app/services/research_breadth.py`: market breadth filtering, score bands, genuine-empty versus source-failure semantics, conservative partial-source risk credit, summaries, and bounded warnings.
- `app/services/research_features.py`: feature snapshot and leadership report assembly with unified cleaning for prices, amount, turnover, volume ratio, ATR/volatility, MA/support/resistance, trend/signal/data-quality/fund/valuation/financial scores, sanitized leadership inputs, stable concept-evidence sorting, and explicit company-profile missing-data notes.
- `app/services/research_chip.py`: chip distribution approximation, finite positive price/volume K-line filtering, feature-price fallback to the latest valid close, guarded bucket construction, current-zone support/pressure bands, and concentration labels.
- `app/services/research_diagnosis.py`: final diagnosis assembly and backward-compatible helper aliases.
- `app/services/research_diagnosis_decisions.py`: diagnosis headline, final action downgrade rules, and confidence adjustment.
- `app/services/research_diagnosis_sections.py`: confirmation signals, hard risks, watch-focus de-duplication, missing-price guards, and summary wording.
- `app/services/research_evidence.py`: evidence-chain support, opposition, confirmation, and invalidation lists.
- `app/services/research_events.py`: event digest and positive/negative/watch event buckets.
- `app/services/research_factors.py`: factor lab assembly and backward-compatible helper exports.
- `app/services/research_factor_current.py`: current factor list construction and valuation-anchor factor assembly.
- `app/services/research_factor_report.py`: factor lab aggregate metrics, top positive/negative factors, notes, and report assembly.
- `app/services/research_factor_scoring.py`: score normalization, named volume-confirmation rules, risk-pressure contribution rules, chip fallback/distance rules, calibration quality, and weighted total scoring.
- `app/services/research_factor_weights.py`: ordered stock-profile factor weight policy and data-quality overlays.
- `app/services/research_factor_text.py`: factor impact, historical-reference wording, alpha/diagnosis helper text.
- `app/services/research_factor_specs.py`: immutable grouped factor specifications, strict duplicate/blank/whitespace ID validation, isolated factor-spec maps, read-only registered snapshots, historical proxy-score contexts, complete-window score-context helpers, rule-table scoring, finite/clamped trigger matching, and rolling helper metrics.
- `app/services/research_factor_calibration.py`: historical factor calibration, sample statistics, percentile calculation, scenario bucket stats, and bucket-note rules.
- `app/services/research_peer.py`: peer availability interpretation, valid partial-sample filtering, relative strength, valuation labels, leader sorting, and source-aware risk notes.
- `app/services/research_qa.py`: backward-compatible stock Q&A facade.
- `app/services/research_qa_answer.py`: free-form stock-question answering, unified topic-answer strategy registry including default comprehensive answers, whitespace-normalized topic lookup, confidence penalty rules, shared cleaned/deduped evidence/actions/invalidations, empty-output fallbacks, invalid-literal and finite-number wording guards, conclusion, and answer text.
- `app/services/research_qa_report.py`: fixed FAQ-style stock Q&A report generation with shared report-item sanitation, risk/reward level checks, support/resistance side checks, T-plan fallbacks, and theme-concept evidence cleaning so missing, wrong-side, blank, or non-finite inputs render as pending instead of `0.00`, `nan`, or `inf`.
- `app/services/research_qa_utils.py`: shared Q&A text cleaning, invalid-literal filtering, case-insensitive de-duplication, and bounded clean-item helpers.
- `app/services/research_qa_topics.py`: question topic routing and related-question suggestions.
- `app/services/research_regime.py`: market regime labels, stock state, cleaned regime context metrics, ordered environment rules, named risk-adjustment components, factor-risk adjustment rules, finite risk multipliers, sanitized industry/factor/breadth evidence, and regime suggestions with breadth-summary fallback text.
- `app/services/research_replay.py`: historical signal replay, replay-pattern context/window helpers, finite price/volume/forward-return filtering, invalid target-day pending handling, replay-pattern rule tables, note templates, and bounded finite-return mature-sample statistics.
- `app/services/research_risk.py`: risk radar item rules, scoring, top-risk extraction, and overall risk level.
- `app/services/research_risk_reward.py`: staged finite-metric target/stop assembly, non-finite/non-positive input cleaning, capped upside-target inputs, downside-stop candidate filtering/bounds, side validation before distance/ratio math, finite reward/risk ratio, ordered rating rules, summaries that mark missing or wrong-side price/target/defense/ATR/volatility/risk multipliers as pending, integer-normalized scenario probabilities with neutral floor, and scenario plans that downweight active paths when key levels are missing, reversed around current price, or rating/validation/timeframe state is cautious.
- `app/services/research_theme.py`: industry/concept theme context, finite concept/industry input cleaning, relative strength, ordered evidence rules, opportunities, risks, missing-data notes, and report-list caps that do not truncate scoring inputs.
- `app/services/research_timeframe.py`: short/swing/mid-term timeframe alignment, score components, conflict rule table, and invalid-price fallback.
- `app/services/research_t_strategy.py`: intraday T strategy style/suitability rule tables, low/high zones, missing-price wording guards, and stop conditions.
- `app/services/research_validation.py`: signal validation loop, centralized status/timeframe constants, finite fallbacks for risk/confidence/factor inputs, strict-priority status/confidence rule tables, trigger/confirmation/invalidation text, T-range open-interval wording, and historical reference.
- `app/services/stock_insights.py`: compatibility facade and insight-bundle assembly.
- `app/services/stock_activity.py`: rule-table fund-flow heat estimate, quote/K-line current-volume policy, price-volume relation, positive-amount/turnover guardrails, order-book pressure rules, shared valid-depth cleaning, invalid-depth and crossed-spread filtering, zero-baseline volume guards, and range-pressure fallback.
- `app/services/stock_abnormal_events.py`: abnormal-event assembly facade.
- `app/services/stock_abnormal_context.py`: abnormal-event input metrics derived from quote/K-lines, including previous-close fallback, quote/K-line current-volume policy, volume ratio, amplitude, and shadow percentages.
- `app/services/stock_abnormal_rules.py`: local abnormal-event detectors and event wording.
- `app/services/stock_abnormal_summary.py`: abnormal-event scoring, level, and main-signal selection.
- `app/services/stock_lhb.py`: LHB candidate signal rules, scoring, verification actions, and missing-seat checklist.
- `app/services/stock_event_sources.py`: stock event source adapters, reliability labels, external-checklist rule table, and aligned next-step generation.
- `app/services/stock_event_summary.py`: stock event panel aggregation and response assembly.
- `app/services/chart_marks.py`: note/event chart-mark assembly, date-alignment guards, text/price sanitation, invisible malformed-date marks, visible-limit categories, and event-to-mark conversion.
- `app/services/stock_events.py`: backward-compatible event re-export layer.
- `app/services/stock_overview.py`: overview score helpers, factor cards, positive valuation-field guards, finite/clean-text industry and market-cap handling, ordered main-conflict rules, consistently normalized key prices and risk triggers, visible-event de-duplication, and score caps.
- `app/services/stock_strategy.py`: strategy cards, data-quality status downshift maps, and signal fallback handling.
- `app/services/financial_metrics.py`: rule-table PE/PB, market-cap, liquidity, finite-number guards, and amount formatting.
- `app/services/financial_health_components.py`: financial-health metric cards, score state, missing-data policy, and liquidity wording.
- `app/services/financial_health.py`: financial-health response assembly.
- `app/services/valuation_anchors.py`: valuation price/PE/PB history anchors, safe numeric history filtering, stable latest-snapshot selection per day, bounded 0-100 percentiles, peer percentiles, and ordered anchor-label bands.
- `app/services/valuation_components.py`: valuation score components, bounded percentile delta rule tables, finite valuation wording, core-vs-enrichment missing-data policy, and watch points.
- `app/services/valuation_analysis.py`: valuation response assembly and compatibility exports.
- `app/services/stock_finance.py`: backward-compatible re-export layer for older imports.
- `app/services/stock_rules.py`: rule-spec definitions, match context assembly, rule-match metadata derived from specs, complete valid 20-high breakout windows, finite/positive checks for current price, MA20, support, volume ratio, fund score, and valuation score, rule state helpers, break-MA20/support-rebound/high-valuation risk gating, abnormal-risk evidence, confidence maps, anomaly-to-missing-data/evidence guards, stable sort keys, and cautious data-quality gate decisions.
- `app/services/scoring.py`: shared 0-100 score helpers.
- `static/app.js`: web workbench orchestration, separate load/render/companion failure states, invalid-search and delayed-redraw guards, local-data warning summaries, request-scoped mutations, duplicate-action guards, fresh-session SSE backoff, SSE validation, and UI event binding.
- `static/js/api.js`: fetch wrapper and readable API error detail normalization.
- `static/js/alerts.js`: alert rule CRUD, isolated manual evaluation results, scoped post-mutation reloads, duplicate-evaluation prevention, and alert event rendering.
- `static/js/chart.js`: canvas K-line drawing, frontend K-line sanity filtering before canvas math, moving-average lines, chart mark filtering/date matching, and mark rendering helpers.
- `static/js/diagnostics.js`: provider/cache/source-plan diagnostics, request-scoped scheduler/monitor polling, provider-detail helpers, scheduler status helpers, source-plan rendering helpers, task controls with failed-run error preservation, and monitor event rendering.
- `static/js/dom.js`: DOM helpers and HTML escaping.
- `static/js/errors.js`: compact user-facing error wording helpers.
- `static/js/format.js`: numeric formatting and neutral tone handling for missing or non-finite values.
- `static/js/notes.js`: stock note CRUD, scoped post-mutation reload/chart-mark refresh, and note-list rendering.
- `static/js/research-panels.js`: AI dashboard, Q&A submit flow with stale/duplicate guards, evidence chain, Alpha/timeframe/risk-reward helpers, peer-source warnings, factor/theme/chip/replay panels, and panel-level rendering isolation.
- `static/js/research-render-utils.js`: shared safe array/object/text coercion, escaped item, metric-pair, missing-data, signed-value, and threshold-tone helpers for research panels.
- `static/js/symbols.js`: UI symbol normalization plus malformed/all-zero search-input validation.
- `static/js/watchlist.js`: watchlist CRUD, response-shape guardrails, request-scoped load/mutation freshness, duplicate-submit prevention, stale-form and last-known-list preservation, and watchlist rendering.
- `static/js/workbench.js`: main stock/insight/quality/review panels, valuation/minute helpers, market and strong-stock degradation rendering, and quote-list rendering.
- `static/js/workbench-render-utils.js`: shared safe array/object coercion and escaped list/tag rendering helpers for workbench panels.
- `static/styles.css`: CSS entry manifest that imports the ordered style modules.
- `static/css/base.css`: design tokens, global reset, top bar, base layout, search controls, and shared hover behavior.
- `static/css/sidebar.css`: watchlist, data-source health, scheduler tasks, and monitor-event styles.
- `static/css/workspace-core.css`: market strip, workspace tabs, stock header, metrics, summary, quality, and core research panels.
- `static/css/research-panels.css`: AI Q&A dashboard, evidence chain, insight panels, theme/chip/replay/finance panels.
- `static/css/interactions.css`: rule cards, strategy cards, events, review/timeline, alerts, notes, chart marks, and minute-analysis styles.
- `static/css/side-footer.css`: right-side quote/leader lists, leader-scope wrapping, footer, empty states, and error states.
- `static/css/responsive.css`: responsive layout rules.
- `data/ashare_radar.sqlite3`: local cache and user data.

## Runtime Boundaries

- The app is single-user local software.
- SQLite is the local persistence layer.
- Public and optional data sources can fail, delay, or change fields. The UI must display data quality and degradation instead of silently pretending data is real-time.
- LLM output is explanatory only. Rule-based answers remain the fallback and grounding source; stock codes are only accepted as code references, not market-price numbers, and model/API errors are redacted before display.
- Runtime files under `data/` are local state and are ignored by Git; only `data/.gitkeep` belongs in source control. The supported local database is `data/ashare_radar.sqlite3`; old `app.db` and `smoke.sqlite3*` files are disposable development artifacts.

## License

MIT. See [LICENSE](LICENSE).
