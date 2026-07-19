# Test Plan and Test Report

## 1. Test Objectives

- Verify stock lookup, quote/K-line acquisition, explicit daily adjustment provenance, analysis, research, and transparent degradation.
- Verify local SQLite state, schema compatibility, advice review history/as-of evaluation, current/custom historical scans, preview-claimed imports, runtime backup/cleanup protection, scheduler, and diagnostics.
- Verify browser request freshness, code/name autocomplete, exact chart inspection, local activity, review/scan reachability, notification retry, accessibility state, persistence isolation, and SSE behavior.
- Verify process-environment-only LLM configuration, dependency, security, generated-document, and maintainability gates.

## 2. Test Commands

```bash
export PROJECT_ROOT="${PROJECT_ROOT:-$HOME/AShareRadar}"
source "$PROJECT_ROOT/.venv/bin/activate"
export PYTHON="$PROJECT_ROOT/.venv/bin/python"
export PYTHONNOUSERSITE=1
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
$PYTHON -m ruff check app tests tools
$PYTHON -m mypy
npm run check:js
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing
npm ci && npx --no-install playwright install chromium && npm run test:e2e
```

The development lock contains runtime and engineering dependencies. Tests run with Python 3.12, `PYTHONNOUSERSITE=1`, temporary SQLite files, and fake providers/clients; the automated suite must not require credentials, persistent runtime data, live providers, or outbound network access.

## 3. Current Automated Coverage

The current test suite is split by domain:

- `tests/test_advice_reviews.py`: snapshot binding, no-lookahead windows, ambiguous barriers, pending/insufficient states, revision ownership, idempotent evaluation persistence, and cross-revision evaluation history.
- `tests/test_advice_review_window_contract.py`: trading-day completeness, 15:15 publication boundaries, cross-contract rejection, and weekend pending behavior.
- `tests/test_analysis_research.py`
- `tests/test_analysis_signal_modules.py`
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
- `tests/test_cache_freshness_modules.py`
- `tests/test_cache_stats_modules.py`
- `tests/test_chart_marks_modules.py`
- `tests/test_config_modules.py`
- `tests/test_container_settings_lifecycle.py`
- `tests/test_data_quality_modules.py`
- `tests/test_data_sources.py`
- `tests/test_datahub_cache_modules.py`
- `tests/test_datahub_klines_modules.py`
- `tests/test_datahub_metadata_modules.py`
- `tests/test_datahub_metadata_structure.py`: metadata facade compatibility, dependency acyclicity, module-size limits, and coverage/persistence use of the same normalized stock-pool set.
- `tests/test_datahub_orderbook_modules.py`
- `tests/test_datahub_quotes_modules.py`
- `tests/test_datahub_runtime_modules.py`: request-key sharing/admission/orphan isolation, bounded daemon executor ownership, queued-work cancellation, idempotent and cancellation-safe deferred close, active-worker/provider-client ordering, automatic post-quiescence cleanup, and stuck-SDK subprocess exit.
- `tests/test_datahub_source_plan_modules.py`
- `tests/test_datahub_status_modules.py`
- `tests/test_datahub_status_service_modules.py`
- `tests/test_db_mappers.py`
- `tests/test_fallback_logging.py`: allowlisted SQLite persistence-failure categories, stderr fallback visibility, and secret/raw-error suppression.
- `tests/test_financial_health_modules.py`
- `tests/test_financial_metrics_modules.py`
- `tests/test_frontend_advice_timeline.py`
- `tests/test_frontend_api_format_workbench.py`: fetch parsing for 204/205, zero-length/empty success payloads, structured errors, and workbench formatting.
- `tests/test_frontend_app_flow.py`: core-workbench-first cold-load order, stock/session orchestration, immediate advice-timeline loading ownership, A-B-A stale response rejection, SSE, persistence, companion request guards, and full browser-state/SSE refresh after user-data import.
- `tests/test_frontend_chart_inspector.py`: immutable daily/minute inspection snapshots, plot-boundary hit testing, CSS-to-canvas pointer mapping, keyboard traversal, redraw clamping, and listener cleanup.
- `tests/test_frontend_chart_context.py`: visible-window clipping, moving-average warm-up context, stable mark limits, and edge-aware non-overlapping mark labels.
- `tests/test_frontend_chart_workspace.py`: daily/minute chart controls, request budgets, stale safety, unavailable audit rows, and explicit unavailable handling for 204/empty/null or mismatched minute responses.
- `tests/test_frontend_diagnostics.py`
- `tests/test_frontend_local_activity_state.py`: notes/alert-event source synchronization, sanitized unavailable states, and stale/aborted read ownership.
- `tests/test_frontend_local_data.py`: latest file-read/import-preview/commit/cleanup-preview ownership, stale file/mode invalidation, server preview-token authority, rollback-backup feedback, portable download cleanup, and storage diagnostic rendering.
- `tests/test_frontend_local_data_security.py`: guarded POST semantics for local user-data export.
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
- `tests/test_market_scan_frontend.py`: split-module/version wiring, strict contracts, one-timer polling, bounded backoff, latest recovery, new-run pagination reset, online recovery, escaped rendering, and ARIA state.
- `tests/test_market_scan_modules.py`: full-universe/per-market accounting, bounded concurrency, structured fallback degradation, unified retry plans, lifecycle release, cancel/restart recovery, terminal `BUSY`/`LOCKED` retry, owned post-worker recovery, foreign-leader protection, publish-time guard, and automatic-run suppression.
- `tests/test_market_scan_repository.py`: transitions, batch invariants, atomic task creation/scan attachment and scan/task terminal writes, retry-copy rollback/concurrency guards, stable ranks, filters, immutable snapshots, structured degradation, idempotent repair, and active-run uniqueness.
- `tests/test_market_scan_repository_structure.py`: repository facade compatibility, dependency direction/acyclicity, and production module-size limits.
- `tests/test_market_scan_scoring.py`: deterministic versioned score, completed-`data_date` snapshot boundary, quote/K-line date and adjustment contracts, required liquidity inputs, missing/skip boundaries, and A-share-only universe tags/exclusions.
- `tests/test_minute_analysis_modules.py`
- `tests/test_optional_kline_parsing_modules.py`
- `tests/test_optional_provider_concurrency.py`
- `tests/test_provider_errors_modules.py`
- `tests/test_provider_failure_status_modules.py`
- `tests/test_provider_registry_modules.py`
- `tests/test_provider_status_aggregation_modules.py`
- `tests/test_provider_status_repository_modules.py`
- `tests/test_provider_utils_modules.py`
- `tests/test_quote_stream_modules.py`
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
- `tests/test_static_assets.py`
- `tests/test_stock_abnormal_events.py`
- `tests/test_stock_activity_modules.py`
- `tests/test_stock_analysis_modules.py`
- `tests/test_stock_event_summary.py`
- `tests/test_stock_lhb_modules.py`
- `tests/test_stock_lookup_modules.py`
- `tests/test_stock_overview_modules.py`
- `tests/test_stock_rule_modules.py`
- `tests/test_stock_strategy_modules.py`
- `tests/test_symbol_modules.py`
- `tests/test_system_diagnostics_modules.py`
- `tests/test_tencent_provider_modules.py`
- `tests/test_tool_inventory_modules.py`: generated-document drift, test-plan completeness, machine-path guards, dependency layering, immutable action SHA pins, and Node 24 action-major guards.
- `tests/test_trading_calendar_modules.py`
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

1. Start the app on `127.0.0.1:8010` with the documented single-worker `--timeout-graceful-shutdown 5` command and confirm `/api/health` succeeds; with an SSE stream open, stop it and confirm the listener exits within the bounded shutdown window.
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
20. On a disposable fake-provider run, include SH/SZ/BJ, ST/new, suspended, short-history, and failed-source rows. Confirm every eligible symbol becomes success/missing/skipped, only successful rows receive stable ranks, coverage matches counts, and page/filter/sort views never render the whole universe.
21. Simulate a deleted active scan and repeated poll failures; confirm the UI returns to latest with bounded backoff, resets page/results when a different run appears, retries immediately after `online`, keeps one request/timer at a time, and announces progress/result milestones through one live region.
22. With disposable dual processes, confirm only one owns `<SQLite path>.runtime-leader.lock`, scheduler and scanner never split ownership, and standby takes over both after the leader exits. Confirm restore refuses the unified lock and both legacy compatibility locks.
23. Confirm invalid symbols stay in the query panel, while a failed valid-stock load clears the prior stock content and shows an explicit failure; then check desktop/mobile layouts for console errors.

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

Stock-search requests are not part of the four-request stock-switch baseline: only a debounced, uncached, non-complete user query may trigger one. A selected suggestion then follows the ordinary stock-load budget. The latest recorded desktop/mobile browser matrix is **28 passed, 4 skipped**; the skipped cases are intentional device-exclusive scenarios recorded by Playwright rather than treated as passes.

Regression-sensitive boundaries include core-workbench-first cold-load dispatch under browser connection limits, equal-event quote quality priority and legacy UTC ordering, `qfq`/legacy K-line isolation, quote/minute session freshness, provider request-key single-flight/orphan isolation plus daemon-worker process exit, the 15:15 daily publish threshold, full-market `data_date` snapshots, atomic task attachment and scan/task terminals, owned terminal-failure recovery, structured degradation and unified retry plans, normalized atomic stock-pool replacement, quiescence-delayed single runtime leadership and whole-service takeover, set-based retention with retry-lineage convergence, cross-process backup leases and explicit rotation/restore guards, scan contract/backoff/latest recovery and ARIA state, server-current-time handling for today's review/scan, rendered-symbol ownership, single-use import previews, latest-owner browser state, serialized backup-before-import, automatic user-history exclusion, review-linked retention, successful write reconciliation, comparable-change unread watermarks, notification cursor advancement, stale companion responses, immutable Node 24 action pins, and request-budget drift.

## 6. Latest Test Report

The first rows below audit the current shared worktree; older records remain for traceability. The current verification used an isolated locked Python environment, fake providers, temporary databases, and no credentials or live-provider dependency.

| Date | Worktree State | Environment | Command | Scope | Result | Notes |
| --- | --- | --- | --- | --- | --- | --- |
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

## 7. Coverage Gaps

- Browser automation covers selected desktop/mobile workflows but has no full visual-regression baseline.
- There are no SSE load tests or provider timeout-cascade performance tests.
- SQLite compatibility helpers are covered, but there is no committed replay fixture for every historical database version.
- Route-level response contracts focus on high-risk paths rather than every stock report endpoint.
