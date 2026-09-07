# 测试计划与当前验收

## 1. 验证目标

覆盖实际用户入口与失败边界：行情身份/时序、扫描封存及评分回放、来源与研究资格、复盘修订、SQLite 迁移/备份/保留、异步取消与并发、前端过期响应/草稿/事件释放。删除旧接口时迁移行为测试，移除仅证明别名相等的测试。

## 2. 交付命令

在项目根目录使用 Python 3.12，所有单元测试使用临时 SQLite 和替代提供者，不依赖真实账户、运行数据库或外网。

```bash
export PYTHON="$PWD/.venv/bin/python"
export PYTHONNOUSERSITE=1
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
npm ci
$PYTHON tools/runtime_contract.py
$PYTHON -m pip check
$PYTHON -m ruff check app tests tools
$PYTHON -m mypy
npm run check:py
npm run check:js
$PYTHON tools/check_repository.py
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
$PYTHON -m pytest -q -p no:cacheprovider --cov=app --cov=tools --cov-report=term-missing
npx --no-install playwright install chromium firefox webkit
npm run test:e2e
```

原有分支与行综合覆盖率门槛为 90%。生产函数不超过 60 行/12 分支；类型显式清单、依赖方向、导入环、文档链接和参考文档漂移均受检查。`npm run check` 聚合运行环境、编译/Pyflakes、JS 语法、仓库一致性与全量 pytest；交付仍须单独执行 Mypy、Ruff、覆盖率及浏览器项目。

CI 还运行 Shanghai 时区回归、Node 24 smoke 与安全工作流。锁审计、npm audit、历史秘密扫描及可复现 SBOM 命令见[运行手册](OPERATIONS.md)。在线 provider canary 为独立诊断，不作为离线通过的依据。

## 3. 当前测试索引

The current test suite is split by domain:

- `tests/test_active_research_review_backend.py`
- `tests/test_advice_review_window_contract.py`
- `tests/test_advice_reviews.py`
- `tests/test_akshare_stock_metadata.py`
- `tests/test_alerts_unavailable_state.py`
- `tests/test_analysis_research.py`
- `tests/test_analysis_signal_modules.py`
- `tests/test_api_alert_routes.py`
- `tests/test_api_container_modules.py`
- `tests/test_api_data_routes.py`
- `tests/test_api_error_modules.py`
- `tests/test_api_local_data_routes.py`
- `tests/test_api_monitoring_routes.py`
- `tests/test_api_notes_routes.py`
- `tests/test_api_paper_trading_routes.py`
- `tests/test_api_review_routes.py`
- `tests/test_api_security_modules.py`
- `tests/test_api_stock_routes.py`
- `tests/test_api_strategy_lab_routes.py`
- `tests/test_api_watchlist_research_queue.py`
- `tests/test_api_watchlist_routes.py`
- `tests/test_app_lifecycle_integration.py`
- `tests/test_architecture_boundaries.py`
- `tests/test_artifact_io.py`
- `tests/test_audit_epoch_repositories.py`
- `tests/test_cache_freshness_modules.py`
- `tests/test_cache_migration_guard.py`
- `tests/test_cache_stats_modules.py`
- `tests/test_chart_marks_modules.py`
- `tests/test_choice_experimental_history.py`
- `tests/test_choice_history_comparison.py`
- `tests/test_choice_research.py`
- `tests/test_choice_research_audit.py`
- `tests/test_clock_modules.py`
- `tests/test_config_modules.py`
- `tests/test_container_settings_lifecycle.py`
- `tests/test_daemon_executor.py`
- `tests/test_daemon_executor_retention.py`
- `tests/test_data_quality_modules.py`
- `tests/test_data_sources.py`
- `tests/test_datahub_cache_modules.py`
- `tests/test_datahub_klines_modules.py`
- `tests/test_datahub_metadata_modules.py`
- `tests/test_datahub_metadata_structure.py`
- `tests/test_datahub_orderbook_modules.py`
- `tests/test_datahub_quotes_modules.py`
- `tests/test_datahub_runtime_cancellation_isolation.py`
- `tests/test_datahub_runtime_modules.py`
- `tests/test_datahub_source_plan_modules.py`
- `tests/test_datahub_status_modules.py`
- `tests/test_datahub_status_service_modules.py`
- `tests/test_discovery_api.py`
- `tests/test_discovery_portability.py`
- `tests/test_discovery_presets.py`
- `tests/test_discovery_rank_changes.py`
- `tests/test_exception_safety.py`
- `tests/test_exchange_calendar_contract.py`
- `tests/test_experimental_direction_probability.py`
- `tests/test_experimental_direction_validation.py`
- `tests/test_experimental_probability.py`
- `tests/test_experimental_probability_frontend.py`
- `tests/test_experimental_probability_process.py`
- `tests/test_fallback_logging.py`
- `tests/test_financial_health_modules.py`
- `tests/test_financial_metrics_modules.py`
- `tests/test_frontend_advice_timeline.py`
- `tests/test_frontend_api_format_workbench.py`
- `tests/test_frontend_app_flow.py`
- `tests/test_frontend_app_lifecycle.py`
- `tests/test_frontend_audit_time.py`
- `tests/test_frontend_chart_context.py`
- `tests/test_frontend_chart_inspector.py`
- `tests/test_frontend_chart_workspace.py`
- `tests/test_frontend_diagnostics.py`
- `tests/test_frontend_discovery.py`
- `tests/test_frontend_event_bindings.py`
- `tests/test_frontend_individual_probability.py`
- `tests/test_frontend_local_activity_state.py`
- `tests/test_frontend_local_data.py`
- `tests/test_frontend_local_data_security.py`
- `tests/test_frontend_market_scan_executable_shadow.py`
- `tests/test_frontend_market_scan_future_range.py`
- `tests/test_frontend_market_scan_history_loading.py`
- `tests/test_frontend_market_scan_reliability.py`
- `tests/test_frontend_market_scan_screening.py`
- `tests/test_frontend_notes_alerts_requests.py`
- `tests/test_frontend_notifications.py`
- `tests/test_frontend_paper_request_ownership.py`
- `tests/test_frontend_paper_trading.py`
- `tests/test_frontend_research_activity.py`
- `tests/test_frontend_research_panels.py`
- `tests/test_frontend_review_drafts.py`
- `tests/test_frontend_review_scan.py`
- `tests/test_frontend_stock_search.py`
- `tests/test_frontend_stock_search_history.py`
- `tests/test_frontend_strategy_templates.py`
- `tests/test_frontend_watchlist_requests.py`
- `tests/test_frontend_workbench_contracts.py`
- `tests/test_frontend_workspace_preferences.py`
- `tests/test_futu_provider_modules.py`
- `tests/test_indicator_levels_modules.py`
- `tests/test_indicator_trend_modules.py`
- `tests/test_indicator_volume_modules.py`
- `tests/test_individual_probability.py`
- `tests/test_individual_probability_data_boundary.py`
- `tests/test_individual_probability_store_failures.py`
- `tests/test_individual_workflow_modules.py`
- `tests/test_javascript_syntax_gate.py`
- `tests/test_joint_execution_probability_contract.py`
- `tests/test_joint_execution_probability_v3_contract.py`
- `tests/test_kline_contract.py`
- `tests/test_leader_scoring_modules.py`
- `tests/test_llm_explainer.py`
- `tests/test_llm_score_semantics.py`
- `tests/test_local_data_portability.py`
- `tests/test_local_lifecycle.py`
- `tests/test_market_overview_modules.py`
- `tests/test_market_quotes_modules.py`
- `tests/test_market_sampling_modules.py`
- `tests/test_market_scan_allocation.py`
- `tests/test_market_scan_allocation_cli.py`
- `tests/test_market_scan_api.py`
- `tests/test_market_scan_architecture.py`
- `tests/test_market_scan_artifact_lease.py`
- `tests/test_market_scan_automation.py`
- `tests/test_market_scan_cohort_feedback.py`
- `tests/test_market_scan_cohort_feedback_cli.py`
- `tests/test_market_scan_delayed_feedback.py`
- `tests/test_market_scan_delayed_feedback_cli.py`
- `tests/test_market_scan_delta.py`
- `tests/test_market_scan_evaluation.py`
- `tests/test_market_scan_evaluation_config.py`
- `tests/test_market_scan_evaluation_execution_contract.py`
- `tests/test_market_scan_evaluation_price_basis.py`
- `tests/test_market_scan_evaluation_time_inference.py`
- `tests/test_market_scan_execution.py`
- `tests/test_market_scan_execution_quote.py`
- `tests/test_market_scan_export.py`
- `tests/test_market_scan_factor_inference.py`
- `tests/test_market_scan_failure_isolation.py`
- `tests/test_market_scan_feature_windows.py`
- `tests/test_market_scan_forward_source_admission.py`
- `tests/test_market_scan_frontend.py`
- `tests/test_market_scan_frontier_cli.py`
- `tests/test_market_scan_future_range.py`
- `tests/test_market_scan_future_range_artifact.py`
- `tests/test_market_scan_future_range_store.py`
- `tests/test_market_scan_input_admission.py`
- `tests/test_market_scan_invalid_completed_bars.py`
- `tests/test_market_scan_joint_execution_fit.py`
- `tests/test_market_scan_joint_execution_maintenance_contract.py`
- `tests/test_market_scan_lifecycle.py`
- `tests/test_market_scan_modes.py`
- `tests/test_market_scan_multiple_testing_dependence.py`
- `tests/test_market_scan_official_execution.py`
- `tests/test_market_scan_performance.py`
- `tests/test_market_scan_pipeline.py`
- `tests/test_market_scan_pressure.py`
- `tests/test_market_scan_probability.py`
- `tests/test_market_scan_probability_artifact.py`
- `tests/test_market_scan_probability_capture.py`
- `tests/test_market_scan_probability_coherence.py`
- `tests/test_market_scan_probability_historical_context.py`
- `tests/test_market_scan_probability_history.py`
- `tests/test_market_scan_probability_label_session_gaps.py`
- `tests/test_market_scan_probability_labels.py`
- `tests/test_market_scan_probability_maintenance.py`
- `tests/test_market_scan_probability_outcomes.py`
- `tests/test_market_scan_probability_preload_process.py`
- `tests/test_market_scan_probability_ranking.py`
- `tests/test_market_scan_probability_replay.py`
- `tests/test_market_scan_probability_source.py`
- `tests/test_market_scan_probability_source_research.py`
- `tests/test_market_scan_prospective.py`
- `tests/test_market_scan_purchase_money.py`
- `tests/test_market_scan_query_integrity.py`
- `tests/test_market_scan_query_service.py`
- `tests/test_market_scan_quote_timezone.py`
- `tests/test_market_scan_raw_score_replay.py`
- `tests/test_market_scan_release_boundary_coverage.py`
- `tests/test_market_scan_repository.py`
- `tests/test_market_scan_repository_structure.py`
- `tests/test_market_scan_research_admission_regressions.py`
- `tests/test_market_scan_research_availability.py`
- `tests/test_market_scan_research_availability_database.py`
- `tests/test_market_scan_research_capacity.py`
- `tests/test_market_scan_research_challengers.py`
- `tests/test_market_scan_research_cli_recovery.py`
- `tests/test_market_scan_research_cli_split_binding.py`
- `tests/test_market_scan_research_execution_audit.py`
- `tests/test_market_scan_research_execution_lease.py`
- `tests/test_market_scan_research_holdings.py`
- `tests/test_market_scan_research_holdings_exit_policy.py`
- `tests/test_market_scan_research_incremental_comparison.py`
- `tests/test_market_scan_research_input_snapshot_transaction.py`
- `tests/test_market_scan_research_inputs.py`
- `tests/test_market_scan_research_output_isolation.py`
- `tests/test_market_scan_research_portfolio.py`
- `tests/test_market_scan_research_primary_objective.py`
- `tests/test_market_scan_research_runner.py`
- `tests/test_market_scan_research_sensitivity.py`
- `tests/test_market_scan_research_unfilled_admission.py`
- `tests/test_market_scan_retention.py`
- `tests/test_market_scan_retry.py`
- `tests/test_market_scan_scheduler.py`
- `tests/test_market_scan_scoring.py`
- `tests/test_market_scan_screen_alert.py`
- `tests/test_market_scan_screening.py`
- `tests/test_market_scan_sensitivity_cli.py`
- `tests/test_market_scan_shadow_scoring.py`
- `tests/test_market_scan_skip_contract.py`
- `tests/test_market_scan_snapshot_integrity.py`
- `tests/test_market_scan_terminal_recovery.py`
- `tests/test_market_scan_trial_registry.py`
- `tests/test_market_scan_trial_registry_clock_recovery.py`
- `tests/test_market_scan_trial_registry_lock_cleanup.py`
- `tests/test_market_scan_trust_contract.py`
- `tests/test_market_scan_universe.py`
- `tests/test_market_scan_validation.py`
- `tests/test_market_strategy_templates.py`
- `tests/test_minute_analysis_modules.py`
- `tests/test_optional_kline_parsing_modules.py`
- `tests/test_optional_provider_concurrency.py`
- `tests/test_paper_trading.py`
- `tests/test_paper_trading_metrics.py`
- `tests/test_paper_trading_schema.py`
- `tests/test_provider_canary.py`
- `tests/test_provider_errors_modules.py`
- `tests/test_provider_failure_status_modules.py`
- `tests/test_provider_priority_settings.py`
- `tests/test_provider_registry_modules.py`
- `tests/test_provider_status_aggregation_modules.py`
- `tests/test_provider_status_repository_modules.py`
- `tests/test_provider_utils_modules.py`
- `tests/test_quote_stream_modules.py`
- `tests/test_reliability_modules.py`
- `tests/test_repository_consistency.py`
- `tests/test_research_alpha_modules.py`
- `tests/test_research_artifact_catalog.py`
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
- `tests/test_review_paper_boundary_coverage.py`
- `tests/test_rules_alerts.py`
- `tests/test_run_market_scan_research_cli.py`
- `tests/test_runtime_artifact_recheck.py`
- `tests/test_runtime_backup.py`
- `tests/test_runtime_coordinator.py`
- `tests/test_runtime_coordinator_cancellation.py`
- `tests/test_runtime_environment_modules.py`
- `tests/test_runtime_maintenance_lock_isolation.py`
- `tests/test_runtime_maintenance_regressions.py`
- `tests/test_scheduler_lifecycle_cancellation.py`
- `tests/test_scheduler_modules.py`
- `tests/test_scheduler_structure.py`
- `tests/test_schema_compat.py`
- `tests/test_scoring_modules.py`
- `tests/test_sina_client.py`
- `tests/test_static_assets.py`
- `tests/test_stock_abnormal_events.py`
- `tests/test_stock_activity_modules.py`
- `tests/test_stock_analysis_modules.py`
- `tests/test_stock_event_summary.py`
- `tests/test_stock_lhb_modules.py`
- `tests/test_stock_lookup_modules.py`
- `tests/test_stock_overview_modules.py`
- `tests/test_stock_pool_industry_enrichment.py`
- `tests/test_stock_pool_metadata.py`
- `tests/test_stock_rule_modules.py`
- `tests/test_stock_strategy_modules.py`
- `tests/test_strategy_automation.py`
- `tests/test_strategy_evidence.py`
- `tests/test_strategy_execution.py`
- `tests/test_strategy_execution_schema.py`
- `tests/test_strategy_lab.py`
- `tests/test_strategy_natural_language_contract.py`
- `tests/test_supply_chain.py`
- `tests/test_symbol_modules.py`
- `tests/test_system_diagnostics_modules.py`
- `tests/test_tencent_provider_modules.py`
- `tests/test_tool_inventory_modules.py`
- `tests/test_trading_calendar_modules.py`
- `tests/test_typing_contract.py`
- `tests/test_uvicorn_smoke.py`
- `tests/test_valuation_modules.py`
- `tests/test_watchlist_research_queue.py`
- `tests/test_watchlist_scan.py`
- `tests/test_workbench_context_cache_modules.py`
- `tests/test_workbench_pipeline_modules.py`

## 4. 浏览器与操作验收

浏览器回归使用静态测试服务器和 mock API，覆盖 Chromium 桌面/移动端、Firefox、WebKit。确认读取过期保护、独立写入、失败草稿、复盘事件、重复绑定、按钮禁用与键盘/ARIA 行为；跳过项按 Playwright 项目限制解释，不报告成通过。

人工操作仅在需要连接真实运行实例时执行：健康检查、有效个股加载、切换图表、创建笔记、预警测试、查看冻结扫描及导出、查看复盘、备份验证。不要为验证结构重构自动清空数据库或触发真实扫描。

## 5. 请求预算

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
| Individual D+2/D+3/D+4 probability | 1 companion request per cold stock load/switch; 1 only on explicit retry; no provider or request-time fit |
| Fixed-condition watchlist scan | 1 |
| Opening the full-market workspace | 1 global latest-task request plus 1 selected-mode history request; if the task is absent/active/failed/other-mode, at most 1 selected-mode latest-published request; then 1 result request for the chosen published run |
| Active full-market scan | 1 progress request per 2-second poll; no overlapping poll |
| Probability source capture pending | 1 same-run request per polling interval; at most 60 attempts including failures; stop on terminal state or run transition |
| Full-market result page/filter change | 1 request, capped at 100 rendered rows |
| Trustworthy screening workbench | 0 while collapsed; up to 3 independent frozen-read requests on first expansion or explicit refresh (`breadth`, `screen/evaluate`, `delta`); an active probability threshold rejects evaluation locally, so only 2 are sent |
| Opening future-range evidence | 0 while collapsed; 1 request on first expansion, then 1 per D+ offset, exact-symbol, or 20-row page change; follow-up pages set `include_research=false` |
| Executable-candidate Shadow | 0 on workspace/Strategy Lab activation or use-current; 1 only per explicit submit; cancel/run change/lab close/page hide aborts or suppresses stale work |
| Strategy execution candidate page/sort change | 1 request, 50 rows in the browser and hard API cap 200 |
| Strategy evidence refresh | 1 compact offline-artifact request; no provider or cross-date evaluator |
| Full-market Excel export | 1 request for the complete current filtered snapshot; no provider refresh or score recomputation |
| Expand/collapse frozen scan evidence | 0; persisted row fields only |
| Apply one discovery preset | 1 leaderboard request plus 1 bounded rank-change request |
| Record one saved-screen change event | 1 explicit idempotent write request; no provider or scoring request |
| Enqueue all filtered discovery rows | 1 leaderboard request per page, then 1 queue request per 100 unique symbols |
| Advice-review evaluation | 1 |
| Expanding one advice-review history for the first time | 1 |
| Opening the global review dashboard | 3 independent no-store branches: 2 fixed reads (`summary`, `due`) plus at least 1 plan-list page per 100 active plans, capped at 100 pages; one branch failure must not suppress the other two |
| Paper dashboard / explicit simulation | 1 read on activation or refresh; 1 write only per explicit run; no broker request |
| Each Tools-tab cleanup preview | 1 |
| Enabling browser notifications | 1 immediate baseline page; later 30-second polls use as many 50-event keyset pages as needed, capped at 200 pages |


预算按实际流量和当前测试维护；跨页面副作用、重复监听器和轮询重入必须有行为回归，不能只检查代码字符串。

## 6. 当前验收结果

| Date | Worktree State | Environment | Command | Scope | Result | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-07 | 全仓维护重构，970 个源码/配置/夹具文件已冻结 | Python 3.12 / Node 24.14.1 / npm 11.11.0 | `pytest --cov=app --cov=tools` | 全量 Python，807.74 秒 | 6292 passed；68 subtests passed；覆盖率 90.71% | 保持原有 90% 门槛 |
| 2026-09-07 | 同一冻结源码 | Chromium 桌面/移动端、Firefox、WebKit | `npm run test:e2e` | 完整 4 项目矩阵 | 167 passed；49 skipped | 跳过项沿用既有项目条件 |
| 2026-09-07 | 同一冻结源码 | Python / Node | Ruff、Mypy、运行环境、编译、Pyflakes、JS、仓库、文档、依赖检查 | 350 个 Mypy 文件；119 个 JS；105 项工程契约测试 | 全部通过 | 323 个应用文件的类型覆盖下限未降低 |

本次源码清单摘要：`f6e572ab15cb01178b29fa793b8ab6f823f86aac029f312cef13a6cb2b24d234`。Python 与 JavaScript 入口依赖图没有孤立模块；9158 个生产函数全部满足 60 行/12 分支门槛。

日志与覆盖率产物保存在本轮验证目录，当前结果和范围写在此表。过期定向跑数不作为当前源码的通过依据。

## 7. 验证限制

本轮没有升级依赖，也没有查询在线漏洞库；依赖一致性与供应链工程测试已执行。运行服务没有重启，真实数据库和用户研究产物未修改。

自动化使用替代提供者和本地数据；不证明实时服务可用、来源授权有效或研究收益具有样本外经济价值。SDK 中不可强制结束的线程需提供者级超时与运行边界共同处理。
