# Test Plan and Test Report

## 1. Test Objectives

- Verify stock lookup, quote/K-line acquisition, explicit daily adjustment provenance, analysis, research, and transparent degradation.
- Verify local SQLite state, schema compatibility, advice review history/as-of evaluation, current/custom historical scans, preview-claimed imports, runtime backup/cleanup protection, scheduler, and diagnostics.
- Verify browser request freshness, code/name autocomplete, exact chart inspection, local activity, review/scan reachability, notification retry, accessibility state, persistence isolation, and SSE behavior.
- Verify Python 3.12 plus Node 22/24 runtime declarations, host-timezone-independent market/audit/duration semantics, backward-compatible SQLite migration, dependency direction, configuration boundaries, exception/cancellation safety, and ratcheted typing.
- Verify liveness/readiness separation, bounded SQLite admission checks, low-cardinality reliability aggregation, explicit SLI/SLO sample floors, and optional isolated provider-canary contracts.
- Verify process-environment-only LLM configuration, provider-free locked security tooling, locked dependency audits, current/history secret scanning, reproducible CycloneDX SBOMs, immutable GitHub Action pins, generated-document drift, and maintainability gates.

## 2. Test Commands

```bash
export PROJECT_ROOT="${PROJECT_ROOT:-$HOME/AShareRadar}"
source "$PROJECT_ROOT/.venv/bin/activate"
export PYTHON="$PROJECT_ROOT/.venv/bin/python"
export PYTHONNOUSERSITE=1
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
npx --no-install playwright install chromium
npm run test:e2e
```

The development lock contains runtime and engineering dependencies. Branch coverage fails below 90%. Tests run with Python 3.12, `PYTHONNOUSERSITE=1`, temporary SQLite files, and fake providers/clients; the automated suite must not require credentials, persistent runtime data, live providers, or outbound network access. `tools/provider_canary.py` is a separate optional live diagnostic and is not part of required pull-request CI.

The Security workflow additionally runs both hashed Python lock audits, `npm audit --audit-level=high`, checksum-verified Gitleaks over the current tree and complete history with fully redacted output, and two independent SBOM generations followed by a recursive diff. Focused engineering-contract regression is available without a network:

```bash
$PYTHON -m pytest -q -p no:cacheprovider \
  tests/test_architecture_boundaries.py \
  tests/test_clock_modules.py \
  tests/test_exception_safety.py \
  tests/test_provider_canary.py \
  tests/test_reliability_modules.py \
  tests/test_supply_chain.py \
  tests/test_typing_contract.py
```

Run the live canary only after required checks pass and only when network/provider evidence is wanted:

```bash
$PYTHON tools/provider_canary.py
```

## 3. Current Automated Coverage

The current test suite is split by domain:

- `tests/test_active_research_review_backend.py`: bounded after-close watchlist refresh, per-symbol failure isolation, market-date and K-line provenance gates, watermark persistence, and same-day data-revision handling.
- `tests/test_advice_reviews.py`: snapshot binding, no-lookahead windows, ambiguous barriers, pending/insufficient states, revision ownership, idempotent evaluation persistence, and cross-revision evaluation history.
- `tests/test_advice_review_window_contract.py`: trading-day completeness, 15:15 publication boundaries, cross-contract rejection, and weekend pending behavior.
- `tests/test_akshare_stock_metadata.py`: AKShare industry/list-date aliases, placeholder cleanup, and strict date normalization.
- `tests/test_analysis_research.py`
- `tests/test_analysis_signal_modules.py`
- `tests/test_architecture_boundaries.py`: lower-layer dependency direction, direct domain-model imports, application import acyclicity, and clock-adapter ownership of wall time.
- `tests/test_api_alert_routes.py`
- `tests/test_api_container_modules.py`
- `tests/test_api_data_routes.py`
- `tests/test_api_error_modules.py`
- `tests/test_api_local_data_routes.py`: single-use preview claims, file/mode/database-state binding, expiry/replay rejection, verified pre-import backups, stable backup failures, and backed-up manual user-history cleanup.
- `tests/test_api_monitoring_routes.py`
- `tests/test_api_notes_routes.py`
- `tests/test_api_review_routes.py`: review-plan deletion success/not-found contracts and OpenAPI response schema coverage.
- `tests/test_api_security_modules.py`
- `tests/test_api_stock_routes.py`
- `tests/test_api_watchlist_research_queue.py`
- `tests/test_api_watchlist_routes.py`
- `tests/test_app_lifecycle_integration.py`
- `tests/test_audit_epoch_repositories.py`: microsecond-preserving audit-time ordering for quote snapshot and history upserts.
- `tests/test_cache_migration_guard.py`: explicit legacy timezone binding, runtime-leader exclusion, and migration disk-space preflight.
- `tests/test_cache_freshness_modules.py`
- `tests/test_cache_stats_modules.py`
- `tests/test_chart_marks_modules.py`
- `tests/test_clock_modules.py`: Shanghai market time, UTC audit serialization/parsing, monotonic/performance adapters, mixed timestamp compatibility, and legacy audit-column migration without market-field rewriting.
- `tests/test_config_modules.py`: environment parsing/validation and exact `ASHARE_RADAR_*` operations-document coverage.
- `tests/test_container_settings_lifecycle.py`
- `tests/test_data_quality_modules.py`
- `tests/test_data_sources.py`: provider adapters and fallback ordering, including AKShare daily fallback through Eastmoney and final Sina `qfq` rows.
- `tests/test_datahub_cache_modules.py`
- `tests/test_datahub_klines_modules.py`: daily/minute cache and provider contracts, including the distinction between stock coverage misses and a typed temporarily unavailable daily provider chain.
- `tests/test_datahub_metadata_modules.py`
- `tests/test_datahub_metadata_structure.py`: metadata facade compatibility, dependency acyclicity, module-size limits, and coverage/persistence use of the same normalized stock-pool set.
- `tests/test_datahub_orderbook_modules.py`
- `tests/test_datahub_quotes_modules.py`
- `tests/test_datahub_runtime_modules.py`: request-key sharing/admission/orphan isolation, provider-chain ready/temporary/permanent state and retry hints, bounded daemon executor ownership, queued-work cancellation, idempotent and cancellation-safe deferred close, active-worker/provider-client ordering, automatic post-quiescence cleanup, and stuck-SDK subprocess exit.
- `tests/test_datahub_source_plan_modules.py`
- `tests/test_datahub_status_modules.py`
- `tests/test_datahub_status_service_modules.py`
- `tests/test_db_mappers.py`
- `tests/test_discovery_api.py`: discovery preset, leaderboard, rank-change, and research-queue HTTP contracts.
- `tests/test_discovery_portability.py`: discovery preset export/import compatibility and transactional conflict behavior.
- `tests/test_discovery_presets.py`: versioned preset CRUD, optimistic concurrency, filtering, sorting, and queue provenance.
- `tests/test_discovery_rank_changes.py`: comparable-run selection and deterministic new/up/down/unchanged/exit rank movements.
- `tests/test_exception_safety.py`: reviewed `BaseException` ownership boundaries, cancellation propagation/terminal observers, provider sanitizer convergence, and static secret-output checks.
- `tests/test_fallback_logging.py`: allowlisted SQLite persistence-failure categories, stderr fallback visibility, and secret/raw-error suppression.
- `tests/test_financial_health_modules.py`
- `tests/test_financial_metrics_modules.py`
- `tests/test_frontend_advice_timeline.py`
- `tests/test_frontend_audit_time.py`: host-timezone-independent parsing of fixed UTC, explicit offsets, and legacy Shanghai-naive audit timestamps, plus use by scan, alert, and diagnostics views.
- `tests/test_frontend_api_format_workbench.py`: fetch parsing for 204/205, zero-length/empty success payloads, structured errors, and workbench formatting.
- `tests/test_frontend_app_flow.py`: core-workbench-first cold-load order, stock/session orchestration, immediate advice-timeline loading ownership, A-B-A stale response rejection, SSE, persistence, companion request guards, and full browser-state/SSE refresh after user-data import.
- `tests/test_frontend_chart_inspector.py`: immutable daily/minute inspection snapshots, plot-boundary hit testing, CSS-to-canvas pointer mapping, keyboard traversal, redraw clamping, and listener cleanup.
- `tests/test_frontend_chart_context.py`: visible-window clipping, moving-average warm-up context, stable mark limits, and edge-aware non-overlapping mark labels.
- `tests/test_frontend_chart_workspace.py`: daily/minute chart controls, request budgets, stale safety, unavailable audit rows, and explicit unavailable handling for 204/empty/null or mismatched minute responses.
- `tests/test_frontend_diagnostics.py`
- `tests/test_frontend_discovery.py`: saved-filter lifecycle, leaderboard paging, queue provenance, rank comparison, stale-response ownership, and escaped rendering.
- `tests/test_frontend_local_activity_state.py`: notes/alert-event source synchronization, sanitized unavailable states, and stale/aborted read ownership.
- `tests/test_frontend_local_data.py`: latest file-read/import-preview/commit/cleanup-preview ownership, stale file/mode invalidation, server preview-token authority, rollback-backup feedback, portable download cleanup, and storage diagnostic rendering.
- `tests/test_frontend_local_data_security.py`: guarded POST semantics for local user-data export.
- `tests/test_frontend_market_scan_reliability.py`: compact whole-run progress, aggregated failure examples, capability labels, and responsive leaderboard contracts.
- `tests/test_frontend_notes_alerts_requests.py`: independent note/alert write ownership, local successful-write reconciliation, stale readback protection, explicit readback degradation, and row-scoped failure feedback.
- `tests/test_frontend_notifications.py`: permission timing, first-poll baseline, keyset page draining, non-advancing-page safety, trigger de-duplication, burst summaries, and failed-delivery retry without skips.
- `tests/test_frontend_research_activity.py`: three-source normalization/order, limits and filters, escaped output, and distinct loading/empty/partial-or-total-unavailable states.
- `tests/test_frontend_research_panels.py`
- `tests/test_frontend_review_scan.py`: escaped review rendering, snapshot-bound and rendered-symbol-owned plan requests, server-current-time handling for today, Shanghai end-of-day historical evaluation, lazy/retryable history ownership, current/custom scan controls, strict prices/symbols, whitelisted conditions, and result rendering.
- `tests/test_frontend_stock_search.py`: 250 ms debounce, abort/stale protection, bounded LRU behavior, payload validation, explicit states, keyboard selection, and destruction cleanup.
- `tests/test_frontend_watchlist_requests.py`
- `tests/test_frontend_workspace_preferences.py`: persisted workspace selection, unsupported-value fallback, and storage failure tolerance.
- `tests/test_futu_provider_modules.py`
- `tests/test_indicator_levels_modules.py`
- `tests/test_indicator_trend_modules.py`
- `tests/test_indicator_volume_modules.py`
- `tests/test_individual_workflow_modules.py`
- `tests/test_kline_contract.py`: legacy migration isolation, coexisting adjustment modes, mixed-batch rejection, and explicit provider provenance.
- `tests/test_leader_scoring_modules.py`
- `tests/test_llm_explainer.py`
- `tests/test_local_data_portability.py`: exact export allowlist, merge/replace dry runs, schema drift rejection, conflict behavior, and transactional rollback.
- `tests/test_local_lifecycle.py`: local persistence/migrations, comparable advice-change unread counting, viewed-through watermark races, automatic user-history cleanup protection, and shared runtime invariants.
- `tests/test_market_overview_modules.py`
- `tests/test_market_quotes_modules.py`: quote persistence, fixed Shanghai event time, epoch ordering, equal-event fallback/completeness/fetched-at priority, and legacy UTC ISO compatibility.
- `tests/test_market_sampling_modules.py`
- `tests/test_market_scan_api.py`: asynchronous create/deduplication, lifecycle controls, no-store reads, validation, pagination, and full filter/sort forwarding.
- `tests/test_market_scan_automation.py`: SH/SZ/BJ provider preflight, sanitized persisted diagnostics, bounded restart-safe cadence, structured retry eligibility, and pending-only automatic resume.
- `tests/test_market_scan_architecture.py`: executor ownership, dependency direction, protocol boundaries, and split-module maintainability.
- `tests/test_market_scan_execution.py`: full-universe batching, bounded concurrency, quote/K-line failure classification, and persistence behavior.
- `tests/test_market_scan_frontend.py`: split-module/version wiring, strict contracts, one-timer polling, bounded backoff, latest recovery, new-run pagination reset, online recovery, escaped rendering, and ARIA state.
- `tests/test_market_scan_lifecycle.py`: lifecycle release, cancel/restart recovery, terminal SQLite retry, owned post-worker recovery, and foreign-leader protection.
- `tests/test_market_scan_pressure.py`: AIMD concurrency, busy/timeout/retry-after/systemic pressure signals, wait-budget accounting, healthy recovery, per-symbol exclusions, and terminal pressure diagnostics.
- `tests/test_market_scan_repository.py`: transitions, batch invariants, atomic task creation/scan attachment and scan/task terminal writes, retry-copy rollback/concurrency guards, stable ranks, filters, immutable snapshots, structured degradation, idempotent repair, and active-run uniqueness.
- `tests/test_market_scan_repository_structure.py`: repository facade compatibility, dependency direction/acyclicity, and production module-size limits.
- `tests/test_market_scan_retry.py`: pending preservation, pending-only batch recovery, provider wait budgets, and resumable retry plans.
- `tests/test_market_scan_scheduler.py`: publish-time guard, automatic-run suppression, stale-run recovery, and scheduler ownership.
- `tests/test_market_scan_scoring.py`: deterministic versioned score, completed-`data_date` snapshot boundary, quote/K-line date and adjustment contracts, required liquidity inputs, missing/skip boundaries, and A-share-only universe tags/exclusions.
- `tests/test_market_scan_trust_contract.py`: score-spec hash/replay, ALL/SH/SZ/BJ publication gates, snapshot span, trading-date drift, and whole-run deadlines.
- `tests/test_market_scan_universe.py`: current-listed A-share filtering, future/unknown listing dates, new-stock boundaries, de-duplication, and precise ST-name recognition.
- `tests/test_market_scan_validation.py`: market coverage thresholds, systemic data-failure guards, degradation accounting, and terminal publication decisions.
- `tests/test_minute_analysis_modules.py`
- `tests/test_optional_kline_parsing_modules.py`
- `tests/test_optional_provider_concurrency.py`
- `tests/test_provider_errors_modules.py`
- `tests/test_provider_canary.py`: SH/SZ/BJ quote plus five-row daily-K contracts, three-market stock-pool validation, independent market/stock-pool and overall timeout boundaries, cancellation/cleanup, temporary SQLite CLI wiring, sanitization, and complete/partial/failure exit codes.
- `tests/test_provider_failure_status_modules.py`
- `tests/test_provider_priority_settings.py`: environment-backed provider ordering, normalization/de-duplication, optional-source defaults, and preflight/automatic-retry setting validation.
- `tests/test_provider_registry_modules.py`
- `tests/test_provider_status_aggregation_modules.py`
- `tests/test_provider_status_repository_modules.py`
- `tests/test_provider_utils_modules.py`
- `tests/test_quote_stream_modules.py`
- `tests/test_reliability_modules.py`: UTC-hour aggregation, bounded dimensions, workbench/provider counters, scan/task exclusions, fixed windows/targets/sample floors, counter invariants, and deterministic nearest-rank percentiles.
- `tests/test_research_alpha_modules.py`
- `tests/test_research_breadth_modules.py`
- `tests/test_research_chip_modules.py`
- `tests/test_research_conclusion_change.py`
- `tests/test_research_diagnosis_modules.py`
- `tests/test_research_event_digest_modules.py`
- `tests/test_research_factor_calibration_modules.py`
- `tests/test_research_factor_modules.py`
- `tests/test_research_factor_scoring_modules.py`
- `tests/test_research_factor_specs_modules.py`
- `tests/test_research_factor_weight_modules.py`
- `tests/test_research_leadership_modules.py`
- `tests/test_research_peer_modules.py`
- `tests/test_research_qa_answer_modules.py`
- `tests/test_research_qa_report_modules.py`
- `tests/test_research_regime_modules.py`
- `tests/test_research_replay_modules.py`
- `tests/test_research_risk_modules.py`
- `tests/test_research_risk_reward_modules.py`
- `tests/test_research_t_strategy_modules.py`
- `tests/test_research_theme_modules.py`
- `tests/test_research_timeframe_modules.py`
- `tests/test_research_validation_modules.py`
- `tests/test_review_modules.py`
- `tests/test_rules_alerts.py`
- `tests/test_runtime_backup.py`: snapshot verification, tamper rejection, unified/legacy guarded restore, fixed-order bounded operation leases, thread/process concurrent rotation, in-use bundle protection, set-based retention, retry/task lineage convergence, cleanup preview parity, and review-linked advice protection.
- `tests/test_runtime_coordinator.py`: repeated cross-process leadership exclusion, shared scheduler/scanner ownership, standby takeover/status, retryable partial activation, pre-leadership service guards, and delayed takeover while a non-cooperative old task remains alive.
- `tests/test_runtime_environment_modules.py`
- `tests/test_runtime_maintenance_regressions.py`: one-pass retry-lineage retention convergence and threshold-gated best-effort SQLite compaction.
- `tests/test_scheduler_modules.py`: task execution/state, cancellation, degraded outcomes, bounded stop with quiescence-delayed guard release, maintenance throttling, persistence fallback, and runtime-leadership integration.
- `tests/test_scheduler_structure.py`: scheduler facade compatibility, internal dependency acyclicity, and production module-size limits.
- `tests/test_schema_compat.py`
- `tests/test_scoring_modules.py`
- `tests/test_sina_client.py`: SH/SZ/BJ symbol mapping, bounded/rate-limited requests, safe JSON and adjustment-factor parsing without `eval`, forward-filled `qfq` calculation, strict OHLC/volume/date validation, and provider error classification.
- `tests/test_static_assets.py`
- `tests/test_stock_abnormal_events.py`
- `tests/test_stock_activity_modules.py`
- `tests/test_stock_analysis_modules.py`
- `tests/test_stock_event_summary.py`
- `tests/test_stock_lhb_modules.py`
- `tests/test_stock_lookup_modules.py`
- `tests/test_stock_overview_modules.py`
- `tests/test_stock_pool_metadata.py`: total and SH/SZ/BJ industry/list-date completeness diagnostics without blocking price-only ranking.
- `tests/test_stock_pool_industry_enrichment.py`: one-shot BaoStock industry enrichment, source provenance, cache persistence, and non-fatal isolated capability failure.
- `tests/test_stock_rule_modules.py`
- `tests/test_stock_strategy_modules.py`
- `tests/test_symbol_modules.py`
- `tests/test_supply_chain.py`: SHA-pinned actions, disabled checkout credentials, runtime/dev Python and npm audits, redacted current/history secret scanning, reproducible Python/npm CycloneDX SBOMs, Dependabot coverage, and portable files.
- `tests/test_system_diagnostics_modules.py`
- `tests/test_tencent_provider_modules.py`: current Tencent quote behavior plus SH/SZ/BJ `newfqkline` requests, `qfqday`/unadjusted-day response handling, validation, and failure classification.
- `tests/test_tool_inventory_modules.py`: generated-document drift, test-plan completeness, machine-path guards, dependency layering, immutable action SHA pins, and Node 24 action-major guards.
- `tests/test_trading_calendar_modules.py`
- `tests/test_typing_contract.py`: explicit existing mypy file scope, non-shrinking ratchet, sorted uniqueness, and prohibition of hidden type errors.
- `tests/test_uvicorn_smoke.py`: real loopback Uvicorn startup with isolated SQLite, API/static responses and cache headers, plus a deliberately held-open quote SSE connection and traceback-free `SIGINT` shutdown bounded by the test's two-second graceful-shutdown setting.
- `tests/test_valuation_modules.py`
- `tests/test_watchlist_research_queue.py`: queue validation/order, mark-viewed state, comparable changed-advice unread increments, and viewed-through watermark preservation of later changes.
- `tests/test_watchlist_scan.py`: explicit/current universes, as-of results, missing rows, versioned fixed rules, script rejection, and symbol caps.
- `tests/test_workbench_context_cache_modules.py`
- `tests/test_workbench_pipeline_modules.py`

Browser regression support is indexed separately:

- `tests/e2e/frontend-flow.spec.js`: desktop/mobile workbench, code/name suggestions, exact chart inspection, local research activity, queue, timeline loading/ownership, stale-request, and request-budget flows.
- `tests/e2e/static-server.mjs`: local static fixture server used by Playwright.

## 4. Manual Smoke Test Checklist

1. Start the app on `127.0.0.1:8010` with the documented single-worker `--timeout-graceful-shutdown 5` command. Confirm `/api/health/live` is process-only, `/api/health/ready` reports SQLite plus `leader`/`standby`/`single`, every health response is `no-store`, and readiness becomes `503` during shutdown; with an SSE stream open, stop it and confirm the listener exits within the bounded shutdown window.
2. Type a Chinese name or partial code in both stock inputs; confirm the 250 ms autocomplete can be navigated by pointer and keyboard, and that loading, empty, and unavailable messages are distinct.
3. Enter a complete valid 6-digit code and confirm it submits directly without `/api/stocks`; confirm a non-complete cache miss adds only its search request.
4. Switch A-B-A between valid SH/SZ symbols and confirm the timeline and advice-review list retain only the newest request's state.
5. Switch daily ranges through 20/60/120/240 and confirm no request is issued.
6. Inspect the workbench JSON and confirm every daily row declares `adjustment_mode=qfq` plus non-empty `as_of`, `data_version`, and `contract_version` values.
7. Inspect first/middle/last rows on both canvases by desktop hover, touch tap, and keyboard; confirm time, OHLC, change, volume, enabled MAs, source/cache/fallback/fetch metadata, and crosshair position match the selected row without a request.
8. Switch minute intervals and confirm one request for each new interval and none when reselecting the current interval.
9. Confirm minute 204/empty/`null`, wrong-symbol, wrong-interval, and unavailable reports clear stale chart data and withhold executable levels or T-plan ranges.
10. In Tools, confirm local activity merges recommendation changes, alert events, and notes with distinct loading, empty, partial, and unavailable states.
11. Create a review plan from a persisted advice snapshot with `target > snapshot > stop`, evaluate it at a historical cutoff and at current time, expand its evaluation history, then edit it and confirm the new revision does not display the prior revision's result as current.
12. Run each fixed watchlist condition and a combined scan against both the current watchlist and custom codes; repeat with a historical cutoff and confirm excluded symbols, all-selected matching, as-of provenance, and missing-data rows are handled explicitly.
13. Add a watchlist item, edit queue metadata, load its advice timeline, and mark it viewed through the newest displayed advice ID; create a later comparable change and confirm it remains unread. Confirm excluded items do not enter quote refresh.
14. Add/update/delete an alert and note, and confirm navigation does not cancel an accepted persistence write.
15. Enable desktop alerts, establish the first-poll baseline, then create a new trigger and confirm one notification while the page remains open. Simulate one notification-construction failure and confirm the failed and later events are delivered in order on the next poll without duplicating the successful prefix.
16. Export user data, load the JSON in merge mode, and confirm commit stays disabled until the matching server dry-run preview succeeds. Confirm the commit reports a verified rollback backup; change the file, mode, or target user data after preview and confirm commit is rejected. Treat replace as destructive and verify it only against disposable data.
17. Create and verify a runtime backup. Open cleanup preview and confirm user-history candidates trigger a verified pre-cleanup backup while review-linked advice is excluded. Run scheduled health cleanup and confirm alert/advice history is unchanged.
18. Open diagnostics and confirm fetch/market freshness, storage budget, categorized rows, providers, scheduler, and trading-calendar guidance remain readable.
19. Open **全市场榜单**, start one scan, and confirm the request returns immediately while real processed/total progress changes. Repeat the click and confirm it follows the same active run. Cancel and retry, then confirm a new linked run is created, the original stays unchanged, clean successful rows are retained, and unfinished/degraded rows resume.
20. On a disposable fake-provider run, include SH/SZ/BJ, ST/new, suspended, short-history, and failed-source rows. Confirm only successful rows receive stable ranks, coverage uses `success + missing`, each market retains at least 90% eligible rows, mass skipping blocks publication, and page/filter/sort views remain bounded.
21. Simulate a deleted active scan and repeated poll failures; confirm the UI returns to latest with bounded backoff, resets page/results when a different run appears, retries immediately after `online`, keeps one request/timer at a time, and announces progress/result milestones through one live region.
22. With disposable dual processes, confirm only one owns `<SQLite path>.runtime-leader.lock`, scheduler and scanner never split ownership, and standby takes over both after the leader exits. Confirm restore refuses the unified lock and both legacy compatibility locks.
23. Confirm invalid symbols stay in the query panel, while a failed valid-stock load clears the prior stock content and shows an explicit failure; then check desktop/mobile layouts for console errors.
24. Generate enough disposable workbench/provider/task/scan observations to cross each sample floor. Confirm `/api/system/reliability` reports the documented 7/30-day targets, returns `insufficient_data` before the floor, excludes cancelled work and retry durations as documented, and keeps bucket rows hourly rather than request-specific.
25. In an explicitly network-enabled environment, run `tools/provider_canary.py` with disposable or default SH/SZ/BJ representatives. Confirm stdout is sanitized JSON, the configured runtime database is untouched, quote/five-row daily-K/stock-pool summaries are present, and the exit status distinguishes complete, partial, and unavailable results.

## 5. Request And Browser Budgets

| Flow | Expected additional requests |
| --- | ---: |
| Cold stock load, including SSE | 14 |
| Each stock switch, including SSE | 5 |
| Daily chart range switch | 0 |
| Each new minute interval | 1 |
| Repeated active minute interval | 0 |
| Complete valid 6-digit input | 0 stock-search requests |
| Non-complete user input, after debounce and on cache miss | 1 stock-search request |
| Repeated cached autocomplete query | 0 stock-search requests |
| Daily/minute pointer, touch, or keyboard inspection | 0 |
| Local research-activity filter switch | 0 |
| Fixed-condition watchlist scan | 1 |
| Opening the full-market workspace | 1 latest-run request; while visible and idle, 1 non-overlapping latest-run check every 30 seconds; one additional result request only for a terminal run |
| Active full-market scan | 1 progress request per 2-second poll; no overlapping poll |
| Full-market result page/filter change | 1 request, capped at 100 rendered rows |
| Advice-review evaluation | 1 |
| Expanding one advice-review history for the first time | 1 |
| Each Tools-tab cleanup preview | 1 |
| Enabling browser notifications | 1 immediate baseline page; later 30-second polls use as many 50-event keyset pages as needed, capped at 200 pages |

Stock-search requests are not part of the four-request stock-switch baseline: only a debounced, uncached, non-complete user query may trigger one. A selected suggestion then follows the ordinary stock-load budget. The latest recorded desktop/mobile browser matrix is **45 passed, 9 skipped**; the skipped cases are intentional device-exclusive scenarios recorded by Playwright rather than treated as passes.

Regression-sensitive boundaries include Python/Node/npm declaration drift, host-timezone independence, Shanghai-to-UTC audit migration, monotonic durations, checked dependency direction and mypy-scope shrinkage, cancellation propagation, health admission semantics, reliability sample floors/cardinality/exclusions, isolated provider-canary state, runtime/dev dependency audits, redacted complete-history secret scanning, reproducible SBOMs, immutable Action pins, core-workbench-first cold-load dispatch under browser connection limits, equal-event quote quality priority and legacy UTC ordering, `qfq`/legacy K-line isolation, quote/minute session freshness, provider request-key single-flight/orphan isolation plus daemon-worker process exit, the 15:15 daily publish threshold, full-market `data_date` snapshots, atomic task attachment and scan/task terminals, owned terminal-failure recovery, structured degradation and unified retry plans, normalized atomic stock-pool replacement, quiescence-delayed single runtime leadership and whole-service takeover, set-based retention with retry-lineage convergence, cross-process backup leases and explicit rotation/restore guards, scan contract/backoff/latest recovery and ARIA state, server-current-time handling for today's review/scan, rendered-symbol ownership, single-use import previews, latest-owner browser state, serialized backup-before-import, automatic user-history exclusion, review-linked retention, successful write reconciliation, comparable-change unread watermarks, notification cursor advancement, stale companion responses, and request-budget drift.

## 6. Latest Test Report

Each row describes the exact worktree scope verified by its command; older records remain for traceability. Automated verification uses an isolated locked Python environment, fake providers/HTTP clients, temporary databases, and no credentials or live-provider dependency unless a row explicitly says otherwise.

| Date | Worktree State | Environment | Command | Scope | Result | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-07-28 | Provider-risk hardening worktree with capability-filtered priority overrides, bounded full-market preflight, persisted automatic retry, adaptive provider-pressure control, independent canary timeouts, and synchronized documentation | macOS, project Python 3.12 `.venv`, Node 24.14.1, npm 11.11.0, Asia/Shanghai host timezone, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing` | Full Python suite with branch coverage, including real Uvicorn loopback smoke tests | 2417 passed and 64 subtests passed in 187.24s; 91.13% coverage | Covers provider capability filtering, SH/SZ/BJ preflight, restart-safe retry cadence, cancellation exclusions, AIMD concurrency/backoff, low-cardinality diagnostics, scheduler due-state reporting, dedicated stock-pool canary timeout, and existing application regressions. |
| 2026-07-28 | Same provider-risk hardening worktree | macOS, project Python 3.12 `.venv`, Node 24.14.1, npm 11.11.0 | `npm run check` | Runtime declaration, Python compile/pyflakes, JavaScript syntax, and full pytest suite | 2417 passed and 64 subtests passed in 144.03s | Confirms the documented local quality command after integration and documentation synchronization. |
| 2026-07-28 | Same provider-risk hardening worktree after installing the Playwright 1.61.1 Chromium runtime | macOS, Playwright Chromium desktop and mobile projects | `npm run test:e2e` | Full responsive browser regression, including discovery, scan/retry/recovery, charts, accessibility, online recovery, and cross-page notifications | 45 passed, 9 intentional device-exclusive skips in 1.1m | The first invocation was an environment-only failure before test execution because the matching Chromium build was absent; the browser runtime was installed and the unchanged suite then passed. |
| 2026-07-28 | Research-loop worktree with historical evidence isolation, active watchlist refresh, structured review evaluation, score replay/publication gates, saved discovery presets, and frontend reliability fixes | macOS, project Python 3.12 `.venv`, Node 24.14.1, npm 11.11.0, Asia/Shanghai host timezone, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing` | Full Python suite with branch coverage, including real Uvicorn loopback smoke tests | 2360 passed and 64 subtests passed in 206.01s; 91.21% coverage | Covers immutable historical evidence, provenance-aware advice refresh/review, score-spec hash and replay, SH/SZ/BJ publication gates, resumable deadlines, discovery CRUD/rank changes/queue provenance, capability labels, and maintainability guards. |
| 2026-07-28 | Same research-loop worktree after browser-regression fixes | macOS, Playwright Chromium desktop and mobile projects | `npm run test:e2e` | Full responsive browser regression, including explicit 360/390/430/desktop discovery layouts | 45 passed, 9 intentional device-exclusive skips in 1.0m | Confirms scan re-entry and global-progress ownership, historical read-only evidence, online retry targets, compact mobile header/focus order, discovery workflows, charts, notifications, and no narrow-screen overflow. |
| 2026-07-28 | Same final worktree | macOS, project Python 3.12 `.venv`, Node 24.14.1, npm 11.11.0 | `npm run check` | Runtime declaration, Python compile/pyflakes, JavaScript syntax, and full pytest suite | 2360 passed and 64 subtests passed in 88.49s | Confirms the documented local quality command after all browser-regression fixes and documentation updates. |
| 2026-07-27 | Final engineering-quality convergence worktree with isolated Security tooling and symmetric scan/retry publication guards | macOS, project Python 3.12 `.venv`, Node 24.14.1, npm 11.11.0, Asia/Shanghai host timezone, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing` | Full Python suite with branch coverage, including real Uvicorn loopback smoke tests | 2272 passed and 64 subtests passed in 156.91s; 91.51% coverage | Includes fixed-width UTC migration, configurable legacy timezone, old-bundle import normalization, frontend Shanghai rendering, SLI sample semantics, strict canary contracts, architecture guards, provider-free security-lock contracts, and retry prevention before daily bars are publishable. |
| 2026-07-27 | Same final worktree | macOS, project Python 3.12 `.venv`, Node 24.14.1, npm 11.11.0 | `npm run check` | Runtime declaration, Python compile/pyflakes, JavaScript syntax, and full pytest suite | 2272 passed and 64 subtests passed in 91.94s | Confirms the documented local quality command in the default host timezone. |
| 2026-07-27 | Same final worktree | macOS, Playwright Chromium desktop and Pixel 7 projects | `npm run test:e2e` | Full responsive browser regression | 33 passed, 5 intentional device-exclusive skips in 49.0s | Confirms request ownership, scan lifecycle, charts, research activity, responsive layout, online recovery, and cross-page notification coordination. |
| 2026-07-24 | Engineering-quality convergence worktree and synchronized documentation | macOS, project Python 3.12 `.venv`, Node 24.14.1, npm 11.11.0, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider <architecture, clock, exception-safety, provider-canary, reliability, supply-chain, and typing-contract modules>` | Runtime-independent engineering contracts with fake providers and temporary SQLite | 53 passed in 10.31s | Confirms dependency direction/acyclicity, UTC/Shanghai/monotonic semantics, legacy audit migration, reviewed cancellation boundaries, temporary-DB quote/five-row-daily-K/stock-pool canary behavior, SLO math/cardinality, supply-chain workflow contracts, and non-shrinking mypy scope. |
| 2026-07-24 | Same current worktree after engineering documentation update | macOS, project Python 3.12 `.venv` | `$PYTHON -m pytest -q -p no:cacheprovider tests/test_tool_inventory_modules.py tests/test_config_modules.py tests/test_supply_chain.py` | Documentation path/index/configuration plus supply-chain guardrails | 65 passed in 15.79s | Confirms every current Python test module appears exactly once in the test index, all app/tool `ASHARE_RADAR_*` variables are documented, no machine-specific path is embedded in guarded documentation, and Security declares runtime/dev audits, redacted current/history scans, reproducible SBOMs, SHA-pinned actions, and no persisted checkout credentials. |
| 2026-07-22 | Final provider-reliability and full-market recovery worktree | macOS, project Python 3.12 `.venv`, Node 22.23.1, `TZ=UTC`, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing --cov-report=xml` | Full Python suite with branch coverage under the GitHub quality-job runtime contract | 2177 passed in 95.27s; 91.67% coverage | Coverage gate is 90%; includes host-timezone-independent cache fixtures, explicit notification-lock test doubles that do not depend on Node 24 globals, provider-chain state/capacity, Tencent/Sina SH/SZ/BJ daily-K fallback, zero-volume and stale suspended-stock handling, bulk-response truncation protection, pending-only recovery, cancellation/write transactions, real Uvicorn/SSE shutdown, multiprocess coordination, and persistence regressions. |
| 2026-07-22 | Same final worktree | macOS, project Python 3.12 `.venv` | `npm run check` | Python compile, pyflakes, JavaScript syntax, and full pytest suite | 2177 passed in 64.10s | Confirms the convenient local quality command against the final source and test inventory. |
| 2026-07-22 | Current shared worktree after provider-chain pending/retry documentation and Tencent/Sina daily-K reliability changes | macOS, project Python 3.12 `.venv`, fake providers/HTTP clients | `$PYTHON -m pytest -q tests/test_config_modules.py tests/test_sina_client.py tests/test_datahub_runtime_modules.py <targeted daily-K, market-scan outage/recovery, and Tencent endpoint tests>` | Reliability-focused configuration, provider-chain, fallback, and scan regression subset | 135 passed in 1.60s | No outbound network; confirms exact environment-variable documentation, chain-state classification, false-missing prevention, pending-only recovery, safe Sina `qfq` parsing, and the Tencent SH/SZ/BJ endpoint contract. |
| 2026-07-19 | Current worktree after stock-pool atomicity, structured degradation, scan/task transaction recovery, unified quiescent runtime leadership, bounded retention/backup leases, module splits, frontend recovery, and runtime-lock hygiene | macOS, isolated locked Python 3.12 environment, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing --cov-report=xml` | Full Python suite with branch coverage | 2000 passed in 83.28s; 91.57% coverage | Coverage gate is 90%; includes real Uvicorn/SSE shutdown, multiprocess leadership/backup, 5,500-symbol retention, and scan consistency regressions. |
| 2026-07-19 | Same current worktree | macOS, isolated locked Python 3.12 environment, `PYTHONNOUSERSITE=1` | `npm run check` | Python compile, pyflakes, JS syntax, full pytest suite | 2000 passed in 52.69s | Confirms the convenient local regression command against the final source and test inventory. |
| 2026-07-18 | Audited full-market baseline with SH/SZ/BJ background scanning, per-market pool guards, deterministic ranking, immutable derived retry, explicit fallback degradation, persistence, API, and frontend workspace | macOS, isolated locked Python 3.12 environment, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing` | Full Python suite with branch coverage | 1899 passed in 66.91s; 91.59% coverage | Coverage gate is 90%; predates the current runtime-leadership, structured-degradation, retention, and module-split changes. |
| 2026-07-18 | Audited baseline after review-plan deletion, alert id-cursor/retention alignment, notification enable/disable and bounded backlog draining, scheduler degraded outcomes, and metadata freshness diagnostics | macOS, isolated locked Python 3.12 environment, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing` | Full Python suite with branch coverage | 1787 passed in 58.24s; 91.92% coverage | Coverage gate is 90%; no live provider, credential, persistent runtime-data, or outbound-network dependency. |
| 2026-07-17 | Current shared worktree after review/scan time ownership, local-data import/cleanup concurrency, alert readback, unread, notification, security, and provider-runtime hardening | macOS, isolated locked Python 3.12 environment, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing` | Full Python suite with branch coverage | 1759 passed in 112.01s; 91.87% coverage | Coverage gate is 90%; installed from `requirements-dev-lock.txt` with `--require-hashes`; no live provider, credential, persistent runtime-data, or outbound-network dependency. |
| 2026-07-16 | Historical source after autocomplete, chart inspection, and local activity; before the current feature set | macOS, Python 3.12, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider` | Full Python suite without coverage | 1633 passed in 35.06s | Retained as a baseline, not a result for the current shared worktree. |
| 2026-07-15 | Historical baseline before the current three frontend additions | macOS, Python 3.12, `PYTHONNOUSERSITE=1` | `$PYTHON -m pytest -q -p no:cacheprovider` | Full Python suite without coverage | 1608 passed in 33.58s | Does not include the four new Python frontend test modules indexed above. |
| 2026-07-10 | Local dirty worktree during state-consistency hardening | macOS, project Python 3.12, `PYTHONNOUSERSITE=1` | `npm run check` | Python compile, pyflakes, JS syntax, full pytest suite | 1142 passed | Historical regression record retained for traceability. |

Recent targeted checks kept for traceability:

| Date | Command | Scope | Result | Why It Was Run |
| --- | --- | --- | --- | --- |
| 2026-07-28 | `$PYTHON tools/provider_canary.py` | Live SH/SZ/BJ representative quote/five-row daily-K contracts plus refreshed three-market stock-pool contract in isolated temporary state | exit `0`; all 3 markets succeeded without cache/fallback; stock pool 5533 rows (SH 2310, SZ 2892, BJ 331) in 9.44s | Confirms the canary now applies the ordinary 8-second budget only to representative market probes and the dedicated 60-second configured budget to the larger stock-pool refresh, all under the 20-second overall deadline. |
| 2026-07-27 | Live HTTP liveness/readiness/workbench checks, pre-15:15 retry request, post-15:15 full-market run `#9`, ranked-result readback, and post-run SQLite checks | Real service lifecycle, daily publication guard, current SH/SZ/BJ universe, deterministic ranking persistence, and database integrity | readiness `leader`; `600519.SH` workbench quality 88; pre-boundary retry HTTP 400; 5532/5532 processed, 5487 ranked, 45 skipped, 99.19% coverage, 517336 ms; ranks 1-5 contiguous; `quick_check=ok`, no foreign-key rows | Confirms the core runtime against current providers without accepting mismatched-date quotes; external AKShare/Eastmoney disconnects and two Tencent bulk omissions were isolated, with one fallback-derived result recorded as degraded rather than hidden. |
| 2026-07-24 | Protected online backup of the active 458 MiB SQLite database, followed by `SQLiteCache` initialization and `PRAGMA quick_check; PRAGMA foreign_key_check;` on the copy | Existing-database UTC-v2 upgrade path without modifying live data | backup manifest integrity `ok`; migration 7.79s; post-migration checks `ok` | Confirms the fixed-width timestamp and schema-migration rebuilds are practical and structurally valid on approximately 1.42 million daily-K rows. |
| 2026-07-27 | Runtime/dev/security `pip-audit --require-hashes --disable-pip`, clean wheel-only security-lock install, simulated CPython 3.12 `manylinux2014_x86_64` wheel resolution, `npm audit --audit-level=high`, two independent SBOM runs with recursive diff, and checksum-verified Gitleaks 8.30.1 scans | Isolated audit-tool installation, locked dependency vulnerabilities, reproducible CycloneDX output, and redacted current-source plus complete-history secret scanning | 47-package security environment installed with `pip check` clean; 50 Linux wheels resolved; no known Python vulnerabilities; 0 npm vulnerabilities; SBOM directories byte-identical; no Gitleaks findings | Confirms Security does not install optional provider SDKs and all local workflow gates pass against the final source and repository history. |
| 2026-07-27 | Identical clock/cache/migration/frontend-audit regression command under `TZ=UTC` and `TZ=Asia/Shanghai` | Host-timezone independence for business, persisted audit, import, diagnostics, and cache semantics | 91 passed and 4 subtests passed in both environments | Confirms the same focused boundary suite and result count under both supported host timezone settings. |
| 2026-07-24 | `$PYTHON tools/runtime_contract.py` | Installed runtime plus `.python-version`, `.node-version`, and `package.json` declaration consistency | passed on Python 3.12, Node 24.14.1, npm 11.11.0 | Confirms the current environment is inside the supported Python 3.12 / Node 22-or-24 / npm 10-or-11 contract. |
| 2026-07-24 | `git diff --check` plus targeted machine-path and common credential-pattern scans over the six changed engineering documents | Patch integrity and documentation privacy | passed; no findings | Confirms the changed Markdown contains no whitespace errors, machine-specific home paths, common API-token/private-key forms, UUID-shaped credential values, or user information. |
| 2026-07-22 | Node 22 and Node 24 runs of `$PYTHON -m pytest -q tests/test_frontend_notifications.py` | Deterministic notification polling, shared-lock coordination, cursor, retry, disable/re-enable, and delivery behavior across supported local/CI Node runtimes | 15 passed on Node 22.23.1; 15 passed on Node 24.14.1 | Confirms notification tests inject their browser lock dependency instead of accidentally borrowing a Node-version-specific global Web Locks implementation. |
| 2026-07-22 | `npm run test:e2e` | Full desktop and mobile browser regression, including full-market run/retry/recovery and multi-page notification coordination | 33 passed, 5 skipped in 47.4s | Confirms responsive workbench behavior, strict response contracts, request budgets, scan lifecycle ownership, online recovery, accessibility state, and cross-page alert de-duplication. |
| 2026-07-22 | `$PYTHON tools/api_inventory.py --check`, `$PYTHON tools/architecture_inventory.py --check`, then `$PYTHON -m pytest -q tests/test_tool_inventory_modules.py tests/test_config_modules.py` | Generated API/architecture references and documentation/configuration guardrails | passed; 55 tests passed in 11.48s | Confirms generated references match the final source tree and runtime settings remain documented without machine-specific paths or credentials. |
| 2026-07-27 | `$PYTHON -m ruff check --no-cache app tests tools`, `$PYTHON -m mypy`, both generated-inventory checks, and focused architecture/typing/supply-chain guards | Static analysis, expanded type scope, generated documentation, dependency direction, and supply-chain contracts | passed; mypy checked 74 source files; 29 focused tests passed | Confirms the final lint, type ratchet, generated references, acyclic boundaries, and workflow guardrails are clean. |
| 2026-07-19 | `npm run test:e2e` | Full desktop and mobile browser regression including scan failure recovery and online resynchronization | 28 passed, 4 skipped in 43.7s | Confirms strict response contracts, bounded one-timer polling, latest-run recovery, new-run pagination reset, responsive charts/layout, request budgets, and ARIA state. |
| 2026-07-19 | `$PYTHON -m pip check`, `$PYTHON -m ruff check app tests tools`, `$PYTHON -m mypy`, both generated-inventory checks, and `git diff --check` | Dependency, static-analysis, type, generated-document, and patch-integrity gates | passed; mypy checked 44 source files; 14 inventory guard tests passed | Matches the quality job gates and confirms current Node 24 GitHub Action pins, portable documentation, complete test indexing, and ignored runtime data. |
| 2026-07-18 | `npm run test:e2e` | Full desktop and mobile browser regression including the full-market background scan workspace | 24 passed, 4 skipped in 34.3s | Confirms immediate task progress, unpublished cancellation, derived-run retry, terminal degraded snapshots, bounded 100-row pagination, sorting/filters, keyboard scrolling, responsive layout, and existing workflows. |
| 2026-07-18 | `npm run test:e2e` | Desktop and mobile browser regression after alert/review/runtime changes | 20 passed, 4 skipped in 19.3s | Confirms request budgets, responsive layouts, chart inspection, local activity, timeline, queue, and stale-response behavior remain intact. |
| 2026-07-18 | `$PYTHON -m pytest -q <alert, review, scheduler, freshness, cleanup, notification, and inventory modules>` | Focused behavior and maintainability regressions | 180 passed in 13.22s; independent audit 132 passed | Confirms id-cursor retention, bounded notification backlog, partial degraded outcomes, plan deletion, metadata diagnostics, and generated-document guardrails. |
| 2026-07-17 | `$PYTHON -m pytest -q <provider runtime, quote, K-line, metadata, scheduler, sampling, lifecycle, and workbench modules>` | Provider admission, request-key single-flight, cancellation/orphan isolation, and downstream concurrency regressions | 363 passed in 3.67s | Confirms concurrent stocks no longer reject healthy foreground calls and true background calls remain bounded. |
| 2026-07-17 | `$PYTHON tools/api_inventory.py --check` and `$PYTHON tools/architecture_inventory.py --check` | Protected generated API/function references after regeneration | passed | Confirms both generated references match the final source tree. |
| 2026-07-17 | `npm run test:e2e` | Desktop and mobile browser regression | 20 passed, 4 skipped in 17.3s | Confirms core-first request order, request budgets, responsive layouts, chart inspection, local activity, timeline, queue, and stale-response behavior. |
| 2026-07-17 | `$PYTHON -m pytest -q -p no:cacheprovider <documentation and feature modules>` | Documentation index/config plus adjustment, review, scan, portability, backup, diagnostics, notifications, and workspace-preference regressions | 105 passed in 10.46s | Confirms the current documentation contracts and focused implemented-feature behavior. |
| 2026-07-16 | `$PYTHON -m pytest -q -p no:cacheprovider -k tool_inventory` | Documentation and generator guardrails | 13 passed | Historical guardrail result retained for traceability. |
| 2026-07-16 | `$PYTHON tools/api_inventory.py --check` and `$PYTHON tools/architecture_inventory.py --check` | Generated API/function references | passed | Both generated references match the current source tree. |
| 2026-07-16 | `npm run test:e2e` | Desktop and mobile browser regression | 20 passed, 4 skipped | Covers code/name search, exact chart inspection, local activity, request budgets, and existing desktop/mobile workflows. |

Final live-provider verification completed on 2026-07-22 against the current SH/SZ/BJ universe. The terminal derived run processed **5529/5529** symbols: **5491 success**, **0 missing**, and **38 skipped**, for **99.31% ranked coverage**. The skipped set was audited as 34 recent listings with fewer than the required 60 completed daily bars plus 4 possible suspensions: one stale daily sequence and three current BaoStock rows with zero volume plus unavailable/zero-volume Tencent quotes. No provider-chain outage was converted into bulk per-symbol missing data; three successful symbols retained explicit fallback or metadata degradation provenance. SQLite `quick_check` and `foreign_key_check` passed after the run.

## 7. Coverage Gaps

- Browser automation covers selected desktop/mobile workflows but has no full visual-regression baseline.
- There are no SSE load tests or provider timeout-cascade performance tests.
- SQLite compatibility helpers are covered, but there is no committed replay fixture for every historical database version.
- Route-level response contracts focus on high-risk paths rather than every stock report endpoint.
- Reliability math and persistence are covered with synthetic data, but no long-running representative dataset yet validates the provisional SLO targets or supports burn-rate alerting.
- Required CI remains hermetic; live provider canary behavior depends on external endpoints and is intentionally evidenced only by an explicit optional run.
- Supply-chain tests cover locks, audits, history scanning, immutable action references, and reproducible SBOMs, but there is no signed release artifact or generated/verified SLSA provenance.
