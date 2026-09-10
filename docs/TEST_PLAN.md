# 测试计划与当前验收

## 1. 验证目标

覆盖实际用户入口与失败边界：行情身份/时序、扫描封存及评分回放、来源与研究资格、复盘修订、SQLite 迁移/备份/保留、异步取消与并发、前端过期响应/草稿/事件释放。删除旧接口时迁移行为测试，移除仅证明别名相等的测试。

扶摇专项分别验证默认关闭/密钥隔离、HTTP与业务码、有限重试/持久预算、财报身份与期间/单位、估值观察日、板块/情绪分页、后台任务取消收尾、未复权Parquet校验/原子发布及导出边界。真实账号成功与离线工程通过分别记载；不从 `code=0` 推导免费、完整授权、数据时点资格或评分效果。

同步任务控制专项验证：观察和成功checkpoint同一事务提交，保存失败不伪造成功；按ID查询、旧任务恢复、重复取消及停止与完成竞争；持久化原始请求和唯一父子补做身份，财报/估值只补未完成股票；历史下载、校验、写出中的合作停止，线程退出前不删除暂存或释放租约。浏览器验证任务详情、正在停止禁用、新旧请求身份、最近列表以外任务回执收敛及市场筛选保留展开状态。所有写入使用临时数据库、合成HTTP或Parquet，不为验收消耗真实账号额度。

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
- `tests/test_datahub_minute_sequence.py`
- `tests/test_datahub_minute_timezone.py`
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
- `tests/test_discovery_preset_read_snapshot.py`
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
- `tests/test_frontend_alert_editor_ownership.py`
- `tests/test_frontend_api_format_workbench.py`
- `tests/test_frontend_app_flow.py`
- `tests/test_frontend_app_lifecycle.py`
- `tests/test_frontend_audit_time.py`
- `tests/test_frontend_cache_clock.py`
- `tests/test_frontend_chart_context.py`
- `tests/test_frontend_chart_inspector.py`
- `tests/test_frontend_chart_market_time.py`
- `tests/test_frontend_chart_workspace.py`
- `tests/test_frontend_creation_draft_ownership.py`
- `tests/test_frontend_diagnostics.py`
- `tests/test_frontend_discovery.py`
- `tests/test_frontend_discovery_bulk_pagination.py`
- `tests/test_frontend_discovery_filter_roundtrip.py`
- `tests/test_frontend_discovery_preset_management.py`
- `tests/test_frontend_discovery_screen_alerts.py`
- `tests/test_frontend_event_bindings.py`
- `tests/test_frontend_fuyao.py`
- `tests/test_frontend_fuyao_jobs.py`
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
- `tests/test_frontend_paper_comparison_ownership.py`
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
- `tests/test_frontend_workspace_request_ownership.py`
- `tests/test_futu_provider_modules.py`
- `tests/test_fuyao_api.py`
- `tests/test_fuyao_client_modules.py`
- `tests/test_fuyao_config_modules.py`
- `tests/test_fuyao_dumps_cli.py`
- `tests/test_fuyao_dumps_sync.py`
- `tests/test_fuyao_dumps_validation.py`
- `tests/test_fuyao_financials.py`
- `tests/test_fuyao_individual_integration.py`
- `tests/test_fuyao_job_control.py`
- `tests/test_fuyao_observations.py`
- `tests/test_fuyao_service.py`
- `tests/test_fuyao_sync_control.py`
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
- `tests/test_minute_retention_event_time.py`
- `tests/test_note_commit_readback_storage.py`
- `tests/test_note_write_transaction.py`
- `tests/test_notification_stream_continuity.py`
- `tests/test_offline_stock_note_creation.py`
- `tests/test_optional_kline_parsing_modules.py`
- `tests/test_optional_provider_concurrency.py`
- `tests/test_order_pressure_range_admission.py`
- `tests/test_paper_account_permissions.py`
- `tests/test_paper_account_write_transaction.py`
- `tests/test_paper_execution_chronology.py`
- `tests/test_paper_exit_cash_admission.py`
- `tests/test_paper_session_coverage.py`
- `tests/test_paper_strategy_write_acknowledgement.py`
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
- `tests/test_run_local.py`
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
- `tests/test_sbom_failure_recovery.py`
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

`tests/test_frontend_fuyao.py` 使用正式前端模块验证范围上限、字段转义、未知单位与负值保留、股票切换迟到响应隔离、只读零采集及重复提交占用。`tests/e2e/fuyao-research.spec.js` 通过实际财报和系统数据管理入口验证报告期切换、显式任务失败重试/完成状态、切股后的旧响应隔离；业务 API 使用合成响应。`tests/test_frontend_fuyao_jobs.py` 和 `tests/e2e/fuyao-task-controls.spec.js` 验证结构化任务详情、停止收尾、补做剩余、旧任务回执核实、写入与导航独立、市场展开状态及筛选焦点。浏览器执行结果须以下方本轮记录为准，不将新增用例本身写成已通过。

`tests/e2e/alert-editor-ownership.spec.js` 在四项目通过真实规则表单验证延迟保存期间编辑同一/另一规则，已确认写入但回读失败后草稿保留，当前保存失败后可按原值重试。Node用例补充取消重开、普通回读、身份替换/删除及提交占用。

`tests/e2e/discovery-bulk-resize.spec.js` 在四项目实际缩放浏览器，验证205只筛选结果仍按固定分页完整入队。`tests/e2e/paper-comparison-ownership.spec.js` 验证旧对比成功/失败不能覆盖新历史反馈，选择改回原值也淘汰旧请求，新对比及历史导出身份保持正确。Node用例补充取消、迟到清理、异常分页零写入和正常路径。

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

`tests/e2e/chart-market-time.spec.js` 在四项目分别设置UTC和洛杉矶浏览器时区，核验真实画布横轴的上海时间、日线字面日期与鼠标/触摸/键盘选中检查器保留的来源时间原文。

人工操作仅在需要连接真实运行实例时执行：健康检查、有效个股加载、切换图表、创建笔记、预警测试、查看冻结扫描及导出、查看复盘、备份验证。不要为验证结构重构自动清空数据库或触发真实扫描。

## 5. 请求预算

| Flow | Expected additional requests |
| --- | ---: |
| Cold stock load, including SSE | 11; hidden diagnostics send 0 requests |
| Each stock switch, including SSE | 6 |
| Enter visible system diagnostics | 4 GETs; monitoring polls 3 GETs every 15 seconds while visible; leaving cancels reads and polling |
| Open Fuyao financial reports | 1 stock GET plus 1 status GET when status is not yet loaded; no supplier requests |
| Open system data management | 2 Fuyao local GETs; 0 cleanup-preview requests; first entering the default diagnostics subpage may start 4 GETs that are cancelled on leaving |
| Check eligible cleanup data | 1 explicit preview GET; execution obtains a fresh preview before any cleanup POST |
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
| Open Fuyao stock facts / change stock while its panel is active | 1 local `/api/fuyao/stock` GET; first use may add 1 `/status` GET; 0 supplier requests |
| Open Fuyao System / Data records or explicitly refresh local market status | 2 independent local GETs (`status`, `market`); explicit full local refresh may also read current stock facts; 0 supplier requests |
| Change Fuyao report period or filter cached market rows | 0; use the current local response |
| Submit or explicitly retry one Fuyao collection task | 1 explicit POST, then one local status GET per 2-second active poll; a running receipt missing from the recent list adds one GET by ID; failed status polls back off to 5 seconds; hidden page does not schedule the next poll |
| Inspect / stop a Fuyao task | Details use 1 explicit local GET by ID; stopping uses 1 explicit POST then local status polling; unknown write outcomes are resolved by reads, never automatic re-submission |
| Fuyao financial collection | Usually 3 statement requests plus 1 indicator request per selected stock; missing report identity omits indicators; bounded by project daily attempt budget, retries counted |
| Fuyao history full / incremental sync | 2 signed-link API requests plus 2 no-Key object downloads; retries are additional API attempts; no qfq cache update |
| Click a single delivered notification | Existing target-stock workbench reads; 0 event-ID lookup and 0 notification or viewed-watermark write; display captured original fields immediately |
| Click a batch notification | 0 additional stock load; show a batch summary without inventing a single event |
| Enabling browser notifications | 1 immediate baseline page; later 30-second polls use as many 50-event keyset pages as needed, capped at 200 pages |


全局JSON读取在15秒TTL内可复用已完成结果，共享进行中的同键请求；TTL从成功完成时的单调时点计算，显式失效后重新读取，展示时间仍为墙钟。系统时钟校准不应提前触发重复请求，也不应延长缓存寿命。

预算按实际流量和当前测试维护；跨页面副作用、重复监听器和轮询重入必须有行为回归，不能只检查代码字符串。

## 6. 当前验收结果

### 同步任务控制（2026-09-10）

本轮生产代码冻结后，全量非smoke运行8,684项：8,683通过，唯一失败是旧静态资源清单未列入新增的两个任务模块。补齐清单后，对该文件全部43项重测通过，JUnit身份核对覆盖原失败及该文件所有用例；生产代码未因补测改变。另有2项独立服务smoke通过，最终8,686个Python测试身份都有通过记录，另计57个子测试；不把重叠补测相加。

| 范围 | 实际结果 |
| --- | --- |
| 行与分支综合覆盖 | app/tools共81,655条语句、29,864个分支，91.19%，通过90%门槛；全量840.72秒 |
| 任务与历史边界 | 原子观察/checkpoint、真实COMMIT拒绝回滚、唯一父子补做、旧任务恢复、连续取消、首次调度前取消、实际ProviderRuntime不再重试、历史各阶段停止、发布后晚取消及进度落盘失败均通过 |
| 受影响浏览器 | 财报及任务控制36项、首页请求预算4项，共40项通过，覆盖桌面/移动Chromium、Firefox、WebKit；无失败或跳过。首页11次、每次切股增加6次的预算保持 |
| 工程门槛 | Ruff、Mypy 414模块、编译/Pyflakes、177个JS语法、运行环境、仓库链接/导入、API/函数索引和diff通过；没有新增依赖 |
| 凭据与数据 | 1,224个候选文件及完整Git历史秘密扫描通过；Key仍为Git忽略的本地0600文件。真实服务空闲时优雅重启成功，请求计数171不变，已有历史版本指针不变 |
| 重启后真实页面 | 桌面1440与移动页面的任务详情GET、旧参数提示、版本模块、市场展开/焦点均通过；无JS异常或横向溢出，POST及其他写请求为0，预算171→171。Fuyao相关GET均200；既有`/api/plates`两次503单独记录，不宣称所有行情源正常 |

测试用临时SQLite、合成HTTP和真实小型Parquet，不消耗真实账号。旧任务缺少请求参数的事实保留，不能据错误信息重建补做范围。阶段进度仅报告真实行/字节，未知总量不生成百分比。本轮验收记录在本机`/tmp/ashare-task-controls-20260910-2ddo0n45/acceptance.json`，全量和补测分别为`/tmp/ashare-task-controls-full.xml`、`/tmp/ashare-task-controls-final-frontend-contract.xml`；这些文件不是新检出复验的前置条件。

### 上一轮：扶摇接入与页面加载（2026-09-10，独立验收）

最终 Python 验收按收集身份合并全量与变更补测：当前非 smoke 的8,630项全部有通过记录，另有2项独立服务 smoke 通过，共8,632项，另计57个子测试。不是将重叠回归次数相加。结构检查发现的3处过多分支已按指针/manifest、报告期选择和分页合同拆分，未降低原门槛。

| 范围 | 已运行验证 | 实际结果与边界 |
| --- | --- | --- |
| Python当前完整集合 | 全量已通过的未变化用例，加剩余用例与最终改动模块补测；隔离本机shell默认设置 | 7,736项沿用已通过结果，894项最终补测全部通过，收集身份校验没有缺项；补测357.56秒。先前失败均已修复或在正确的测试进程入口复验通过，不计作最终通过证据 |
| 服务smoke | 原样运行 `tests/test_uvicorn_smoke.py`，临时数据库和回环端口 | 2 passed，5.47秒；没有操作真实数据库或API Key |
| 最终行/分支覆盖 | app与tools合并统计；4个最后修改的Python模块先清除旧覆盖数据，再执行相关用例 | 81,273条语句、29,740个分支，综合91.17%，通过90%门槛；JavaScript不计入此数字 |
| 全浏览器基线 | 完整Playwright矩阵，desktop/mobile Chromium、Firefox、WebKit | 400 passed、52既有平台条件skipped，0 failed/flaky；此后页面改动由下列最终矩阵补验，重叠次数不相加 |
| 最终受影响浏览器流程 | `stock-search-flow`、`frontend-flow`、`workspace-design`、`fuyao-research`、`workspace-loading` 五个spec | 110 passed、10平台条件skipped、0 failed；准确验证首页11、切股累计17/23、名称检索累计18及4个隐藏诊断接口0请求；长错误换行和显式清理入口包含在内 |
| 页面所有权 | 6个完整Python/Node前端模块 | 84 passed；新增4个行为在旧源码均失败，修后通过。覆盖退出/重入、排队旧timer、页面可见性、保留显式写请求及执行前重新预览 |
| 轻量依赖可用性 | 真实Fuyao router和AppContainer，占满AnyIO worker token | 旧同步 `get_datahub` 导致3种缓存GET超时；改为异步容器读取后三项通过；不是外部网络测速 |
| Market Dumps | 全部校验、同步与CLI测试 | 77 passed；真实pyarrow与MockTransport，涵盖严格指针/manifest、全量/增量、同日多行动保留、缩股、缺口、原子回退、取消、无Key下载和研究导出 |
| 工程检查 | runtime、pip check、Ruff、Mypy、编译/Pyflakes、JS语法、仓库/索引及diff | 全部通过；Mypy 412源文件，JS语法174文件；函数与参考文档门槛保持 |
| 依赖与秘密 | hash锁安装、三份Python锁审计、npm audit、重复生成Python/npm SBOM、Gitleaks候选文件及完整历史 | 通过；SBOM逐字节一致。密钥仅位于Git忽略的本地0600文件，未写入源码、测试夹具或Git历史，未提交或推送 |
| 真实财务与估值 | 30只有效沪深京证券、三张财报及指标、估值快照 | 财务原值/报告期可读；估值31个请求对象中30个成功，旧430047.BJ的3001单独失败，其他结果保留。未核实金额单位、首次披露/修订版本及费用政策 |
| 真实市场观察 | 完整板块与当日情绪采集 | 710目录/710行情；2026-09-10涨停34、跌停11、炸板22、龙虎榜60条，指定异动解释0条；不将缺项补零或用于正式评分 |
| 真实历史归档 | 原始文件校验、规范化发布及CLI `verify` | 5,560股、10,275,240条日线、2,427交易日期，2016-09-12至2026-09-10；57,183条企业行动，删除1条完全重复并保留2组同日不同记录及2条缩股记录。校验/写入/发布165.65秒，不含下载。十日原文件55,479条已校验，联网增量发布尚未执行 |
| 真实页面 | 最终本地服务，正常浏览器请求，无采集或清理POST | 财报4个年度可切换，板块/情绪/历史摘要可见；4个Fuyao响应均200，单次296–765ms，不作为SLA；1440视口无横向溢出、无Key输入、无页面JS异常。既有 `/api/plates` 仍出现提供者503，不据此声称所有行情源正常 |

真实历史继续作为独立未复权研究数据，不更新生产qfq缓存，也不证明历史成员、交易状态或PIT资格。真实页面普通读取没有增加供应商请求数；同步必须显式提交。进入数据管理不再自动深度校验既有研究JSON，检查可清理数据和最终执行仍使用原完整性校验。

本机会话证据包括 `/tmp/ashare-fuyao-complete-tests.xml`、`/tmp/ashare-fuyao-final-test-plan.json`、`/tmp/ashare-fuyao-final-supplement.xml`、`/tmp/ashare-fuyao-final-smoke.xml`、`/tmp/ashare-fuyao-final-coverage.json`、`/tmp/ashare-fuyao-real-ui-verification.json` 和 `/tmp/ashare-fuyao-workspace-five-browser.log`。这些不是新检出环境运行或复验的前置条件；复验仍使用第2节命令及测试夹具。

### 前一轮：分钟序列、图表时间与策略创建（2026-09-10）

以下为前一轮独立验收记录，不代表新增扶摇代码已通过同一全量。该轮验收完成，修改前1,162个源码/配置文件已独立备份，包含此前全部修改；全量使用1,168文件的隔离副本。该轮新增57个Python测试收集项和2个浏览器流程。8,328个Python测试收集项全部通过（全量8,326项，加独立smoke 2项），另有57个子测试通过。

| Date | Worktree State | Environment | Command | Scope | Result | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-10 | 冻结的生产代码与测试 | Python 3.12；初始化插件隔离默认shell设置，配置测试沿用自身夹具 | `python -m pytest -p isolated_test_environment -q -p no:cacheprovider --ignore=tests/test_uvicorn_smoke.py --cov=app --cov=tools --cov-report=term-missing`，另输出JSON/JUnit | 8,326项；两项需要回环端口的smoke独立原样运行 | 8,326 passed、57 subtests passed；879.82秒；无失败或跳过 | 本轮全量首次通过，所有新增Python回归均包含在内 |
| 2026-09-10 | 与全量相同的冻结源码 | 允许回环端口；临时SQLite；`ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY=local` | `python -m pytest -p isolated_test_environment -q -p no:cacheprovider tests/test_uvicorn_smoke.py` | 临时服务启动、静态缓存头、SSE退出及未结束worker有界关闭 | 2 passed；6.44秒 | 未操作用户服务或真实数据 |
| 2026-09-10 | 冻结的生产源码 | pytest-cov；app与tools行/分支联合统计 | 本轮全量生成 `coverage-final.json` | 79,360行、28,988分支 | 91.19%，原90%门槛通过 | 本轮实际覆盖率；JavaScript另由行为和浏览器验证 |
| 2026-09-10 | 前端、配置和浏览器测试与全量副本共177文件摘要一致 | desktop/mobile Chromium、Firefox、WebKit；模拟业务API | `npx --no-install playwright test`，选择下列三个文件并输出JSON | 80个收集身份 | 70 passed、10 skipped；66.26秒；无失败、flaky或重试 | 新增两流程在四项目共8项全部通过，跳过来自原有桌面/移动端适用范围 |
| 2026-09-10 | 工作区，生产与测试保持冻结字节 | Python 3.12、Node 24 | runtime、pip check、Ruff、Mypy、Python/JS静态、仓库、API/函数索引、diff | 10项工程检查 | 全部通过；Mypy 389个源文件 | 图表导入映射与统一静态版本已更新；最终文档另核对链接、索引及验收表格 |

浏览器三个文件为 `chart-market-time.spec.js`、`paper-account-permissions.spec.js` 和 `frontend-flow.spec.js`。10项跳过分别为三条移动端专属流程在三个桌面项目的9项，以及桌面图表检查在移动端的1项；不计作通过。

修前证据与修后回归：分钟排序扩展基线17项中15失败/2通过，最终新增23项与相关模块共185项通过；维护核心6项在原排序下全部失败，最终7项新增与已有维护回归共28项通过。图表24项在原实现15失败/9通过，原浏览器两个时区均画出错误小时；修复后新8项浏览器全部通过，图表与资源装配86项相关回归通过。策略创建3项在原实现2失败/1通过，修复后相关57项通过。异常安全和连接生命周期、公开清理事务的26项，以及架构/文档31项均通过；这些重叠验证不重复加入全量总数。

独立复核补齐两处边界：同一分钟以A/B/A时间格式连续刷新，相同获取时间下缓存仍返回最新价格；失败降级在LIMIT之前排除未来记录，合法旧数据仍可取回。4项独立测试包含116个真实SQLite数值存储案例，普通数字文本保留列亲和性转换，数值列中的BLOB明确作为异常缓存排除。旧分钟测试曾要求无效行占用LIMIT而少返回一条，现按更强合同断言返回3条有效行情，日线预期保持；未删除用例或放宽质量门槛。

策略独立复核验证返回模型在事务中构造，另一连接在提交前看不到记录，真实COMMIT拒绝后HTTP失败、回滚、重试和删除均正常。独立真实浏览器还确认新页面实际请求带新版本号的图表模块，上海时间显示生效。原始时间文本、微秒和图表检查器证据保留，分钟清理仍按原股票/周期分区及物理行数上限执行，日K规则保持。稳定文档已与代码交叉核对。

性能范围：SQLite 3.46.0、合成单分区、每次返回120行，3次运行中位值。2万行分区原字符串查询0.21毫秒，修复初版121.83毫秒，复用时间计算后83.22毫秒；10万行压力样本分别0.21、618.55、416.13毫秒。查询计划先使用股票/周期索引，结果条数有界，但仍需扫描并处理目标分区，成本随分区增长；这是保证时间顺序、有效观测及去重的额外成本，不是页面端到端延迟或相对原实现的加速承诺。未变更数据库schema或索引。

过程证据位于 `/tmp/ashare-consistency-20260910-092003/`，包括 `baseline-manifest.json`、`acceptance-command.json`、`acceptance.log`、`acceptance-result.json`、`smoke.xml`、`coverage-final.json`、`browser-final.json`、`browser-summary.json`、`gates.json`、`independent-reviews.json`、`change-scope.json` 和 `validation-record.json`。最终源码与全量及浏览器副本按摘要核对，收尾只更新当前Goal和验收文档。测试使用合成数据、临时SQLite与源码副本，不读取真实业务数据、部署或重启用户服务；新检出环境按第2节命令和测试自身夹具复验，不依赖本机临时日志。
