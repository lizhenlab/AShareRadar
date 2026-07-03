# Test Plan and Test Report

## 1. Test Objectives

- Verify single-stock analysis behavior under realistic and degraded data.
- Verify provider fallback, capability health, cache reuse, and data-quality gates.
- Verify local lifecycle features: watchlist, alerts, notes, chart marks, advice history, monitor events.
- Verify LLM integration never blocks rule fallback.
- Verify frontend JavaScript remains syntactically valid.
- Verify the CSS entrypoint imports existing style modules in the expected order.

## 2. Test Commands

```bash
export PYTHON=${PYTHON:-/opt/anaconda3/bin/python3}
npm run check
$PYTHON tools/architecture_inventory.py
$PYTHON tools/api_inventory.py
```

Optional runtime smoke checks:

```bash
curl -sS http://127.0.0.1:8010/api/health
curl -sS 'http://127.0.0.1:8010/api/stocks?keyword=600519&limit=5'
curl -sS 'http://127.0.0.1:8010/api/stock/workbench?symbol=600519'
```

## 3. Current Automated Coverage

The current test suite is split by domain:

- `tests/test_rules_alerts.py`: rule definitions, factor calibration, alert cooldown/validation, future-trigger timestamp recovery, alert transition decisions, alert-condition evaluation, and chart marks.
- `tests/test_api_error_modules.py`: API validation-message rule order, Chinese validation text for parsing/type/range/unknown-field errors, fallback messages, joined error-location rendering, and SQLite database-error mapping for async/sync API helpers.
- `tests/test_data_sources.py`: provider priority, provider fallback, capability health, optional provider adapters, strict Eastmoney quote-field parsing, Eastmoney request dedupe, missing change-percent fallback, non-negative core quote fields, AKShare quote bridge priority, AKShare spot missing-code reporting, AKShare quote mapper guards, AKShare K-line import-failure fallback versus schema-error surfacing, AKShare concept-candidate/code-column matching, all-candidate concept-loader failure escalation, AKShare plate capability failure recording, AKShare minute-period mapping and OHLC row filtering, BaoStock/Tushare stock-pool row filtering, Eastmoney endpoint retry/order preservation/OHLC/K-line filtering, source plans, cache fallback, unregistered quote-priority skips, authoritative stock-pool cache, and incomplete-provider miss semantics.
- `tests/test_provider_utils_modules.py`: provider row-field picking, NaN skipping, expected missing-field fallback, unexpected adapter-error propagation, shared positive-limit validation, and finite/positive/bounded OHLC validity validation.
- `tests/test_datahub_cache_modules.py`: datahub cache-helper guardrails for explicit minute interval aliases, case/empty defaults, unsupported interval errors, stock-pool freshness windows, intraday current-day K-line acceptance, and future K-line date rejection.
- `tests/test_datahub_klines_modules.py`: K-line/minute K-line coordinator guardrails for empty/invalid provider responses, fallback-cache tagging, provider cooldown recording, missing minute capability skips with capability-failure recording, interval normalization, unregistered priority providers, fresh-cache request-limit coverage before provider skipping, cache read/write invalid-row filtering, parsed date/time latest-window ordering without backfilling older rows, huge provider-limit caps, malformed max-limit fallbacks, cancellation propagation, non-positive cache limits, positive provider-call limits, and future fetched-at rejection.
- `tests/test_datahub_metadata_modules.py`: metadata coordinator guardrails for empty plate responses, provider cooldown recording, unregistered priority providers, concept normalization after fallback, stock-pool provider skips, authoritative profile misses, local-only profile fallback prevention, non-mutating local profile enrichment, non-positive cache limits, stable metadata ordering, invalid metadata-row filtering, concept de-duplication, optional numeric-field cleaning, local-provider limit guards, and future updated-at rejection.
- `tests/test_provider_registry_modules.py`: provider priority normalization, unknown-kind handling, capability fallback metadata, mismatched capability-name normalization, and supported-kind mapping.
- `tests/test_datahub_source_plan_modules.py`: source-plan decision/action guardrails for statusless failures, unprobed capabilities, disabled providers, dirty/duplicate provider names and statuses, cooling priority, missing primary source warnings, unique non-demo provider counts, warning/suggestion de-duplication, and warning-rule order.
- `tests/test_datahub_status_modules.py`: provider source-key normalization, unknown-source fallback, primary-source selection, duplicate capability status ranking, unhealthy-label de-duplication, finite success-rate counts, recovery-action rule order, cleaned error text, error-type priority, provider-specific setup hints, and default fallback guidance.
- `tests/test_quote_stream_modules.py`: quote-route and SSE quote-stream guardrails for empty and oversized batch-symbol rejection, watchlist/seed fallback caps with dirty-symbol skips, explicit/watchlist/seed symbol selection, canonical symbols, refresh-interval lower bounds, JSON-safe event formatting, readable quote-error events, and disconnect stop behavior.
- `tests/test_api_alert_routes.py`: route-level and repository contract tests for alert-rule creation, blank-name defaulting, Chinese type-error rendering, finite threshold/cooldown guards, unknown-field update rejection, alert-rule read failure mapping, and alert-event read failure mapping.
- `tests/test_api_notes_routes.py`: route-level and repository contract tests for stock-note creation, blank trade-date fallback/clearing, trimmed content, non-positive/non-finite price rejection, trade-date normalization/rejection, stable same-day note ordering, note updates, and note-list read failure mapping.
- `tests/test_api_stock_routes.py`: route-level contract tests for stock endpoints, including malformed zero-symbol rejection before profile or minute data fetches.
- `tests/test_config_modules.py`: environment-backed settings guardrails for `ASHARE_RADAR_*` names, legacy aliases, new-name precedence, dynamic reads, invalid-value fallback, explicit booleans, and numeric lower bounds.
- `tests/test_llm_explainer.py`: LLM environment loading, fallback, grounded answer acceptance, stock-code reference boundaries, API-key redaction on failure, non-finite allowed-number filtering, ungrounded comma-adjacent number rejection, narrow list-marker exemptions, and ungrounded-number rejection.
- `tests/test_symbol_modules.py`: SH/SZ symbol normalization, provider symbol formatting, malformed-code rejection, all-zero symbol rejection, and conflicting market marker rejection.
- `tests/test_market_sampling_modules.py`: market-sampling seed normalization, seed exclusion from breadth samples, seed dedupe before stock-pool sampling, stock-pool symbol/code/market/industry metadata consistency filtering, invalid-literal industry skips, stock-pool failure logging, provider-sample normalization/skipped-value logging, request-symbol quote filtering, batch-missing-symbol fallback logs, single fallback wrong-symbol rejection, sample fill/dedupe, duplicate-safe even sampling, cancellation propagation, and peer-symbol filters.
- `tests/test_market_quotes_modules.py`: quote snapshot/history column mapping, trade-date normalization, cached snapshot/history persistence, invalid quote write filtering, dirty cache read filtering, non-positive cache windows, and future fetched-at rejection.
- `tests/test_market_overview_modules.py`: market overview index quote degradation, strong-stock custom-symbol quote fallback, explicit all-invalid custom-symbol rejection, and custom-symbol list size guardrails.
- `tests/test_minute_analysis_modules.py`: minute unavailable-reason priority, trend-label rules, momentum-rule order, volume-pulse windows, warning-rule order, support/resistance level ordering and candidate filtering, shared minute K-line sanity filtering for malformed OHLC/non-finite values/negative volume/amount/turnover, T-plan defensive/range/waiting decisions, zone legality, confidence downgrades, conservative no-price/no-range/insufficient-sample text, interval fallback, and recent-extreme fallbacks.
- `tests/test_leader_scoring_modules.py`: shared leader-score profile formulas and context-specific tag thresholds.
- `tests/test_research_leadership_modules.py`: feature snapshot sanitation, leadership-report feature cleaning, concept evidence filtering and stable ordering, company-profile missing-data notes, score-summary thresholds, data-quality downgrades, and tag limits.
- `tests/test_analysis_research.py`: minute analysis, workflow interval normalization, stock-confirmation before minute fetches, indicators, data quality, strategy/rule gates, research reports, market sampling, valuation history, market-regime fallbacks, and alert quality gates.
- `tests/test_data_quality_modules.py`: focused data-quality score-component guardrails, quote-field rule order and critical-value sanitation, stricter high/low/change_pct boundaries, cache-source wording, future quote/K-line timestamp penalties, intraday current-day K-line handling, latest parsable K-line date/source selection under unsorted or malformed tails, K-line level/penalty rule order, missing-K-line de-duplication, terminal K-line penalties, field-mismatch boundaries, and consistency penalty/anomaly guardrails.
- `tests/test_indicator_levels_modules.py`: support/resistance valid-row filtering, invalid-close fallback, realtime breakdown adjustment, and level-order guardrails.
- `tests/test_indicator_trend_modules.py`: focused trend-score contribution order, moving-average rule order, volume-confirmation rule order, and threshold guardrails.
- `tests/test_indicator_volume_modules.py`: shared positive-volume ratio, zero/invalid window fallbacks, recent-volume sample guards, and positive average-volume filtering.
- `tests/test_analysis_signal_modules.py`: analysis signal facade compatibility, finite-value guardrails, sanitized signal snapshot scores/labels/quality/contributions/notes, direct summary-helper sanitation, malformed confidence fallbacks, risk-level priority, ordered buy/sell point rules, strength-tag rules, T-plan invalid-zone pending wording, low-quality gate rules, action-advice decision boundaries, and module-boundary guardrails.
- `tests/test_research_factor_modules.py`: factor lab facade compatibility and current/report module-boundary guardrails.
- `tests/test_research_factor_scoring_modules.py`: volume-confirmation rule priority/boundaries, risk-pressure contribution rules, chip-position factor fallback priority, distance-to-cost-center boundaries, and concentration adjustment guardrails.
- `tests/test_research_factor_specs_modules.py`: immutable factor registration order, read-only registered snapshots, factor-spec registration validation, duplicate/blank/whitespace-ID validation, isolated factor-spec maps, historical proxy-score rule components, complete score-context boundary/window helpers, malformed/non-finite K-line fallbacks, zero-volume handling, chip cost-center gates, finite/clamped trigger-score boundaries, and rolling helper boundaries.
- `tests/test_research_factor_weight_modules.py`: factor weight profile priority, low-quality overlays, default notes, and final weight clamps.
- `tests/test_research_factor_calibration_modules.py`: factor calibration sample gates, invalid entry/forward-row skips, no-match fallback, forward-return statistics, confidence/expected-level rules, scenario bucket stats, and bucket-note priority.
- `tests/test_research_peer_modules.py`: peer sample filtering, relative-strength percentile, amount metrics, leader sorting, valuation/strength risks, and no-sample fallback.
- `tests/test_research_regime_modules.py`: market-regime state priority, cleaned context metrics, ordered market-label rules, named risk-adjustment components, non-finite adjustment guards, factor-risk adjustment contributions, blank industry/factor/breadth-summary filtering, breadth-summary fallback text, missing support/resistance guardrails, finite evidence/suggestions, and market-breadth boundaries.
- `tests/test_research_theme_modules.py`: theme-context industry fallback, finite industry/stock/concept input cleaning, blank/non-finite concept filtering, concept dedupe, report concept caps after full-input scoring, ordered evidence slots, and hot-theme/weak-stock risk guardrails.
- `tests/test_research_event_digest_modules.py`: event-digest risk/positive/watch buckets, default watch fallback, and missing-data de-duplication.
- `tests/test_research_replay_modules.py`: replay minimum-sample gates, invalid entry/window filtering, replay context boundary and volume-window helpers, recent pending-signal handling, invalid target-day pending handling, finite price/volume/forward-return filtering, mature-sample success rates, bounded completed-sample counts, missing-return statistics, and replay outcome/pattern notes.
- `tests/test_research_risk_modules.py`: risk-radar item rule order, score boundaries, top-risk sorting, and overall-level guardrails.
- `tests/test_research_alpha_modules.py`: Alpha evidence point source, positive/negative bucket non-finite impact and non-displayable text filtering, strength sorting, non-positive limit guards, cleaned title/reason de-duplication before limits, dirty point/impact-string tolerance, missing-data de-duplication/limits with invalid-literal/`N/A` filtering, whitespace normalization, empty rule-match tolerance, explicit absent factor/regime/timeframe/risk-reward and abnormal feature fields, data-quality note cleaning, factor-lab point filtering/calibration, impact direction, bounded finite-value confidence adjustment and summary display, verdict-context fallbacks, and verdict-priority guardrails.
- `tests/test_research_breadth_modules.py`: market-breadth invalid-sample filtering, empty-state fallback, score component formula, and label-band boundaries.
- `tests/test_research_chip_modules.py`: chip-analysis invalid-row filtering, finite positive price/volume gates, valid-sample gates, flat/upper-bound bucket building, current-zone support/pressure bands, nearest support/pressure ordering, and feature-price fallback to the latest valid close.
- `tests/test_research_diagnosis_modules.py`: diagnosis action downgrades, headline/action rule priority, missing key-price text, watch-focus de-duplication, main-conflict sentence normalization, and diagnosis-section guardrails.
- `tests/test_review_modules.py`: individual-review period gates, malformed K-line filtering, review-window metrics, event priority, and latest-event limits.
- `tests/test_research_timeframe_modules.py`: timeframe trend score components, invalid-price fallback, non-positive window handling, insufficient-sample fallback, same-direction conflict boundaries, and alignment-label boundaries.
- `tests/test_research_risk_reward_modules.py`: risk/reward rating priority for timeframe conflicts, external risk gates, attractive-ratio requirements, mixed-timeframe wait boundaries, finite/non-positive metric sanitation, finite reward/risk ratios, capped upside targets, stale structural-stop filtering, upside-target/downside-stop side validation before distance math, downside-stop bounds/adjustments, pending summary wording for missing or wrong-side price/target/defense/ATR/volatility/risk multipliers, integer scenario-probability normalization/sanitization with neutral floor, decision-state probability caps, missing/wrong-side-level active-path downweighting, missing-price wording guards, blank/non-finite action/status/timeframe fallbacks, and default boundaries.
- `tests/test_research_validation_modules.py`: signal-validation item, non-finite risk/confidence/factor fallback handling, confidence penalties, T-range strict open-interval wording, timeframe-note boundaries, summary confirmed/defensive grouping, and overall-status priority for environment, timeframe, weak factors, reverse risk, and confirmation counts.
- `tests/test_research_t_strategy_modules.py`: T-strategy style/suitability rule priority, active-T blocking, tradable-range gates, validation-risk gates, zone text, missing-price fallbacks, and price-buffer behavior when resistance is unavailable.
- `tests/test_research_qa_answer_modules.py`: free-form Q&A topic strategy registry coverage, comprehensive/default-topic mapping, whitespace-normalized topic lookup, unregistered-topic default fallback, answer-action display limits, cleaned/deduped list limits, empty-output fallback, confidence penalty boundaries, conservative theme fallback, invalid-literal/non-finite wording guards, and action de-duplication.
- `tests/test_research_qa_report_modules.py`: fixed FAQ Q&A guardrails for risk/reward target/stop evidence, report-item exit sanitation, support/resistance invalid or wrong-side levels, T-plan fallback evidence, and theme-concept invalid-literal/non-finite cleaning.
- `tests/test_local_lifecycle.py`: local persistence lifecycle, alert-rule name defaulting, guarded SQLite compatibility migrations, stock-note content/price/date cleaning including non-finite direct-call guards, malformed legacy note-date handling, dirty legacy advice/alert row sanitation, unsupported legacy alert disabling, non-negative trigger-count updates, non-positive user/runtime list limits, concept/theme/event context, workbench cache, advice history, and replay confidence.
- `tests/test_individual_workflow_modules.py`: `stock_workbench` stage boundaries, advice snapshot persistence, local-state symbol normalization, fixed chart-mark/alert-rule/alert-event/note read limits, and response assembly guardrails.
- `tests/test_workbench_context_cache_modules.py`: focused workbench-context cache guardrails for expired entries, cancelled in-flight tasks, clear-during-build behavior, concurrent request sharing, and DataHub instance-owned cache isolation.
- `tests/test_api_container_modules.py`: application-container object sharing guardrails for DataHub-owned workbench context cache.
- `tests/test_workbench_pipeline_modules.py`: focused workbench pipeline guardrails for order-book degradation and readable fallback errors.
- `tests/test_db_mappers.py`: DB mapper compatibility facade guardrails.
- `tests/test_provider_status_aggregation_modules.py`: provider/capability status aggregation guardrails for config-only rows, disabled-capability stale activity, active health, invalid count normalization, deterministic tie-break ordering, repository ordering, and unprobed providers.
- `tests/test_tencent_provider_modules.py`: Tencent quote URL construction, quote text parsing, empty-response errors, payload extraction with trailing whitespace and unclosed-payload rejection, malformed K-line payload-shape fallback, stripped field/minimum-count validation, field fallback, required code/name validation, real timestamp validation, open/current price containment inside high/low, missing change-percent fallback, finite non-negative core volume/amount guards, index market flags, strict critical quote-field parsing, malformed quote/K-line row rejection, and demo-provider random-state isolation plus rounded quote/K-line containment and Monday small-limit backfill.
- `tests/test_futu_provider_modules.py`: Futu empty quote requests, ordered snapshot missing-code reporting after invalid critical price filtering, minute-row OHLC filtering, interval mapping, order-book depth cleaning, and empty-depth rejection.
- `tests/test_datahub_orderbook_modules.py`: order-book coordinator timeout wrapping, failure recording, and cooldown.
- `tests/test_optional_kline_parsing_modules.py`: Tushare and BaoStock daily K-line OHLC row filtering.
- `tests/test_static_assets.py`: static frontend CSS entrypoint, imported CSS module guardrails, JS function-size guardrails, UI symbol all-zero rejection, Node-based research-panel render/Q&A-submit/market/factor/Alpha/timeframe/risk-reward smoke coverage, escaped workbench review/evidence renderer smoke coverage, diagnostics-panel smoke coverage, and fake-canvas chart mark plus dirty-K-line filtering smoke coverage.
- `tests/test_frontend_app_flow.py`: Node/fake-DOM app-flow guardrails for symbol input synchronization, stale advice-timeline responses, SSE frame parse errors, and stream symbol selection.
- `tests/test_frontend_research_panels.py`: Node/fake-DOM AI question guardrails for stale request suppression, same-form latest-request priority, current-error rendering, and escaped research render-helper boundaries.
- `tests/test_frontend_api_format_workbench.py`: Node/fake-DOM frontend guardrails for readable API detail messages, finite-number formatting, neutral missing-number classes, partial workbench rendering, and escaped valuation/leader output.
- `tests/test_chart_marks_modules.py`: chart-mark categories after visible limiting, regular-event filtering before caps, malformed-date alignment/visibility guards, note/event text and price sanitation, and internal negative-limit guards.
- `tests/test_stock_abnormal_events.py`, `tests/test_stock_event_summary.py`, `tests/test_financial_metrics_modules.py`, `tests/test_financial_health_modules.py`, `tests/test_valuation_modules.py`: focused insight-module guardrails for abnormal events, quote-vs-K-line current-volume context metrics/fallbacks, event summaries, external-checklist rule order, financial metric interpretation, financial health, valuation scoring, percentile delta rule order, out-of-range percentile ignoring, latest daily valuation-history snapshot selection, finite valuation wording, valuation-anchor bands, and malformed valuation-history filtering.
- `tests/test_stock_lhb_modules.py`: LHB candidate default state, move/turnover triggers, strong-move scoring bonus, weak-trend action, and abnormal-event reason limits.
- `tests/test_stock_overview_modules.py`: overview fundamental-factor scoring, non-positive/non-finite valuation and market-cap guards, clean text guards, missing-field evidence, high/low valuation boundaries, ordered main-conflict rules, usable-fund divergence gates, shared normalized key-price/takeaway/risk-trigger handling, visible-event de-duplication, factor order, score caps, and low-quality conflict prefixes.
- `tests/test_stock_strategy_modules.py`: strategy-card data-quality status downshift maps and signal-level downgrade boundaries.
- `tests/test_stock_rule_modules.py`: focused stock-rule guardrails for rule-spec definition/config/raw order, rule-id uniqueness, confidence map alignment, spec-derived match metadata, volume-breakout status/confidence/missing-data, complete valid 20-day high windows, current quote-volume handling, finite/positive current-price/MA20/support/volume-ratio/fund-score/valuation-score gates, break-MA20 boundaries, support-rebound risk downgrades, missing support levels, fund/technology-divergence risk/observation boundaries, high-valuation chase boundaries without invalid valuation-score triggers, abnormal-risk evidence, data-quality gate decisions, and stable same-rank sorting.
- `tests/test_stock_activity_modules.py`: focused fund-flow/order-pressure guardrails for negative amount availability, price-volume relation rules, current quote-volume handling, invalid quote-volume fallback, real-time order-book pressure, threshold-boundary neutrality, shared invalid-depth filtering, crossed-spread protection, zero-depth ratios, zero-volume baselines, invalid turnover handling, fallback range pressure, and data-quality downgrades.
- `tests/test_system_diagnostics_modules.py`: focused monitoring-diagnostics guardrails for cache freshness, failed provider capabilities, provider-diagnostic priority/caps, quote-source redundancy, scheduler state, sanitized storage/table counts and dirty keys, de-duplicated warnings/suggestions, demo source warnings, and trading-calendar fallback.
- `tests/test_api_data_routes.py`: route-level contract tests for trading-calendar refresh success, explicit refresh-error payloads, data-status SQLite failure mapping, and Futu status runtime-failure mapping.
- `tests/test_api_watchlist_routes.py`: route-level watchlist and advice-history read failure mapping for local SQLite errors.
- `tests/test_api_monitoring_routes.py`: route-level monitoring failure contracts for scheduler status, task runs, monitor events, system diagnostics, and manual task failures.
- `tests/test_scheduler_modules.py`: focused scheduler task-definition/state guardrails, deterministic task ordering, DataHub settings ownership, positive-integer interval/limit/freshness setting sanitation, refresh symbol cleaning/de-duplication, no-valid-symbol skips, manual/background failure semantics, per-symbol K-line refresh degradation, data-health provider failure priority, missing/stale cache events, healthy state, reschedule boundaries, and finite cleanup summaries.
- `tests/test_tool_inventory_modules.py`: documentation generator guardrails for function-inventory tooling coverage, business-API reference scope, and duplicate model-field detection.
- `tests/factories.py`: shared Quote/K-line/plate/stock fixtures.

Covered areas:

- Rule definitions are versioned and parameterized.
- API and UI validation errors keep Chinese message mapping stable for bounds, length, parsing, boolean, missing-field, malformed all-zero/conflicting-market symbols, and fallback cases.
- API read endpoints that touch local state map SQLite/database failures to stable Chinese 503 `detail` responses instead of leaking raw 500 errors.
- Manual scheduler task failures record failed task history and return an API failure instead of `ok: true`; background scheduler failures remain recorded without stopping the scheduler loop.
- Alert cooldown, recovery, validation, condition evaluation dispatch, dynamic support/resistance thresholds, create/update name defaulting, update lifecycle, and quality gates.
- Chart mark visibility and K-line date contract.
- Provider priority, priority deduplication, unknown-kind guardrails, capability fallback metadata, partial quote merge, market-sample diversity, sample normalization/dedupe logging, stock-pool failure logging, market overview quote degradation, strong-stock K-line evidence gating, capability health, cooldown, and unregistered-provider skip behavior.
- Main analysis degrades optional plate-rank failures into missing industry context while preserving quote/K-line analysis; custom strong-stock lists have explicit size limits before provider calls.
- AKShare/Eastmoney quote, daily K-line, minute K-line, and concept fallback parsing, including endpoint retry, empty-request short-circuiting, deduped fetch with requested-order preservation, quote bridge priority, strict critical Eastmoney quote parsing, missing change-percent fallback, non-negative core quote fields, spot-row missing-code reporting, malformed quote rows, invalid short/zero codes, AKShare import-failure K-line fallback, AKShare returned-row schema error surfacing, all-candidate concept-loader failure escalation, empty/invalid provider-response downgrade, fallback-cache tagging, OHLC/cache filtering, minute-period mapping, minute-row invalid time/OHLC/amount/turnover filtering, concept-candidate filtering, and placeholder numeric fields that must not be mapped to fake or misleading quotes.
- Tencent quote/K-line URL construction, parsed-empty error handling, payload parsing with trailing whitespace, malformed K-line payload-shape fallback, backup high/low fields, required code/name validation, real timestamp validation, open/current price containment inside high/low, index market flags, optional market-cap handling, strict critical price-field parsing, missing change-percent fallback, finite non-negative core volume/amount guards, malformed payload/K-line rejection, and demo random-state isolation with contained open/price values.
- Futu quote/minute/order-book parsing preserves request order, reports missing A-share snapshots after filtering non-A-share rows and invalid critical prices, skips malformed minute OHLC rows, skips malformed depth prices while preserving zero-volume levels, rejects fully empty order-book depth, wraps order-book timeouts as data-source failures, and rejects unsupported minute intervals.
- Data source status and provider source plan.
- Provider source-key normalization keeps known source aliases stable and preserves unknown source prefixes.
- Provider recovery actions keep network/proxy, remote-disconnect, timeout, provider-specific, and default suggestions stable.
- Source-plan primary selection and decision actions distinguish active failed capabilities, unprobed enabled capabilities, cooling providers, disabled providers, and missing aggregate provider rows.
- Quote routes reject empty or oversized unique batch-symbol requests before provider calls, dedupe normalized duplicate symbols, while SSE quote streaming keeps symbol fallback caps, dirty fallback-symbol skips, canonical symbol labels, refresh-interval lower bounds, JSON-safe data events, readable quote-error events, and disconnect stop behavior stable at the helper level.
- Stock-note creation and updates reject blank content, non-positive manual prices, invalid dates, and unknown fields; trim stored content; normalize supported date formats; and fall back to quote timestamps when create requests send blank trade dates.
- Repository list/cache reads return empty results for non-positive limits and reject future cache timestamps instead of allowing SQLite negative-limit full-table reads or long-lived future rows.
- Warmup behavior, authoritative stock-pool cache misses, incomplete stock-pool provider misses, unregistered stock/plate/concept priority skips, AKShare plate capability failure recording, empty metadata-response downgrade, non-mutating profile enrichment, local profile enrichment without overriding authoritative misses, local smoke-symbol fallback, and stale local stock-master matches.
- Settings read environment variables at instantiation time and fall back safely when numeric or boolean values are malformed.
- LLM environment variable parsing, fallback, grounded answer acceptance, stock-code reference boundaries, API-key redaction on failure, non-finite allowed-number filtering, ungrounded comma-adjacent number rejection, narrow list-marker exemptions, and ungrounded-number rejection.
- Minute analysis T-plan, stock existence confirmation before source degradation, unavailable state, unavailable-reason mapping, OHLC consistency filtering, finite non-negative volume/amount/turnover filtering, support/resistance candidate filtering, legal T-plan zones, trend/momentum/volume rules, warning priority, conservative no-price/no-range/insufficient-sample wording, confidence downgrades, decision priority, interval fallback, and support/resistance fallbacks.
- Shared leader-score profiles preserve feature-snapshot and strong-stock ranking formulas while keeping tag thresholds context-specific.
- Leadership reports clean feature inputs, drop concept evidence with blank names or non-finite changes, sort concepts stably by heat/rank/input order, keep company-profile missing-data notes explicit, keep score-summary thresholds stable, and downgrade low-quality data.
- Indicator support/resistance, volume ratio, trend contribution breakdown, contribution order, and trend threshold direction.
- Support/resistance filters inverted high/low rows, uses the last valid close when realtime price is absent, adjusts to valid recent highs/lows on breakout or breakdown, and preserves support below resistance.
- Analysis signal module compatibility exports, finite-value guards, ordered risk-level priority, ordered buy/sell point rules, strength-tag rules, action advice quality gates, strong-trend attention, risk-priority control, hold observation, and wait-state boundaries.
- Data quality freshness, intraday stale quote penalties, critical quote-field sanitation before derived diagnostics, high/low/change_pct boundary checks, future quote/K-line timestamp penalties, intraday current-day K-line notes, midday snapshots, short-cache/normal-cache/fallback-cache wording, stale/fallback/demo K-line level and penalty rule tables, terminal missing/invalid K-line penalties without duplicate missing-K-line notes, after-hours quote handling, quote-only checks, ordered quote-field sanity rules, non-negative consistency penalties, and consistency anomaly propagation.
- Research report quality sharing, market breadth invalid-sample filtering and label boundaries, valuation, theme context, and event-digest bucket priority.
- Free-form stock Q&A has complete strategy outputs for all routed topics, falls back conservatively for unregistered topics, applies confidence penalties predictably, caps theme missing-data penalties, and de-duplicates repeated risk actions.
- Fixed FAQ stock Q&A preserves risk/reward pending-level wording, normalizes dirty report items at the builder boundary, and does not turn missing or wrong-side target/stop/support/resistance levels or theme/T-plan evidence into `0.00`, `nan`, or `inf` output.
- Market-regime state priority, cleaned context metrics, ordered market-label rules, named risk multiplier adjustment scaling, non-finite adjustment guards, blank industry/factor/breadth-summary filtering, breadth-summary fallback wording, finite evidence/suggestions, and missing support/resistance guardrails.
- Peer comparison keeps invalid quotes out of the sample, calculates relative-strength position, sorts peer leaders, and reports valuation/strength risks explicitly.
- Factor scoring preserves chip-position fallback priority, cost-center distance bands, and concentration score adjustment.
- Factor specifications keep registration immutable, validated, and duplicate-checked, preserve historical trend, volume, risk, chip, and leadership proxy scoring as rule-driven logic, and make malformed/non-finite K-lines or missing volume return neutral fallbacks instead of fake strong signals.
- Factor weight policy preserves profile priority, low-quality risk overlays, and final weight min/max clamps.
- Factor calibration separates sample collection, statistics, expected-level rules, confidence labels, scene bucket summaries, and bucket-note priority.
- Chip analysis filters invalid/non-finite/non-positive price or volume K-lines, requires enough valid rows, keeps bucket volume stable across flat ranges and upper-bound prices, includes the current price bucket in nearby support/pressure bands, orders bands by proximity before volume, and falls back to the latest valid close when feature price is invalid.
- Theme-context industry-name fallback, concept dedupe before scoring, report caps after full-input scoring, ordered evidence slots, and hot-theme/weak-stock risk flags.
- Replay minimum K-line gates, invalid entry/window filtering, recent pending-signal handling, malformed K-line rejection, invalid target-day pending handling, finite-return success-rate/average-return statistics, bounded completed-sample counts, missing-return win-rate statistics, and pending-outcome notes.
- Risk radar preserves item ordering, risk score formulas, top-risk sorting, timeframe conflict boundaries, and overall-level thresholds.
- Alpha evidence source collection, positive/negative bucket non-finite impact filtering, strength ordering, de-duplication before limits, missing-data de-duplication/limits with explicit absent upstream inputs and abnormal feature fields, impact direction, bounded finite-value confidence adjustment penalties/bonuses, verdict-context fallbacks, and verdict priority.
- Timeframe scoring preserves moving-average, return, drawdown, invalid-price fallback, short-term trend-blend boundaries, same-direction conflict classification, and alignment labels.
- Risk/reward rating keeps timeframe conflict ahead of external risk priority, requires factor/validation/breadth confirmation for attractive ratios, preserves wait/default boundaries, cleans non-finite/non-positive inputs before ratio and summary assembly, caps stale/abnormal upside and structural-stop levels, keeps scenario probabilities normalized to 100 after input sanitization with a neutral-path floor, caps active positive paths when rating/validation/timeframe state is cautious or when price/support/resistance/target/defense levels are missing, and avoids misleading zero-price scenario wording when levels are missing.
- Signal validation keeps environment/timeframe suppression ahead of weaker confirmations, preserves reverse-risk triggers, centralizes status/timeframe constants, sanitizes non-finite risk/confidence/factor inputs, treats doing-T ranges as strict open intervals, and rolls confirmed/defensive items into stable summaries and overall statuses.
- Diagnosis data-quality gates, headline/action rule priority, timeframe-conflict downgrade rules, contextual confirmation/risk sections, missing-price guards, watch-focus de-duplication, and main-conflict sentence normalization.
- Individual review filters malformed K-lines, treats non-positive periods as insufficient, preserves review-window metrics, and keeps review-event priority/limit stable.
- Focused valuation-module behavior for missing fields, percentile score direction, valuation-anchor band priority, price-position fallback, and malformed valuation-history filtering.
- Focused financial-metric and financial-health behavior for non-finite values, non-positive market cap, liquidity amount/turnover rules, missing core quote fields, and metric-card ordering.
- Overview factor cards keep fundamental-field evidence, missing fields, non-positive valuation handling, valuation boundaries, ordered main-conflict priority, and low-quality conflict messaging stable.
- Strategy-card checks keep severe/weak data-quality status downshifts and signal-level downgrades stable.
- LHB candidate checks keep pre-listing reasons, verification actions, strong-move scoring, and weak-trend caveats explicit.
- Stock event source rules keep external placeholder events and next-step actions aligned for LHB candidates, announcement checks, high-risk states, and margin-financing checks.
- Stock rule checks keep rule definitions and match metadata aligned, rule confidence maps synchronized, volume-breakout trigger state, complete valid 20-high breakout windows, current quote-volume detection, finite/positive current-price/MA20/support/volume-ratio/fund-score/valuation-score gates, break-MA20 status boundaries, confidence, missing volume/high/price notes, anomaly evidence, support-rebound risk downgrades, missing support levels, fund/technology divergence direction, high-valuation chase thresholds that ignore invalid valuation scores, sell-pressure risk escalation, abnormal-risk evidence, cautious quality-gate downshifts, and stable sort tie-breaks explicit.
- Focused fund-flow and order-pressure behavior for positive amount availability, price-volume relation rules, current quote-volume detection, invalid quote-volume fallback, real-time bid/ask ratio, threshold-boundary neutrality, invalid-depth filtering, crossed-spread protection, zero bid amount, missing ask depth, range fallback, and quality downgrade labels.
- Factor lab module compatibility exports.
- SQLite persistence for quote history, invalid quote filtering, concepts, partition-aware runtime cleanup, monitor/cache/alert events.
- Local user persistence keeps dirty legacy advice rows displayable without absorbing fresh snapshots, and keeps dirty/unsupported legacy alert rules readable but disabled while normalizing trigger counts and event numeric values.
- DB row-mapper compatibility exports.
- Provider status aggregation preserves history on config-only rows, reflects disabled capabilities, ignores disabled stale activity in metrics, normalizes invalid counts, uses deterministic priority/name/kind tie-breaks, and avoids treating unprobed capabilities as recent failures.
- System diagnostics warnings and suggestions for stale/invalid cache timestamps, failed capabilities, provider-diagnostic priority/caps, scheduler gaps, demo sources, and calendar fallback.
- Scheduler data-health events keep capability failures ahead of aggregate provider failures, use the same settings instance as DataHub, keep task definitions in deterministic order, distinguish missing from stale or invalid caches, and report runtime cleanup totals.
- Workbench `stock_workbench` stages advice snapshot persistence, normalized-symbol local-state reads, response assembly, workbench context cache trimming, and advice-history dedupe behavior.
- Workbench context cache expiry pruning, cancelled in-flight cleanup, clear-during-build writeback prevention, concurrent request coalescing, and DataHub instance isolation.
- Replay confidence warning on small samples.
- Static frontend CSS entrypoint, imported CSS module existence/order, JS function-size guardrails, workbench review/valuation HTML escaping, and research-panel render/Q&A/market/factor smoke coverage.
- Frontend app orchestration guards against stale advice/minute/chart-mark/AI responses, duplicate alert/note submits, malformed SSE frames, unsynchronized watchlist input symbols, unreadable API detail payloads, and partial workbench payloads.
- Documentation tooling keeps `app/`, `tests/`, and `tools/` function/class inventory in scope, while the API reference explicitly excludes the UI root route from business API counts.

## 4. Manual Smoke Test Checklist

1. Start the app on `127.0.0.1:8010`.
2. Open the browser and confirm the title is `AShareRadar`.
3. Query `600519`, `000001`, and `002182`.
4. Confirm a valid stock shows quote, trend score, support/resistance, K-line chart, data quality, alerts, notes, and market panels.
5. Confirm invalid symbols return a clear user-facing error without replacing the previous successful stock with misleading data.
6. Add and remove a watchlist item.
7. Add, update, pause, evaluate, and delete an alert rule.
8. Add, hide, show, and delete a note; confirm chart marks update.
9. Open diagnostics and check provider/capability state.
10. Confirm no browser console errors after initial load.

## 5. Regression Risk Areas

| Area | Risk | Required Test Style |
| --- | --- | --- |
| `DataHub.quotes` | Partial provider responses can reorder or drop symbols. | Unit tests with fake providers and temp SQLite. |
| Data quality | Trading-day logic can falsely penalize weekends/holidays. | Tests with fixed datetime and cached trading calendar. |
| Workbench cache | Cached context can suppress necessary user-data refreshes or leak across DataHub instances. | Tests for advice dedupe, alert/note freshness, and DataHub-scoped cache isolation. |
| LLM explanation | Model can invent ungrounded numeric claims or echo secrets in errors. | Unit tests for number grounding, stock-code reference boundaries, punctuation-adjacent numbers, list-marker exemptions, redaction, and fallback. |
| Frontend rendering | External data text can inject HTML or break layout. | JS syntax check, fake-DOM escaping tests, plus browser smoke tests. |
| Provider adapters | Upstream field names can change. | Adapter parsing tests with fixture-like rows. |

## 6. Latest Test Report

Last verified in this worktree:

```text
/opt/anaconda3/bin/python3 -m pytest tests/test_research_chip_modules.py tests/test_research_factor_scoring_modules.py tests/test_research_factor_specs_modules.py tests/test_research_diagnosis_modules.py -q -> 39 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_research_replay_modules.py tests/test_research_validation_modules.py -q -> 26 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_research_replay_modules.py -q -> 20 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_provider_utils_modules.py tests/test_data_sources.py tests/test_market_quotes_modules.py -q -> 111 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_research_risk_reward_modules.py tests/test_research_alpha_modules.py tests/test_tencent_provider_modules.py tests/test_data_sources.py tests/test_market_quotes_modules.py -q -> 147 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_research_leadership_modules.py tests/test_data_quality_modules.py tests/test_individual_workflow_modules.py tests/test_workbench_context_cache_modules.py tests/test_workbench_pipeline_modules.py tests/test_local_lifecycle.py -q -> 74 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_stock_rule_modules.py tests/test_minute_analysis_modules.py tests/test_research_regime_modules.py tests/test_analysis_research.py -q -> 100 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_research_qa_answer_modules.py tests/test_research_factor_specs_modules.py tests/test_research_risk_reward_modules.py tests/test_analysis_signal_modules.py tests/test_analysis_research.py -q -> 126 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_scheduler_modules.py tests/test_stock_activity_modules.py tests/test_research_alpha_modules.py tests/test_system_diagnostics_modules.py tests/test_analysis_research.py tests/test_stock_abnormal_events.py -q -> 115 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_market_sampling_modules.py tests/test_stock_overview_modules.py tests/test_research_theme_modules.py tests/test_analysis_research.py tests/test_market_overview_modules.py -q -> 83 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_datahub_klines_modules.py tests/test_tencent_provider_modules.py tests/test_analysis_signal_modules.py tests/test_data_sources.py tests/test_analysis_research.py -q -> 182 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_research_alpha_modules.py tests/test_stock_overview_modules.py tests/test_system_diagnostics_modules.py tests/test_data_quality_modules.py tests/test_research_factor_specs_modules.py tests/test_analysis_research.py -q -> 132 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_market_sampling_modules.py tests/test_market_overview_modules.py tests/test_research_risk_reward_modules.py tests/test_valuation_modules.py tests/test_analysis_research.py -q -> 99 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_chart_marks_modules.py tests/test_scheduler_modules.py tests/test_datahub_source_plan_modules.py tests/test_datahub_status_modules.py tests/test_system_diagnostics_modules.py tests/test_analysis_research.py -q -> 124 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_research_alpha_modules.py tests/test_research_risk_reward_modules.py tests/test_research_risk_modules.py tests/test_data_sources.py tests/test_datahub_klines_modules.py -q -> 154 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_quote_stream_modules.py tests/test_static_assets.py tests/test_frontend_app_flow.py tests/test_frontend_api_format_workbench.py -q -> 29 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_local_lifecycle.py tests/test_api_alert_routes.py tests/test_rules_alerts.py tests/test_api_watchlist_routes.py -q -> 63 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_research_risk_reward_modules.py tests/test_research_qa_report_modules.py tests/test_analysis_research.py -q -> 76 passed
/opt/anaconda3/bin/python3 -m pytest tests/test_research_qa_report_modules.py tests/test_research_qa_answer_modules.py tests/test_analysis_research.py -q -> 73 passed
PYTHON=/opt/anaconda3/bin/python3 npm run check -> 954 passed
/opt/anaconda3/bin/python3 tools/architecture_inventory.py -> passed
/opt/anaconda3/bin/python3 tools/api_inventory.py -> passed
```

## 7. Coverage Gaps

- No browser automation test is committed for first-screen rendering.
- No snapshot tests for the large frontend DOM.
- Route-level contract tests cover high-risk failure contracts and selected local-state reads, but not every stock response model.
- No load tests for SSE quote streaming.
- No migration tests from older SQLite database versions.
- No explicit performance budgets for provider timeout cascades.

Future test work should continue expanding FastAPI route-level contract tests across the remaining response models and add browser-level visual regression checks for the first screen.
