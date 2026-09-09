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
npm ls --depth=0
npx --no-install playwright --version
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

Python `app/` 与 `tools/` 的分支和行综合覆盖率门槛为 90%；JavaScript 不计入该数字，使用独立语法、Node行为与浏览器回归。Python 生产函数不超过 60 行、12 个 AST 分支点；前端生产 JavaScript 函数严格少于 60 行、20 个分支点，入口与 E2E 文件另受逐文件增长预算约束。类型显式清单、依赖方向、导入环、文档链接和参考文档漂移均受检查。`npm run check` 聚合运行环境、编译/Pyflakes、JS 语法、仓库一致性与全量 pytest；交付仍须单独执行 Mypy、Ruff、覆盖率及浏览器项目。

CI 还运行 Shanghai 时区回归、Node 24 smoke 与安全工作流。锁审计、npm audit、历史秘密扫描及可复现 SBOM 命令见[运行手册](OPERATIONS.md)。在线 provider canary 为独立诊断，不作为离线通过的依据。

## 3. 当前测试索引

The current test suite is split by domain:

- `tests/test_active_research_review_backend.py`
- `tests/test_advice_review_window_contract.py`
- `tests/test_advice_reviews.py`
- `tests/test_akshare_stock_metadata.py`
- `tests/test_alert_notification_feed.py`
- `tests/test_alert_rule_mutation_transactions.py`
- `tests/test_alert_stream_persistence.py`
- `tests/test_alerts_unavailable_state.py`
- `tests/test_analysis_hard_quality_admission.py`
- `tests/test_analysis_research.py`
- `tests/test_analysis_signal_modules.py`
- `tests/test_api_alert_routes.py`
- `tests/test_api_container_modules.py`
- `tests/test_api_data_routes.py`
- `tests/test_api_error_modules.py`
- `tests/test_api_inventory_dependencies.py`
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
- `tests/test_chart_note_audit_dates.py`
- `tests/test_choice_experimental_history.py`
- `tests/test_choice_history_comparison.py`
- `tests/test_choice_research.py`
- `tests/test_choice_research_audit.py`
- `tests/test_clock_modules.py`
- `tests/test_config_modules.py`
- `tests/test_container_settings_lifecycle.py`
- `tests/test_daemon_executor.py`
- `tests/test_daemon_executor_retention.py`
- `tests/test_daily_kline_duplicate_admission.py`
- `tests/test_data_quality_modules.py`
- `tests/test_data_sources.py`
- `tests/test_datahub_cache_modules.py`
- `tests/test_datahub_klines_modules.py`
- `tests/test_datahub_metadata_modules.py`
- `tests/test_datahub_metadata_structure.py`
- `tests/test_datahub_orderbook_modules.py`
- `tests/test_datahub_quotes_modules.py`
- `tests/test_datahub_request_rejoin.py`
- `tests/test_datahub_requested_cache_coverage.py`
- `tests/test_datahub_runtime_cancellation_isolation.py`
- `tests/test_datahub_runtime_modules.py`
- `tests/test_datahub_source_plan_modules.py`
- `tests/test_datahub_status_modules.py`
- `tests/test_datahub_status_service_modules.py`
- `tests/test_discovery_api.py`
- `tests/test_discovery_enqueue_retry_effects.py`
- `tests/test_discovery_portability.py`
- `tests/test_discovery_presets.py`
- `tests/test_discovery_rank_changes.py`
- `tests/test_due_review_frontend_contract.py`
- `tests/test_due_review_queue_pagination.py`
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
- `tests/test_frontend_cache_clock.py`
- `tests/test_frontend_chart_context.py`
- `tests/test_frontend_chart_inspector.py`
- `tests/test_frontend_chart_workspace.py`
- `tests/test_frontend_creation_draft_ownership.py`
- `tests/test_frontend_diagnostics.py`
- `tests/test_frontend_discovery.py`
- `tests/test_frontend_discovery_filter_roundtrip.py`
- `tests/test_frontend_discovery_preset_management.py`
- `tests/test_frontend_discovery_screen_alerts.py`
- `tests/test_frontend_event_bindings.py`
- `tests/test_frontend_individual_probability.py`
- `tests/test_frontend_local_activity_state.py`
- `tests/test_frontend_local_data.py`
- `tests/test_frontend_local_data_security.py`
- `tests/test_frontend_market_scan_executable_shadow.py`
- `tests/test_frontend_market_scan_future_range.py`
- `tests/test_frontend_market_scan_history_loading.py`
- `tests/test_frontend_market_scan_history_pagination.py`
- `tests/test_frontend_market_scan_reliability.py`
- `tests/test_frontend_market_scan_screening.py`
- `tests/test_frontend_note_commit_recovery.py`
- `tests/test_frontend_note_revision.py`
- `tests/test_frontend_notes_alerts_requests.py`
- `tests/test_frontend_notification_navigation.py`
- `tests/test_frontend_notification_request_ownership.py`
- `tests/test_frontend_notifications.py`
- `tests/test_frontend_paper_account_permissions.py`
- `tests/test_frontend_paper_request_ownership.py`
- `tests/test_frontend_paper_trading.py`
- `tests/test_frontend_qa_cancellation.py`
- `tests/test_frontend_research_activity.py`
- `tests/test_frontend_research_panels.py`
- `tests/test_frontend_review_drafts.py`
- `tests/test_frontend_review_due_queue.py`
- `tests/test_frontend_review_pagination_recovery.py`
- `tests/test_frontend_review_plan_navigation.py`
- `tests/test_frontend_review_save_ownership.py`
- `tests/test_frontend_review_scan.py`
- `tests/test_frontend_stock_search.py`
- `tests/test_frontend_stock_search_history.py`
- `tests/test_frontend_strategy_draft_identity.py`
- `tests/test_frontend_strategy_history.py`
- `tests/test_frontend_strategy_recovery.py`
- `tests/test_frontend_strategy_schedule_management.py`
- `tests/test_frontend_strategy_templates.py`
- `tests/test_frontend_tools_recovery.py`
- `tests/test_frontend_watchlist_navigation.py`
- `tests/test_frontend_watchlist_queue_view.py`
- `tests/test_frontend_watchlist_requests.py`
- `tests/test_frontend_workbench_contracts.py`
- `tests/test_frontend_workspace_preferences.py`
- `tests/test_futu_provider_modules.py`
- `tests/test_historical_replay_estimator_binding.py`
- `tests/test_indicator_atr_window.py`
- `tests/test_indicator_levels_modules.py`
- `tests/test_indicator_trend_context.py`
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
- `tests/test_kline_execution_persistence.py`
- `tests/test_leader_scoring_modules.py`
- `tests/test_llm_explainer.py`
- `tests/test_llm_score_semantics.py`
- `tests/test_local_data_connection_lifetime.py`
- `tests/test_local_data_portability.py`
- `tests/test_local_lifecycle.py`
- `tests/test_market_data_numeric_fastpath.py`
- `tests/test_market_overview_modules.py`
- `tests/test_market_quotes_modules.py`
- `tests/test_market_sampling_modules.py`
- `tests/test_market_scan_allocation.py`
- `tests/test_market_scan_allocation_cli.py`
- `tests/test_market_scan_api.py`
- `tests/test_market_scan_architecture.py`
- `tests/test_market_scan_artifact_lease.py`
- `tests/test_market_scan_automation.py`
- `tests/test_market_scan_benchmark_equivalence.py`
- `tests/test_market_scan_cohort_feedback.py`
- `tests/test_market_scan_cohort_feedback_cli.py`
- `tests/test_market_scan_completed_snapshot_time.py`
- `tests/test_market_scan_completed_snapshot_trend.py`
- `tests/test_market_scan_delayed_feedback.py`
- `tests/test_market_scan_delayed_feedback_cli.py`
- `tests/test_market_scan_delta.py`
- `tests/test_market_scan_dimension_version_migration.py`
- `tests/test_market_scan_evaluation.py`
- `tests/test_market_scan_evaluation_config.py`
- `tests/test_market_scan_evaluation_execution_contract.py`
- `tests/test_market_scan_evaluation_execution_evidence.py`
- `tests/test_market_scan_evaluation_price_basis.py`
- `tests/test_market_scan_evaluation_time_inference.py`
- `tests/test_market_scan_execution.py`
- `tests/test_market_scan_execution_phase.py`
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
- `tests/test_market_scan_future_range_execution_evidence.py`
- `tests/test_market_scan_future_range_store.py`
- `tests/test_market_scan_holdings_cash_ledger.py`
- `tests/test_market_scan_input_admission.py`
- `tests/test_market_scan_invalid_completed_bars.py`
- `tests/test_market_scan_joint_execution_calendar_replay.py`
- `tests/test_market_scan_joint_execution_fit.py`
- `tests/test_market_scan_joint_execution_maintenance_contract.py`
- `tests/test_market_scan_keyword_admission.py`
- `tests/test_market_scan_lifecycle.py`
- `tests/test_market_scan_modes.py`
- `tests/test_market_scan_multiple_testing_dependence.py`
- `tests/test_market_scan_official_execution.py`
- `tests/test_market_scan_performance.py`
- `tests/test_market_scan_pipeline.py`
- `tests/test_market_scan_pressure.py`
- `tests/test_market_scan_previous_close_admission.py`
- `tests/test_market_scan_probability.py`
- `tests/test_market_scan_probability_artifact.py`
- `tests/test_market_scan_probability_capture.py`
- `tests/test_market_scan_probability_coherence.py`
- `tests/test_market_scan_probability_historical_context.py`
- `tests/test_market_scan_probability_history.py`
- `tests/test_market_scan_probability_label_admission.py`
- `tests/test_market_scan_probability_label_session_gaps.py`
- `tests/test_market_scan_probability_labels.py`
- `tests/test_market_scan_probability_maintenance.py`
- `tests/test_market_scan_probability_outcomes.py`
- `tests/test_market_scan_probability_preload_process.py`
- `tests/test_market_scan_probability_ranking.py`
- `tests/test_market_scan_probability_ranking_inference.py`
- `tests/test_market_scan_probability_replay.py`
- `tests/test_market_scan_probability_source.py`
- `tests/test_market_scan_probability_source_research.py`
- `tests/test_market_scan_prospective.py`
- `tests/test_market_scan_provider_bar_admission.py`
- `tests/test_market_scan_purchase_money.py`
- `tests/test_market_scan_purchase_search.py`
- `tests/test_market_scan_query_integrity.py`
- `tests/test_market_scan_query_service.py`
- `tests/test_market_scan_quote_timezone.py`
- `tests/test_market_scan_ranked_page_serialization.py`
- `tests/test_market_scan_ranking_maintenance_snapshot.py`
- `tests/test_market_scan_raw_score_replay.py`
- `tests/test_market_scan_read_shutdown.py`
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
- `tests/test_market_scan_screen_alert_history.py`
- `tests/test_market_scan_screening.py`
- `tests/test_market_scan_sensitivity_cli.py`
- `tests/test_market_scan_shadow_scoring.py`
- `tests/test_market_scan_skip_contract.py`
- `tests/test_market_scan_snapshot_integrity.py`
- `tests/test_market_scan_terminal_recovery.py`
- `tests/test_market_scan_top100_snapshot_admission.py`
- `tests/test_market_scan_trend_precision_contract.py`
- `tests/test_market_scan_trial_registry.py`
- `tests/test_market_scan_trial_registry_clock_recovery.py`
- `tests/test_market_scan_trial_registry_lock_cleanup.py`
- `tests/test_market_scan_trust_contract.py`
- `tests/test_market_scan_universe.py`
- `tests/test_market_scan_validation.py`
- `tests/test_market_strategy_templates.py`
- `tests/test_market_time_equivalence.py`
- `tests/test_minute_analysis_modules.py`
- `tests/test_note_commit_readback_storage.py`
- `tests/test_note_write_transaction.py`
- `tests/test_notification_stream_continuity.py`
- `tests/test_offline_stock_note_creation.py`
- `tests/test_optional_kline_parsing_modules.py`
- `tests/test_optional_provider_concurrency.py`
- `tests/test_order_pressure_range_admission.py`
- `tests/test_paper_account_permissions.py`
- `tests/test_paper_execution_chronology.py`
- `tests/test_paper_exit_cash_admission.py`
- `tests/test_paper_session_coverage.py`
- `tests/test_paper_trading.py`
- `tests/test_paper_trading_metrics.py`
- `tests/test_paper_trading_schema.py`
- `tests/test_playwright_runner.py`
- `tests/test_probability_outcome_execution_evidence.py`
- `tests/test_probability_outcome_execution_phase_version.py`
- `tests/test_provider_canary.py`
- `tests/test_provider_errors_modules.py`
- `tests/test_provider_failure_status_modules.py`
- `tests/test_provider_priority_settings.py`
- `tests/test_provider_registry_modules.py`
- `tests/test_provider_status_aggregation_modules.py`
- `tests/test_provider_status_repository_modules.py`
- `tests/test_provider_utils_modules.py`
- `tests/test_qa_event_evidence_admission.py`
- `tests/test_qa_evidence_contract.py`
- `tests/test_quote_stream_modules.py`
- `tests/test_reliability_modules.py`
- `tests/test_repository_consistency.py`
- `tests/test_research_alpha_direction_admission.py`
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
- `tests/test_research_volume_scoring_contract.py`
- `tests/test_review_modules.py`
- `tests/test_review_paper_boundary_coverage.py`
- `tests/test_rules_alerts.py`
- `tests/test_run_market_scan_research_cli.py`
- `tests/test_runtime_artifact_recheck.py`
- `tests/test_runtime_backup.py`
- `tests/test_runtime_cleanup_public_transaction.py`
- `tests/test_runtime_coordinator.py`
- `tests/test_runtime_coordinator_cancellation.py`
- `tests/test_runtime_environment_modules.py`
- `tests/test_runtime_maintenance_lock_isolation.py`
- `tests/test_runtime_maintenance_regressions.py`
- `tests/test_runtime_restore_alert_stream.py`
- `tests/test_scheduler_lifecycle_cancellation.py`
- `tests/test_scheduler_modules.py`
- `tests/test_scheduler_recovery_admission.py`
- `tests/test_scheduler_structure.py`
- `tests/test_scheduler_tick_isolation.py`
- `tests/test_scheduler_worker_ownership.py`
- `tests/test_schema_compat.py`
- `tests/test_scoring_downside_deviation.py`
- `tests/test_scoring_modules.py`
- `tests/test_sina_client.py`
- `tests/test_sqlite_connection_lifetime.py`
- `tests/test_static_assets.py`
- `tests/test_stock_abnormal_events.py`
- `tests/test_stock_activity_modules.py`
- `tests/test_stock_analysis_modules.py`
- `tests/test_stock_event_summary.py`
- `tests/test_stock_lhb_modules.py`
- `tests/test_stock_lookup_modules.py`
- `tests/test_stock_note_revision_contract.py`
- `tests/test_stock_overview_modules.py`
- `tests/test_stock_pool_industry_enrichment.py`
- `tests/test_stock_pool_metadata.py`
- `tests/test_stock_rule_modules.py`
- `tests/test_stock_strategy_modules.py`
- `tests/test_strategy_automation.py`
- `tests/test_strategy_automation_atomic_completion.py`
- `tests/test_strategy_evidence.py`
- `tests/test_strategy_execution.py`
- `tests/test_strategy_execution_schema.py`
- `tests/test_strategy_lab.py`
- `tests/test_strategy_natural_language_contract.py`
- `tests/test_strategy_portfolio_cash_admission.py`
- `tests/test_strategy_schedule_claim_admission.py`
- `tests/test_strategy_schedule_mutation_admission.py`
- `tests/test_supply_chain.py`
- `tests/test_symbol_modules.py`
- `tests/test_system_diagnostics_modules.py`
- `tests/test_t_strategy_execution_range.py`
- `tests/test_tencent_provider_modules.py`
- `tests/test_tool_inventory_modules.py`
- `tests/test_trading_calendar_modules.py`
- `tests/test_trend_price_scale.py`
- `tests/test_typing_contract.py`
- `tests/test_user_data_alert_stream.py`
- `tests/test_uvicorn_smoke.py`
- `tests/test_valuation_modules.py`
- `tests/test_watchlist_monotone_read_watermark.py`
- `tests/test_watchlist_research_queue.py`
- `tests/test_watchlist_scan.py`
- `tests/test_watchlist_write_transaction.py`
- `tests/test_workbench_admission.py`
- `tests/test_workbench_context_cache_modules.py`
- `tests/test_workbench_pipeline_modules.py`


## 4. 浏览器与操作验收

`npm run test:e2e` 使用 `tools/run_playwright.mjs`。它先收集实际测试身份，按项目和完整文件组织批次；WebKit 每批最多 32 个收集身份，单文件超界应明确失败。每批使用官方 `--test-list` 精确选择并再次核对身份，随后启动独立 Playwright 进程，保留独立上下文、原顺序和超时。各批输出目录分开，用官方 blob/merge-reports 合并结果；跳过和失败保留，不进行静默重试。只读/交互模式及显式分配参数直通原 CLI，不擅自覆盖用户选择。

这是针对本机 Playwright 1.62.1 / WebKit 2336 的验证基础设施修正：原长序列及两种无项目脚本的最小 HTML 实验均在第 64 个上下文首次导航卡住，而 33+33 个上下文分属新进程时全部通过。最小实验中也无 SSE，去掉 API 拦截仍复现；具体原生资源原因尚未确定，不能据此声称修复了 WebKit 本身。`tests/test_playwright_runner.py` 验证批次、身份、CLI 参数与失败传播，完整矩阵另验证实际浏览器执行。

浏览器回归使用静态测试服务器和 mock API，覆盖 Chromium 桌面/移动端、Firefox、WebKit。确认读取过期保护、独立写入、失败草稿、复盘事件、重复绑定、按钮禁用与键盘/ARIA 行为；跳过项按 Playwright 项目限制解释，不报告成通过。`tests/e2e/review-save-ownership.spec.js` 在四项目验证保存等待期间编辑同计划/另一计划，确认草稿保留、按钮占用及下一次提交的ID和修订。`tests/e2e/notification-stream-continuity.spec.js` 新增真实IndexedDB双标签页迟到旧流隔离与分页中换流后重试；前者按既定协调浏览器范围执行，后者覆盖四项目。通知连续性专项另以独立临时FastAPI与真实浏览器保留页面，停测试服务后执行正式备份恢复，再启动同端口继续HTTP轮询；通知构造器受控，API响应不模拟。

`tests/e2e/cache-clock.spec.js` 在四项目使用真实页面和 `loadWatchlist`，验证系统时间后调、单调TTL到期后会重新请求并显示新的自选状态；短TTL只用于加快浏览器测试。默认15秒的精确边界、墙钟前后跳、零时点、显式失效、共享/取消与失败快照由 `tests/test_frontend_cache_clock.py` 验证。

`tests/e2e/strategy-history.spec.js` 通过实际入口验证超过100条策略的分页、归档策略可达、失败重试，以及刷新后只读重开已有执行和纸面委托；读取保留编辑草案，不提交执行或委托。`tests/e2e/review-pagination-recovery.spec.js` 验证追加页失败后已有记录与编辑内容仍可用，同页重试不会漏页或重复。`tests/e2e/notification-navigation.spec.js` 验证原通知记录到目标股票的导航、当前行情失败时仍保留触发快照，以及批量摘要不虚构单股；通知构造器受控。三组均覆盖四项目，Node行为用例另覆盖归属变化、迟到响应与无副作用边界。

`tests/e2e/review-plan-navigation.spec.js` 用可控响应顺序确认单计划先定位、普通列表后返回时仍保留卡片焦点；另验证用户已转去编辑输入框时不抢焦点。Node回归补充卡片消失、外部同ID和列表内其它控件等边界。

`tests/test_frontend_review_due_queue.py` 覆盖到期模式按需读取、服务端筛选请求、翻页保留与重试、409重新读取、迟到响应及实际取消；`tests/e2e/review-due-queue.spec.js` 在四项目通过真实控件验证201条队列的第五页可达、服务端筛选、首次和续页失败重试、409从首页重读、迟到响应与退出到期模式；读取不触发评估写入。

`tests/test_due_review_queue_pagination.py` 使用真实临时SQLite和实际FastAPI路由，验证完整到期集合、服务端筛选、跨页身份与数据变更。`tests/test_due_review_frontend_contract.py` 将实际API JSON交给Node中的正式前端校验器，验证首段、续页和空集合，避免手写夹具与后端契约脱节。

`tests/test_qa_event_evidence_admission.py` 使用真实静默行情分析→事件生产者→摘要→问答工作流，验证能力提示不构成证据、序列化保留类别、真实观察事件仍可回答、模型调用预算；`tests/test_chart_note_audit_dates.py` 使用临时 SQLite 和公开笔记/标注 API，验证清空日期、UTC/偏移跨日、旧格式和隐藏状态，并核查提示不占用事件标注配额。

`tests/test_frontend_qa_cancellation.py` 验证真实应用加载失败后问答清理、原股/换股失败回退、第二问实际成功与新表单所有权；`tests/e2e/qa-cancellation-recovery.spec.js` 在四项目通过真实页面控件验证相应恢复路径。浏览器使用受控业务 API，不冒充在线模型实验。

`tests/test_stock_note_revision_contract.py` 验证真实 API/SQLite 的过期编辑、显隐/删除、同时间戳、两连接竞争、读取后事务、原始类型摘要、回滚与恢复/导入；`tests/test_offline_stock_note_creation.py` 验证显式字段和可信本地身份零报价写入，以及缺省、错配、未知、过期和失败回滚。`tests/test_frontend_note_revision.py` 验证缺失令牌零写入、最新版本身份校验及迟到读取隔离；`tests/e2e/note-write-integrity.spec.js` 通过多页共享状态验证冲突恢复与保存期间草稿所有权。

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
| Saved strategy list page/refresh | 1 GET per 100-row page; at most 1 corrective GET if records shrink; failure preserves the visible page and loaded identity |
| Saved execution history page/refresh | 1 execution-list GET plus 1 version-list GET; at most 1 corrective execution-list GET if records shrink |
| Reopen one saved execution | 1 detail GET, then 2 independent GETs for candidates and existing simulation plan; no execution/plan POST; null plan remains absent |
| Strategy execution candidate page/sort change | 1 request, 50 rows in the browser and hard API cap 200 |
| Strategy evidence refresh | 1 compact offline-artifact request; no provider or cross-date evaluator |
| Full-market Excel export | 1 request for the complete current filtered snapshot; no provider refresh or score recomputation |
| Expand/collapse frozen scan evidence | 0; persisted row fields only |
| Apply one discovery preset | 1 leaderboard request plus 1 bounded rank-change request |
| Record one saved-screen change event | 1 explicit idempotent write request; render returned details locally; no provider or scoring request |
| Saved-screen list pagination | 1 request per page or explicit refresh, at most 100 summaries; preserve selected identity across pages |
| Saved-screen change history | 0 while collapsed; 1 summary request per expansion/page/refresh; 1 detail request per selected event/category/page; no mutation, provider or score recomputation |
| Enqueue all filtered discovery rows | 1 leaderboard request per page, then 1 queue request per 100 unique symbols |
| Advice-review evaluation | 1 |
| Load more advice-review plans / retry failed page | 1 GET at the same offset until success; no implicit evaluation or draft write |
| Expanding one advice-review history for the first time | 1 |
| Opening the global review dashboard | 2 independent no-store branches: `summary` plus at least 1 plan-list page per 100 active plans, capped at 100 pages; branch failures stay independent; due mode reads its own page only when active |
| Due-review mode / filter / page / explicit refresh or retry | 1 GET per action, 50 rows; server filters before total and pagination; continuation binds original as_of/token; no evaluation or draft write |
| Each history batch page | 1 navigation-only request; at most 1 corrective read if retention shrinks the page range; selecting a batch still verifies its trusted detail and results |
| Explicitly open watchlist changes | Existing workbench/timeline reads, then 1 viewed-watermark write only after current records are visible; ordinary chart browsing sends 0 viewed writes |
| Open stock notes or switch remembered research pages | 0 additional global review or paper-account requests |
| Watchlist queue search/status/due/unread filters, including clear and visible-page date refresh | 0; preserve complete queue and quote subscriptions |
| Strategy schedule management | 0 while collapsed; 1 list request per expansion, page or refresh; 1 PATCH per explicit toggle, followed by list synchronization; no automation evaluation |
| Open an exact plan from the global review dashboard | 1 single-plan read after stock context loads; ordinary plan pagination stays independent; no evaluation request |
| Paper dashboard / explicit simulation | 1 read on activation or refresh; 1 write only per explicit run; no broker request |
| Each System / Data-tab cleanup preview | 1 |
| Click a single delivered notification | Existing target-stock workbench reads; 0 event-ID lookup and 0 notification or viewed-watermark write; display captured original fields immediately |
| Click a batch notification | 0 additional stock load; show a batch summary without inventing a single event |
| Enabling browser notifications | 1 immediate baseline page; later 30-second polls use as many 50-event keyset pages as needed, capped at 200 pages |


全局JSON读取在15秒TTL内可复用已完成结果，共享进行中的同键请求；TTL从成功完成时的单调时点计算，显式失效后重新读取，展示时间仍为墙钟。系统时钟校准不应提前触发重复请求，也不应延长缓存寿命。

预算按实际流量和当前测试维护；跨页面副作用、重复监听器和轮询重入必须有行为回归，不能只检查代码字符串。

## 6. 当前验收结果

本轮：策略执行准入与交互恢复（2026-09-10），验收完成。全部 8,165 个 Python 测试收集项已通过分批验证（主运行 8,163 项、独立 smoke 2 项，文档失败已补跑解决）。修改前 1,147 个文件已冻结，包含并保留已有工作；本轮使用 1,151 文件的隔离副本验证。新增 17 个 Python 收集项及 5 个浏览器流程；生产修改限于五个已有文件。

| Date | Worktree State | Environment | Command | Scope | Result | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-10 | 冻结的生产代码与测试 | Python 3.12；临时测试初始化插件隔离默认 shell 设置，配置测试沿用自身夹具 | `python -m pytest -p isolated_test_environment -q -p no:cacheprovider --ignore=tests/test_uvicorn_smoke.py --cov=app --cov=tools --cov-report=term-missing`，另输出 JSON/JUnit | 8,163 项；两项需要回环端口的 smoke 独立原样运行 | 8,162 passed、57 subtests passed、1 failed；837.65 秒 | 唯一失败为更新验收记录时遗漏标准表头；生产/行为回归无失败，不能把本次运行写成一次全量零失败 |
| 2026-09-10 | 最终文档；生产与测试保持原字节 | 同一隔离测试环境 | `python -m pytest -q -p no:cacheprovider tests/test_tool_inventory_modules.py` | 文档表头与工具/库存契约 | 14 passed，40.44 秒 | 恢复原七列表格结构，不修改测试断言或门槛 |
| 2026-09-10 | 与全量相同的冻结源码副本 | 允许回环端口；临时 SQLite；`ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY=local` | `python -m pytest -q -p no:cacheprovider tests/test_uvicorn_smoke.py` | 真实服务启动、静态缓存头、SSE退出及未结束worker有界关闭 | 2 passed，5.98 秒 | 单独安排需端口的测试，未操作用户服务或真实数据 |
| 2026-09-10 | 冻结的源码 | pytest-cov；app 与 tools 行/分支联合统计 | 本轮全量生成 `coverage-final.json` | 79,323 行、28,974 分支 | 91.17%，原 90% 门槛通过 | 不使用前一轮覆盖率；文档修正不改变统计源码 |
| 2026-09-10 | 同一前端与测试副本 | Playwright；desktop/mobile Chromium、Firefox、WebKit；模拟业务 API | `npx --no-install playwright test`，选择下列七个文件并输出 JSON | 68 个收集身份 | 62 passed，6 skipped，56.85 秒；无失败、flaky 或重试 | 新增五流程在四项目全部20项通过；六个跳过均为原有项目限定 |
| 2026-09-10 | 工作区，生产/测试与冻结副本一致 | Python 3.12、Node 24 | runtime、pip check、Ruff、Mypy、Python/JS静态、仓库、API/函数索引、diff | 10 项工程检查 | 已通过；Mypy 388 个源文件 | 最终文档另核对仓库链接与diff |

浏览器文件为 `input-import-ownership.spec.js`、`notification-request-cancellation.spec.js`、`stock-search-flow.spec.js`、`tools-recovery.spec.js`、`notification-stream-continuity.spec.js`、`notification-coordination.spec.js` 和 `strategy-schedule-management.spec.js`。跳过来自原有两条 Chromium 专属 IndexedDB 多标签流程在另外三个项目的既定限制；不把跳过计作通过。新增通知取消流程在四项目均观察到实际请求失败/取消事件；IME流程在真实DOM派发组合输入键并验证应用处理，不等同于操作系统输入法端到端自动化。

独立复核通过：策略取得执行权与停用跨连接串行，固定修订不追随当前head，已拥有执行保持原收尾；通知停用释放读取、旧finally不影响新请求、等候交付锁时取消不推进游标；导入占用在首个await前建立，旧提交结束保留新预览、通用包装器收尾后再次同步；组合输入不触发应用按键行为。相关领域首次修后回归分别为112、41和39项；独立复核另运行54、6和28项。重叠领域用例不重复计入全量总数。

测试使用合成数据、临时SQLite和源码副本，不部署、不迁移真实数据或重启用户服务。过程命令、源码摘要、失败反例与结果位于 `/tmp/ashare-improvement-20260910-023821/`，包括 `acceptance-command.json`、`acceptance.log`、`smoke.log`、`browser-final.json`、`gates.json`、`change-scope.json` 和 `validation-record.json` 及 `docs-final.log`。临时初始化插件用于隔离本机默认设置，不改变产品代码或测试断言；新检出环境仍按第2节命令和测试自身夹具复验，不依赖该临时目录。
