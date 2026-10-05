# 测试计划与当前验收

## 1. 验证目标

覆盖实际用户入口与失败边界：行情身份/时序、扫描封存及评分回放、来源与研究资格、复盘修订、SQLite 迁移/备份/保留、异步取消与并发、前端过期响应/草稿/事件释放。删除旧接口时迁移行为测试，移除仅证明别名相等的测试。

扶摇专项分别验证默认关闭/密钥隔离、HTTP与业务码、有限重试/持久预算、财报身份与期间/单位、估值观察日、板块/情绪分页、后台任务取消收尾、未复权Parquet校验/原子发布及导出边界。真实账号成功与离线工程通过分别记载；不从 `code=0` 推导免费、完整授权、数据时点资格或评分效果。

同步任务控制专项验证：观察和成功checkpoint同一事务提交，保存失败不伪造成功；按ID查询、旧任务恢复、重复取消及停止与完成竞争；持久化原始请求和唯一父子补做身份，财报/估值只补未完成股票；历史下载、校验、写出中的合作停止，线程退出前不删除暂存或释放租约。浏览器验证任务详情、正在停止禁用、新旧请求身份、最近列表以外任务回执收敛及市场筛选保留展开状态。所有写入使用临时数据库、合成HTTP或Parquet，不为验收消耗真实账号额度。

财报修复回归覆盖请求跨秒与真正未来批次、显式报告期空响应和占位指标、失败补做及请求预算；跨100次窄范围刷新、年报/季报并存、仅指标不遮蔽报表、部分报表替换及完整性提示重算；页面切换、财务体检和问答保留逐期来源时间，旧格式兼容。

扶摇估值评分专项验证同一PE TTM/PB MRQ观察仅替换个股既有基本面槽、总览与估值一致，特征/因子/风险/规则随同一批次传播；金额与季度口径未确认不派生财务分。覆盖摘要、股票、未来及过期观察、旧上游时间不能靠重新获取刷新、缺失与零值不归一化、负值不奖励、组件唯一与分项合计、规则版本/来源/观察身份、工作台缓存复用和无远端读取。额外拒绝晚于行情时点的观察，避免跨日或日内后到信息写成旧批次特征；七日窗口仍按真实评估时刻计算。

个股价值研究专项验证已准入输入覆盖、正倍数倒数及溢出边界、拒用观察保留原值时不得旁路准入；按所选报告期显示利润、经营现金流和权益符号，重复/缺失/错表/未来观察不可用，仅完整年度合并报表允许利润现金流方向检查。原行情路径验证市值不构成估值可用或加分、零值不扣分、未来历史及同行样本不进入分位；浏览器验证期间切换、缺口核实动作、转义、旧格式及无额外请求。

页面加载专项验证研究历史只恢复最后成功股票、失败选择与清空历史；系统与自选启动、切入、断网恢复和隐藏恢复时不读取隐藏个股，切回研究只加载一次，复盘恢复仍读取其全局看板。自选首次失败后成功写入回读须恢复就绪、清除错误并重建报价流，重复选择自选页不能断流；系统数据导入后保留提交成功回执并只刷新可见数据。

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

评分v3新增专项：`tests/test_factor_aggregation_v3.py`、`tests/test_alpha_factor_admission_v2.py`、`tests/test_research_volume_direction_contract.py`、`tests/test_current_factor_replay_admission.py`。覆盖预算固定/身份重复、方向连续性、下游准入及收盘交易日完整链路。

量化策略专项：`tests/test_strategy_quant_metrics.py`验证独立OHLC公式、条件边界、缺失/篡改/重新封签/旧算法隔离与执行指纹；`tests/test_volume_confirmation_session_basis.py`验证公司行动、会话缺口及极低量比在当前与历史路径一致拒绝。策略自动化覆盖周末、春节、盘前和15:15发布时点，执行输入拒绝NaN与无效权重。

The current test suite is split by domain:

- `tests/test_active_research_review_backend.py`
- `tests/test_advice_review_evidence_contract.py`
- `tests/test_advice_review_window_contract.py`
- `tests/test_advice_reviews.py`
- `tests/test_akshare_stock_metadata.py`
- `tests/test_alert_notification_feed.py`
- `tests/test_alert_rule_mutation_transactions.py`
- `tests/test_alert_stream_persistence.py`
- `tests/test_alerts_unavailable_state.py`
- `tests/test_alpha_factor_admission_v2.py`
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
- `tests/test_current_factor_replay_admission.py`
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
- `tests/test_factor_aggregation_payload_v3.py`
- `tests/test_factor_aggregation_v3.py`
- `tests/test_factor_calibration_label_basis.py`
- `tests/test_factor_evidence_contract.py`
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
- `tests/test_frontend_market_context.py`
- `tests/test_frontend_market_scan_comparison.py`
- `tests/test_frontend_market_scan_condition_impact.py`
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
- `tests/test_frontend_quote_stream.py`
- `tests/test_frontend_research_activity.py`
- `tests/test_frontend_research_panels.py`
- `tests/test_frontend_review_drafts.py`
- `tests/test_frontend_review_due_queue.py`
- `tests/test_frontend_review_pagination_recovery.py`
- `tests/test_frontend_review_plan_navigation.py`
- `tests/test_frontend_review_save_ownership.py`
- `tests/test_frontend_review_scan.py`
- `tests/test_frontend_screening_ownership.py`
- `tests/test_frontend_stock_search.py`
- `tests/test_frontend_stock_search_history.py`
- `tests/test_frontend_strategy_draft_identity.py`
- `tests/test_frontend_strategy_history.py`
- `tests/test_frontend_strategy_rebalance.py`
- `tests/test_frontend_strategy_recovery.py`
- `tests/test_frontend_strategy_schedule_management.py`
- `tests/test_frontend_strategy_templates.py`
- `tests/test_frontend_tools_recovery.py`
- `tests/test_frontend_value_research.py`
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
- `tests/test_fuyao_financial_history.py`
- `tests/test_fuyao_financials.py`
- `tests/test_fuyao_individual_integration.py`
- `tests/test_fuyao_job_control.py`
- `tests/test_fuyao_observations.py`
- `tests/test_fuyao_score_integration.py`
- `tests/test_fuyao_scoring.py`
- `tests/test_fuyao_service.py`
- `tests/test_fuyao_sync_control.py`
- `tests/test_historical_probability_rejection_cache.py`
- `tests/test_historical_replay_estimator_binding.py`
- `tests/test_indicator_atr_window.py`
- `tests/test_indicator_levels_modules.py`
- `tests/test_indicator_trend_context.py`
- `tests/test_indicator_trend_modules.py`
- `tests/test_indicator_volume_modules.py`
- `tests/test_individual_industry_context.py`
- `tests/test_individual_probability.py`
- `tests/test_individual_probability_data_boundary.py`
- `tests/test_individual_probability_store_failures.py`
- `tests/test_individual_workflow_modules.py`
- `tests/test_industry_plate_resilience.py`
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
- `tests/test_market_context_binding.py`
- `tests/test_market_context_scoring.py`
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
- `tests/test_market_scan_comparison.py`
- `tests/test_market_scan_condition_impacts.py`
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
- `tests/test_market_scan_paired_groups.py`
- `tests/test_market_scan_performance.py`
- `tests/test_market_scan_pipeline.py`
- `tests/test_market_scan_pressure.py`
- `tests/test_market_scan_previous_close_admission.py`
- `tests/test_market_scan_probability.py`
- `tests/test_market_scan_probability_artifact.py`
- `tests/test_market_scan_probability_capture.py`
- `tests/test_market_scan_probability_coherence.py`
- `tests/test_market_scan_probability_estimators.py`
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
- `tests/test_market_scan_probability_refresh.py`
- `tests/test_market_scan_probability_replay.py`
- `tests/test_market_scan_probability_runtime.py`
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
- `tests/test_market_scan_score_contract_stream.py`
- `tests/test_market_scan_score_tracking.py`
- `tests/test_market_scan_scoring.py`
- `tests/test_market_scan_screen_alert.py`
- `tests/test_market_scan_screen_alert_history.py`
- `tests/test_market_scan_screening.py`
- `tests/test_market_scan_sensitivity_cli.py`
- `tests/test_market_scan_shadow_scoring.py`
- `tests/test_market_scan_skip_contract.py`
- `tests/test_market_scan_snapshot_integrity.py`
- `tests/test_market_scan_terminal_recovery.py`
- `tests/test_market_scan_time_verification_stream.py`
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
- `tests/test_plate_observation_contract.py`
- `tests/test_playwright_runner.py`
- `tests/test_prediction_diagnostics.py`
- `tests/test_price_volume_replay_contract.py`
- `tests/test_probability_maintenance_budget.py`
- `tests/test_probability_outcome_execution_evidence.py`
- `tests/test_probability_outcome_execution_phase_version.py`
- `tests/test_probability_outcome_quarantine.py`
- `tests/test_provider_canary.py`
- `tests/test_provider_errors_modules.py`
- `tests/test_provider_failure_status_modules.py`
- `tests/test_provider_priority_settings.py`
- `tests/test_provider_registry_modules.py`
- `tests/test_provider_status_aggregation_modules.py`
- `tests/test_provider_status_repository_modules.py`
- `tests/test_provider_utils_modules.py`
- `tests/test_public_execution_baostock.py`
- `tests/test_public_execution_cli.py`
- `tests/test_public_execution_comparison.py`
- `tests/test_public_execution_notices.py`
- `tests/test_public_execution_store.py`
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
- `tests/test_research_volume_direction_contract.py`
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
- `tests/test_scheduler_cleanup_task.py`
- `tests/test_scheduler_lifecycle_cancellation.py`
- `tests/test_scheduler_modules.py`
- `tests/test_scheduler_recovery_admission.py`
- `tests/test_scheduler_stock_pool_task.py`
- `tests/test_scheduler_structure.py`
- `tests/test_scheduler_tick_isolation.py`
- `tests/test_scheduler_windows.py`
- `tests/test_scheduler_worker_ownership.py`
- `tests/test_schema_compat.py`
- `tests/test_score_tracking_cli.py`
- `tests/test_score_tracking_report.py`
- `tests/test_scoring_downside_deviation.py`
- `tests/test_scoring_modules.py`
- `tests/test_sina_client.py`
- `tests/test_sqlite_connection_lifetime.py`
- `tests/test_static_assets.py`
- `tests/test_stock_abnormal_events.py`
- `tests/test_stock_activity_direction_contract.py`
- `tests/test_stock_activity_modules.py`
- `tests/test_stock_analysis_modules.py`
- `tests/test_stock_event_summary.py`
- `tests/test_stock_lhb_modules.py`
- `tests/test_stock_lookup_modules.py`
- `tests/test_stock_note_revision_contract.py`
- `tests/test_stock_overview_aggregation_contract.py`
- `tests/test_stock_overview_modules.py`
- `tests/test_stock_pool_industry_enrichment.py`
- `tests/test_stock_pool_maintenance.py`
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
- `tests/test_strategy_prospective_cli.py`
- `tests/test_strategy_prospective_collection.py`
- `tests/test_strategy_prospective_evidence.py`
- `tests/test_strategy_prospective_plan.py`
- `tests/test_strategy_prospective_replay.py`
- `tests/test_strategy_quant_metrics.py`
- `tests/test_strategy_schedule_claim_admission.py`
- `tests/test_strategy_schedule_mutation_admission.py`
- `tests/test_strategy_template_net_returns.py`
- `tests/test_strategy_template_selection.py`
- `tests/test_strategy_template_tracking.py`
- `tests/test_strategy_template_tracking_cli.py`
- `tests/test_strategy_template_tracking_metrics.py`
- `tests/test_strategy_template_tracking_report.py`
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
- `tests/test_value_research.py`
- `tests/test_volume_confirmation_session_basis.py`
- `tests/test_watchlist_monotone_read_watermark.py`
- `tests/test_watchlist_research_queue.py`
- `tests/test_watchlist_scan.py`
- `tests/test_watchlist_write_transaction.py`
- `tests/test_workbench_admission.py`
- `tests/test_workbench_context_cache_modules.py`
- `tests/test_workbench_pipeline_modules.py`

## 4. 浏览器与操作验收

`tests/e2e/workspace-loading.spec.js` 验证系统/自选偏好恢复的请求范围、重新打开成功研究股票、失败选择不覆盖历史及清空后回到默认。`tests/e2e/market-scan-probability.spec.js` 验证高级研究默认收起、键盘展开及再次折叠后保留期间和证据；`tests/e2e/layout-regression.spec.js` 检查手机扫描控制与三个股票操作按钮完整可见。

`tests/test_frontend_strategy_rebalance.py` 与 `tests/e2e/strategy-rebalance.spec.js` 验证持有期与调仓间隔分别读写：载入10/5规格后再次编译仍为10/5；修改任一字段不改变另一个，缺失或非整数间隔不能隐式补值。

`tests/e2e/strategy-template-catalog.spec.js`在桌面/移动Chromium、Firefox和WebKit验证目录v2状态、仅编译不保存、影子/缺字段路线禁用，以及载入新增动量和风险条件后修改组合数量仍保留完整筛选值与PIT要求。真实后端目录另通过Node验证器回归核验，避免浏览器替身掩盖字段漂移。

`tests/test_frontend_value_research.py` 与 `tests/e2e/value-research.spec.js` 验证价值摘要的输入覆盖、倒数边界、报告期精确匹配、核实动作、旧格式兼容、股票身份与迟到响应隔离；通过页面操作检查无额外读取、无任务写入及移动端宽度。

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
| Strategy template catalog/load | 1 pure catalog GET on lab activation/reload; loading one available template sends 1 dry-run compile POST, with no save, scan or provider request |
| Saved execution history page/refresh | 1 execution-list GET plus 1 version-list GET; at most 1 corrective execution-list GET if records shrink |
| Reopen one saved execution | 1 detail GET, then 2 independent GETs for candidates and existing simulation plan; no execution/plan POST; null plan remains absent |
| Strategy execution candidate page/sort change | 1 request, 50 rows in the browser and hard API cap 200 |
| Strategy evidence refresh | 1 compact offline-artifact request; no provider or cross-date evaluator |
| Full-market Excel export | 1 request for the complete current filtered snapshot; no provider refresh or score recomputation |
| Frozen candidate comparison | 0 when selecting up to 4 candidates across pages; 1 bounded read-only POST per explicit comparison, verifying the batch once; differences and JSON export use the validated response locally |
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

### 页面恢复、按需加载与研究入口（2026-10-04）

- 复用成功研究历史恢复股票，系统与自选页延迟个股读取，返回研究/复盘恢复一次；可见页面分别拥有数据刷新和报价订阅。高级概率研究与历史回放默认收起，保留原控件与状态；手机扫描控制及股票操作按钮完整可见。
- 全部前端 Node 行为与相关静态回归967项通过，另有架构、仓库一致性、工具索引与异常安全63项通过。覆盖失败历史、清空恢复、旧请求取消、复盘看板、隐藏子页、重复进入自选、断网与可见性恢复、成功写入回读和系统导入回执。本轮没有Python生产代码变更，未重跑全库后端测试或重新计算其覆盖率。
- 完整浏览器集合612项，经全量和受影响场景补验按文件、标题与项目去重，562项通过、50项按既有设备/浏览器覆盖范围跳过，无未解决失败。首轮两种旧断言在三个项目共失败6项：摘要通知返回研究页会恢复原股票一次，离页取消后的旧导航不再写提示。更新为精确原股请求次数、实际取消、迟到结果不抢焦点、不误标已读及再次查看恢复后，两个完整spec在四项目24项全部通过；不重复累计补测次数。桌面及手机截图核对通过。
- Ruff、465个显式源文件Mypy、运行环境、编译/Pyflakes、197份JavaScript语法、仓库链接与导入、API/函数清单及差异格式检查通过。未放宽模块或函数门槛，静态资源版本更新为`20261004-workspace-loading-v1`。
- 验证使用独立本机静态服务和合成API；没有调用实际供应商、启动正式扫描或改写研究产物。概率回撤仅补充消费者与语义说明，历史公式、评分与研究资格不变。

本机离线证据为`/tmp/ashare-optimization-final-frontend.xml`、`/tmp/ashare-optimization-final-engineering.xml`；浏览器去重汇总及完整报告位置见`/tmp/ashare-optimization-acceptance-20261004.json`。临时报告不是新检出复验的前置条件。

### 全市场条件影响与筛选解释归属（2026-09-23）

- [产品对照与计划](MARKET_SCAN_PRODUCT_PLAN.md)第二轮增加六个同类产品的官方资料。本轮交付单条件独立影响、标准/方案空榜解释、完整已应用筛选及冻结身份绑定；自选范围、指定股票体检和进出榜原因仍属后续计划。
- 后端、前端Node/静态及架构相关回归按JUnit用例身份去重共725项通过，无失败或跳过。原有候选对比、方案分页、批量收集、保存修订及概率页面相关行为纳入回归，未跑全库测试或重新计算覆盖率。
- 后端覆盖从完整条件中真实删除一项的独立SQL对照，含双边范围、OR分组、字面关键词、零与缺失、多项失败及极限分页。一次完整受检事务内读取标量总体及最多200+100条详情，验证只开一次连接、只做一次全批验证；并发封存替换不混合两份快照，过期/跨线程/事务重开能力继续拒绝。
- 前端覆盖已应用普通条件和完整保存方案，未提交草稿不替换解释；错误条件、计数、示例原值及完整批次身份拒绝。7项完整控制器专项验证健康轮询、普通/方案迟到覆盖、后台刷新不夺取方案、隐藏后晚响应失效，以及普通/方案隐藏恢复时列表与上下文一致。已准入的普通读取保留原有执行顺序，失去提交资格后不能恢复旧显示。
- 新条件影响8场景及原筛选工作台1场景，在桌面/移动Chromium、Firefox、WebKit共36项通过，无失败、跳过或flaky。覆盖双向请求竞态、空榜按钮/键盘聚焦、手机表格滚动、XSS转义和只读请求预算。桌面及移动截图核对通过；浏览器使用独立4173静态服务和合成API。
- Ruff、465个显式源文件Mypy、编译/Pyflakes、197份JavaScript语法、仓库引用与链接、API/函数清单和差异格式检查通过。主控制器仍574行、概率期间控制器586行，未放宽模块或函数门槛。
- 本地服务空闲时优雅重启，ready=leader，页面缓存版本为`20260923-condition-impact-v1`。真实run176的5,565条冻结记录、原33条命中及全部旧响应字段逐项一致（仅新schema版本及包含新字段的响应摘要不同），原快照摘要不变，响应为v2及`no-store`。示例规则“沪深、成功、分数≥95、成交额≥10亿元”移除分数新增387只，移除成交额新增150只；它们是条件敏感性统计，不是更优策略结论。
- 同一本地请求修改前6.216秒、修改后4.668秒，响应341,575字节。单次前后测量不保证缓存/机器负载可比，不外推稳定吞吐或整页提速；只将一次评估完整验证从2次变1次作为确定性工作量改善。扶摇持久请求0→0，无活跃采集任务；未启动扫描、改写冻结评分或推送运行数据。

本机证据：`/tmp/ashare-condition-impact-test-summary-20260923.json`汇总五份JUnit并去重，`/tmp/ashare-condition-impact-e2e-20260923.json`记录最终浏览器报告位置，`/tmp/ashare-condition-impact-runtime-20260923.json`保存真实运行对照。持久服务响应夹具为`tests/fixtures/market_scan_screen_evaluation_v2.json`。临时报告不是新检出运行前置条件。

### 全市场冻结候选对比（2026-09-23）

- [产品对照与计划](MARKET_SCAN_PRODUCT_PLAN.md)的P0已交付：2–4只股票同批次对比、跨页选择、仅看差异及来源JSON导出。P1/P2仍为后续范围。
- 后端375项、前端Node/静态及相关回归143项，共518项不同测试通过，无失败或跳过；JUnit身份去重核对。覆盖单次全批验证、同事务取数、封印冲突、篡改、真实新股跳过记录禁用日历自动刷新、缺失与零值、旧批次审计、重读准入、过期响应和失败后禁止导出。
- `tests/e2e/market-scan-comparison.spec.js`的6个场景在桌面/移动Chromium、Firefox和WebKit共24项通过。覆盖标准/方案入口、选择上限、与研究队列独立、XSS、摘要/批次变化、仅看差异及真实JSON下载；合同冻结后相关用例复验通过，不重复累计。桌面与移动端截图核对通过，使用独立静态服务及合成API。
- Ruff、465个显式源文件Mypy、编译/Pyflakes、190份JavaScript语法、仓库引用与链接、API/函数清单和差异格式检查通过。未重跑全库测试或覆盖率，不宣称本轮达到全库90%覆盖门槛。
- 本地服务空闲时优雅重启后ready及runtime leader正常。真实run176的两股请求200、2,949字节、7.339秒；错误预期摘要409、23.152秒。完整批次校验仍有固定成本，单次重启后测量不能推导稳定吞吐或提速。原快照摘要不变，扶摇持久请求数0→0，未发起正式扫描或供应商采集任务。

本机证据：`/tmp/ashare-comparison-offline-final-20260923.xml`、`/tmp/ashare-comparison-regression-20260923.xml`、`/tmp/ashare-comparison-frontend-node-20260923.xml`、`/tmp/ashare-comparison-frontend-regression-20260923.xml`、`/tmp/ashare-comparison-e2e-result-20260923.json`及`/tmp/ashare-comparison-runtime-20260923.json`。临时证据不是运行前置条件。

### 概率数值边界与受检读取优化（2026-09-23）

本轮提取纯数值估计器并合并已确认的重复扫描，评分版本、规范摘要、严格 JSON、同事务能力及取消门禁保持。25个模块相关回归最终共1,188项通过，0失败、0跳过；新数值测试跨平台修订后整体替换该模块原记录，未重复累计。未重跑全量套件、覆盖率或浏览器测试，前端无修改。

| 范围 | 验证与结果 |
| --- | --- |
| 数值边界 | 主概率模块3,793→3,517行，248行数值估计与48行共享值校验独立拥有；删除原实现及4个代理，迁移实验、联合执行、个股产物和研究消费者。估计器不反向依赖编排 |
| 确定性对照 | 同一运行时，成功/不足/不收敛完整证据、预测和拒绝结果共69,794字节逐字节一致，SHA256 `13e1843709892bde4c08c792beaf32e2d18b790152a31d70bc5f6d9c3ed15ca6`。持久夹具精确约束版本/分区/状态/计数，浮点使用rel=1e-9、abs=1e-12，完整回放验证当前内部摘要，避免跨BLAS末位差异误报 |
| 时间检查 | publication逐结果检查并入强制规范行遍历，时间验证先于观察器，封印写入仍早期预检。17项专项覆盖末行迟到/坏时间、各结果状态、时区/微秒、观察器顺序、无第二次时间扫描、封印不变和legacy审计语义 |
| 评分合同 | 56项专项以原SQL为差分基准，覆盖缺失/null/布尔/数值/容器/Unicode、异构、计数错误、非法末行、观察器异常和资格拒绝。完整摘要及动作准入通过后才发布合同，无第二次全批JSON聚合 |
| 完整性与并发 | 相关整合包括原始非排名字段篡改、重复JSON键、NaN、drop/change/recreate触发器、跨线程、DML/DDL、事务重开、WAL更新及过期会话拒绝；完整5,382行合成扫描仍通过 |
| 只读性能 | 对本地封存run176的5,565条结果，两版各5次独立进程交替调用公开verified-read入口，连接每次新建、无可信TTL缓存。中位3,223.955→2,977.298毫秒，约减少7.65%；原范围2,994.289–4,327.873毫秒，新范围2,880.932–3,299.921毫秒。全部原摘要、动作资格、5,421成功评分合同及runtime日历摘要一致 |
| 基准限制 | 最初baseline源码副本缺少运行日历，触发bundled来源并拒绝原runtime来源绑定；该组耗时作废。修正依赖后才做上述对照。性能数字仅属于完整受检读取，不外推为页面整体提速；严格JSON、全图摘要和资格重放仍有约3秒成本 |
| 工程与本地服务 | Ruff、Mypy463源文件、编译/Pyflakes、185个JS语法、仓库导入/链接、API/函数索引和差异检查通过。空闲时平滑重启，ready=leader；索引及联合执行维护收敛后，概率响应除本次联合执行诊断时间外与重构前相同，waiting_labels、历史unavailable保留。最终单次完整接口3,551.18毫秒，仅作运行验收，不作配对性能统计；扶摇持久请求0→0 |

可复验的持久回归为`tests/test_market_scan_probability_estimators.py`、`tests/test_market_scan_time_verification_stream.py`、`tests/test_market_scan_score_contract_stream.py`及对应夹具。本机对照材料为`/tmp/ashare-next-final-test-summary-20260923.json`、`/tmp/ashare-next-verified-read-comparison-20260923.json`和`/tmp/ashare-next-http-final-20260923.json`，不作为新检出环境的前置依赖。旧rev2计划摘要、日历和收据链头未变化，source_drift继续阻断原试验；未创建新试验、采用策略、请求真实供应商或推送GitHub。

### 职责边界与唯一合同重构（2026-09-23）

本轮按任务所有权、数据摘要和浏览器连接职责重构，不改变评分合同、HTTP接口或数据库格式。按JUnit身份合并最终相关回归，共1,673项通过，0失败、0跳过，另有4项子测试通过；桌面Chromium的8项浏览器测试通过。未重跑全量套件或覆盖率，本节不替代完整90%覆盖门槛的独立验收。

| 范围 | 验证与结果 |
| --- | --- |
| 扫描与概率运行时 | manager 1,470→1,086行、80→59方法，getattr 52→8；概率运行时拥有任务、锁、回调与worker，固定共享同一research stores。15模块512项通过，覆盖来源优先、历史错误隔离、预热、子进程、查询、导出、发布快照和调度 |
| 关闭所有权修复 | 归档错误诊断写入改用受管I/O；manager和runtime并发停止、连续取消时仍持有任务与guard，直到真实写线程结束。另验证固定store身份、关闭阶段顺序、旧callback失效及默认时钟微秒保留 |
| 复盘摘要唯一实现 | 仓储写入/回读、导入验证和ID重映射共享低层合同，保留边界版本准入、REAL规范化及事务。222项域回归和25项门禁通过；固定v1/v2摘要夹具及300组合成数据改前/改后对照一致 |
| 指标所属模块 | 删除16个指标转发别名，消费者直接依赖metrics。相关331项与47项消费层回归通过；不足样本、单/多折拟合等3组固定输入的合同和输出共159,888字节逐字节一致，摘要e48f3e0b1f643b8d18fbad15acb4a73cf4a73f1c9645d4b6af75e4bfc57494d8 |
| 报价流 | 页面入口2,858→2,672行，连接/timer/代次由闭包持有，纯帧合同独立。127项Python/Node和架构回归通过；8项浏览器流程使用4173隔离合成API及本地SSE，覆盖股票切换、旧帧、重连、后台恢复和模拟persisted pagehide/pageshow，未访问8010或真实供应商 |
| 组合与API | 24模块375项及4子测试通过，扫描/扶摇API77项通过，最终生命周期/架构补测134项通过；独立临时数据库的真实Uvicorn启动2项通过。上述集合存在重叠，总数按用例身份去重 |
| 工程检查 | Ruff、Mypy461源文件、编译/Pyflakes、185个JS语法、仓库导入/链接、API和函数索引、diff检查全部通过。没有新增依赖或放宽函数/异常/类型门槛 |
| 本地运行 | 空闲时优雅重启后ready=leader、12类任务、自动全市场扫描关闭、无活跃扶摇任务、持久请求0→0。概率查询收敛至waiting_labels，旧历史参考明确unavailable。rev2计划摘要与收据链头均未变化，source_drift及日历匹配状态保留 |

本机会话证据包括`/tmp/ashare-refactor-validation-20260923.json`、`/tmp/ashare-runtime-refactor-final-20260923.xml`、`/tmp/ashare-review-evidence-refactor-20260923.xml`、`/tmp/ashare-refactor-metrics-comparison-20260923.json`、`/tmp/ashare-refactor-engineering-gates-20260923.json`。持久测试夹具位于`tests/fixtures/advice_review_evidence_golden_v1.json`；临时会话文件不是新检出复验依赖。所有旧研究档案与试验计划继续保留，不将结构重构或工程通过解释为预测效果提高。

### 后台维护隔离与前台轻量读取（2026-09-23）

本轮修复覆盖健康检查与清理解耦、股票池独立更新、保留查询投影、概率后台刷新和历史参考拒绝隔离。按JUnit用例身份合并初验及修复后的补测，共1,465项Python回归通过，失败和跳过均为0；最终概率相关30模块711项通过，来源与历史同时失败的优先级另行补测通过。浏览器概率面板在独立静态服务、合成API下3项通过。没有重跑全量套件或覆盖率，不据此宣称全项目90%覆盖门槛重新通过。

| 范围 | 实际验证与边界 |
| --- | --- |
| 清理与事务 | 无扫描删除候选时自动维护跳过无关研究档案深验；事务内重新发现候选则回滚。存在候选和手动清理仍完整核验引用、内容和命名空间；取消保持实际worker所有权 |
| 保留查询 | 同一真实SQLite只读快照、预热后交替5次、6类候选ID完全一致。日K候选查询中位1,779.898→575.464ms；报价223.990→73.766ms。没有执行生产删除，不是整个清理或页面耗时 |
| 后台任务 | 清理移至每日20:00–09:00，其他任务/扫描/扶摇/研究索引忙时待续；健康检查首轮243ms、最终重启首轮262ms。全市场自动扫描仍关闭 |
| 股票池 | 单次显式元数据任务9.33秒成功，5,568只（沪2,320、深2,902、北346），缓存真实更新时间为当天11:19。完整性、缩水、三市场覆盖、写入失败及取消回归通过 |
| 概率索引 | 前台已提交投影不深读整个目录；冷启动只请求单个后台刷新。失败退避无需第二次页面请求，旧生命周期回调失效，来源绑定变化和目录竞态仍拒绝。真实投影初次约16ms、后续中位0.81ms、深读0次；完整概率API仍约3–4秒，未消除整批扫描验证成本 |
| 历史参考 | 稳定旧合同拒绝明确不可用，相同快照不重复深验；暂时I/O、读期间变化、不可信路径不能作为稳定拒绝缓存。原始旧档案只用临时副本复验，源变更/坏摘要/路径别名均重新拒绝，原档案未改。历史错误不阻断有效来源归档，来源本身失败仍阻断。最终重启后真实API已从待验证收敛为当前研究等待标签、历史参考明确不可用；对应新监控事件仅记录一次 |
| 工程门禁 | Ruff、Mypy459源文件、编译/Pyflakes、183个JS语法、仓库导入/链接、API/函数索引和diff检查通过；没有新增依赖 |
| 真实运行 | 最终服务优雅重启，ready为leader、任务列表12类，扶摇持久请求数0→0。行业行情当天11:18曾成功获取20条，之后仍有上游失败/缓存过期提示；不宣称提供者已经稳定 |
| 前瞻试验 | rev2原计划和收据保留；source_drift、calendar_matches_frozen=true、1条source_incomplete、无到期遗漏。rev3为待用户确认的具体方案，未创建或切换自动化，未补填历史信号 |

会话证据为本机`/tmp/ashare-background-final-tests-20260923.json`、`/tmp/ashare-background-final-delta-20260923.xml`、`/tmp/ashare-retention-benchmark-20260923.json`、`/tmp/ashare-historical-real-copy-review-20260923.json`和`/tmp/ashare-background-final-restart-20260923.json`。这些临时文件不是新检出复验的依赖。旧结果的合同/重放拒绝和独立前瞻样本缺口继续如实保留，工程通过不等于预测准确或策略收益已证明。

### 后台调度与旧结果隔离（2026-09-22）

- 53个相关及工程模块首轮1448项通过，另有17个子测试；随后仅概率维护模块补充“坏档案删除/目录消失不得解除隔离”，最终102项补测通过。真实重启后发现任务状态展示重复验封，再补轻量导航读取并完成253项调度/API回归。三份JUnit去重为**1493个测试身份**，不重复累加。
- 综合行/分支覆盖**91.60%**，满足仓库90%门槛。9个变更生产文件先清除旧覆盖，再运行首轮；后续变更的维护模块及调度执行模块分别再次清除旧覆盖，采用各自最终补测数据。该模块单独覆盖为88.91%，不能误称每文件均达到90%。其他文件使用源码身份相同的历史覆盖；本轮不是全库或浏览器重测，未修改前端。
- 覆盖可信日历、节假日/午休/夜间、结束时刻排他、UTC转换、手动任务、取消所有权、失败退避、协议失败不切源、延迟源不参与评分、坏档案身份绑定、整个比较组禁止拟合及文件消失后保留隔离。独立代码复核发现的隔离解除缺口已修正并复核。
- Ruff、Mypy（457个显式源文件）、编译/Pyflakes、API/函数清单、仓库导入/链接、函数复杂度及源码秘密扫描通过。所有最终源码的Ruff、457模块Mypy均已重新通过。
- 对真实run135坏档案的临时副本只读验证：首次3.419秒，第二次0.000095秒，仍拒绝同一旧标记冲突；原文件与副本字节摘要均未变化。全目录冷校验在626秒预算处中止，没有完成的第二轮，**冷启动耗时仍未解决**；独立研究缓存仍严格拒绝旧结果，不把整项后台任务写成已恢复成功。
- 8010服务空闲优雅重启后ready、runtime leader正常；报价/行业/日K的下次时间为09:15，研究任务为15:15，全市场自动开关仍关闭，扶摇当日计数0→0。首轮健康检查用时115秒，结束后下次间隔900秒；最终导航修复重启后，在健康清理仍运行时，任务状态读取实测0.387秒，ready及扶摇状态约0.002秒。这不是所有读取场景的性能保证。源码变化使旧前瞻计划进入保护，原9月21日`source_incomplete`收据及源码备份保留，未自动另建试验。

本机验收文件为`/tmp/ashare-scheduler-20260922.xml`、`/tmp/ashare-scheduler-delta-20260922.xml`、`/tmp/ashare-scheduler-navigation-20260922.xml`、`/tmp/ashare-scheduler-final-validation-20260922.json`、`/tmp/ashare-scheduler-final-coverage-20260922.json`、`/tmp/ashare-scheduler-navigation-restart-20260922.json`、`/tmp/ashare-scheduler-navigation-runtime-20260922.json`及`/tmp/ashare-probability-quarantine-benchmark-20260922.jsonl`。这些临时文件不是运行前置条件。

### 行情恢复与概率维护续跑（2026-09-22 后续修复）

- 33个相关模块 **907项测试及17个子测试通过**。隔离子进程协议最后重构后，清除该模块旧覆盖并补测191项；前端隔离文案、错误概率准入及模块边界调整后补测53项。后两组是前述907项的子集，不重复累计。
- Ruff、457个显式源文件Mypy、编译/Pyflakes、183份JavaScript语法、仓库导入/链接、API/函数清单及差异格式检查通过。没有重跑全库或浏览器套件，没有继承上一轮临时覆盖文件，也不宣称通过本轮全库90%覆盖门槛。
- 定向验证失败立即冷却、20个排队请求不穿透、已有共享调用/取消所有权、完整行业首屏与短页拒绝、不可变日历区间边界/换代、分段验证不提前发布、跨片失败与隔离保留、整个比较组阻断、暂存拒绝立即屏蔽旧模型及严格更新后再解除。刷新失败的摘要经过真实任务120字符裁剪路径检验。
- 同一真实run156结果档案的只读cProfile：16.329秒降至9.567秒（约41%），仍严格拒绝相同语义漂移。真实run135旧标记冲突在隔离worker首读3.629秒，重复缓存读取0.000004秒；原档案摘要未变。这些是单档案测量，不能代表整个冷目录已完成。
- 8010空闲时优雅重启后ready、runtime leader正常；现有观察池任务2.235秒更新10只股票报价，最新市场时间为9月22日16:14:54（抓取时间17:41:56），两者不混淆。扶摇持久预算0→0，没有启动下载或全市场扫描。
- 真实自动概率维护约61秒完成源目录13/19进度，返回pending；一次显式续跑60.607秒完成源目录，进入结果目录12/73，并报告12份已知隔离。来源摘要中的14是按既有规则选出的canonical数量，不是19份文件丢失。整个目录尚未完成，后续按原自动窗口与间隔续跑；已有隔离仍按降级退避，不伪装成功。CLI预算缓存仅在同进程内续用。
- 东方财富行业来源仍有间歇断连，重启后一次刷新0.659秒明确返回503，不改写旧缓存时间。随后通过既有分钟分析接口核验600519.SH：0.954秒返回48条5分钟K线、availability=ok，市场时间覆盖9月22日15:00；这只证明该股本次恢复，不代表全市场分钟数据已补齐。首次健康清理132秒仍是未优化的独立开销；状态接口一次测量为4毫秒，不表示所有读取均同速。旧前瞻rev2继续source_drift保护，原计划、9月21日失败收据和源码备份保留。

本轮本机证据：`/tmp/ashare-reliability-20260922.xml`、`/tmp/ashare-reliability-delta-20260922.xml`、`/tmp/ashare-reliability-frontend-20260922.xml`、`/tmp/ashare-reliability-coverage-20260922.json`、`/tmp/ashare-reliability-runtime-final-20260922.json`及`/tmp/ashare-reliability-continuation-20260922.json`。临时文件不是运行前置条件。

### 读取性能与请求合并（2026-09-22）

- 41个相关模块 **877项测试及4个子测试通过**，涵盖缓存统计、旧库索引迁移、清理事务隔离、档案读取/重放/隔离/拟合边界、前端请求所有权及工程结构。验收前记录全部生产源码摘要，测试期间无源码变更；未重跑全库或声明本轮覆盖率达到90%。
- `advice-review-drafts`、`paper-comparison-ownership`、`watchlist-changes-navigation` 三个浏览器spec在desktop-chromium **9项通过**。测试使用独立4173静态服务与合成API，未访问8010或供应商。精确请求数量、200条复盘集合不被迟到8条覆盖、取消及快速切股由Node行为测试验证；不是全浏览器矩阵验收。
- Ruff、457个显式源文件Mypy、编译/Pyflakes、183份JavaScript语法、仓库导入/链接、API/函数清单与差异格式检查通过。
- 从实际数据库只读复制1,423,834行日K的四个统计字段到临时表，原三次查询耗时0.261～0.265秒，合并聚合后0.091～0.098秒。新覆盖索引约69.78MiB，临时表建索引0.77秒；固定1万行更新时间的中位数8.75→67.17毫秒，约增加58.4毫秒。该写入基准使用savepoint回滚，不代表完整生产upsert；统计结果保持等价，未把抓取时间改为行情时间。
- 同一实际source run176档案（5421条记录）：目录查询3.465→2.270秒；按批次加载5.405→1.959秒，约下降63.8%。结果档案run156、截至9月4日：3.749→3.189秒，仍拒绝相同语义漂移，原因摘要、来源绑定及原始文件摘要一致。这些是单档案测量，不代表完整目录冷启动已完成。
- 独立复核发现并修复两处提速边界：被摘要剥离的时间子树仍验证指数溢出与无效Unicode；按批次选中来源后，安全重读并逐字节核对本次验证的压缩内容，拒绝删除、替换、符号链接和恢复mtime后的改写。每次新的清理操作仍重新完整验证，事务中的原始字节与目录核对保留。
- 复盘页同一股票和加载代次合并进行中的建议历史请求，完整200条供复盘选择、前8条供短时间线；8→200升级会取消旧请求，完成后再次刷新仍访问接口。进入模拟交易页面的看板去掉一次重复加载。诊断计数使用单独只读快照，清理事务持锁时仍只能看到已提交状态。
- 本地5.53GB数据库完成SQLite一致备份、摘要/外键/封存校验后，在无活动任务窗口优雅重启8010服务，ready与runtime leader正常，兼容索引已建立。后台空闲前后各三次请求的中位数：`/api/data/status` 287.7→104.7毫秒，`/api/system/diagnostics` 307.6→120.6毫秒；任务状态约2毫秒。扶摇持久请求数0→0，未手动启动供应商下载或正式扫描。
- 启动并发负载仍明显：健康清理与概率校验同时运行时，上述两个接口分别为3.77～5.52秒和4.61～7.67秒；首轮健康清理114.8秒后成功。概率维护61.1秒进入pending、源目录16/19，尚未完成全目录；不能把重启后的暂时零隔离计数解释为旧隔离解除。这些启动期数据与空闲接口分开记录，不宣称消除了全部冷启动瓶颈。
- 下一优先级已定位但未在本轮实现：将前台概率投影触发的完整预热交给受生命周期管理的后台任务，前台明确返回待验证并保留未核验概率禁用；将健康诊断和全档案清理分开调度。`research_projection(blocking=False)`当前只避免等锁，取得锁后仍可同步刷新；是否触发取决于批次准入、目录变化及已有pending状态。调整清理前置候选检查时仍须在事务中复核候选和引用，不能用mtime复用替代删除授权。

本机证据：`/tmp/ashare-speed-final-20260922.xml`、`/tmp/ashare-speed-validation-sources-20260922.json`、`/tmp/ashare-speed-frontend-e2e-20260922.xml`、`/tmp/ashare-speed-readpaths-20260922.json`、`/tmp/ashare-source176-speed-before.json`、`/tmp/ashare-source176-speed-after.json`、`/tmp/ashare-outcome156-speed-before.json`、`/tmp/ashare-outcome156-speed-after.json`、`/tmp/ashare-speed-pre-restart-20260922.json`、`/tmp/ashare-speed-post-restart-20260922.json`、`/tmp/ashare-speed-stable-after-20260922.json`及`/tmp/ashare-speed-final-validation-20260922.json`。临时文件不是运行前置条件。

### 独立前瞻采集与执行资料（2026-09-19）

- 25个相关及工程模块 **823项通过**，覆盖事前登记、固定日期/截止、首批无回退、坏时间、迟到和永久缺失、源码与运行时日历漂移、完整归档重放、资金/篮子/来源篡改、只读一致前缀及供应商研究资料边界。工程门禁与旧模板、正式执行、扶摇档案回归包含在该数字中。
- 综合行/分支覆盖 **91.50%**。与前轮源码逐一比对，11个新增或变更的Python文件先清理覆盖记录，再运行本轮用例；其他模块沿用源码身份相同的已验证记录。这是增量验收，本轮未重新运行全库或浏览器用例；本轮没有前端变化。
- Ruff、Mypy（452个显式源文件）、编译/Pyflakes、API/函数清单、仓库引用与文档、函数复杂度、源码差异秘密扫描均通过；未增加依赖。真实计划创建后未修改冻结的生产源码。
- 真实扶摇增量已完成并离线核验：截至2026-09-18、5565只股票、10,308,548条日线和57,390条公司行动，使用2次签名请求；生产行情缓存未覆盖，资料仍为研究来源，正式执行准入为false。
- 已创建`strategy-templates-20260921-phase1`并实际运行`status/capture/evidence/evaluate`。初始源码/日历匹配、等待2026-09-21，7个信号锚点、6个日历可覆盖退出日、收据0条。Codex heartbeat已启用工作日15:30/17:30/21:30，尚无实际触发或成熟前瞻收益可验。

本机证据为`/tmp/ashare-prospective-collection-20260919.xml`、`/tmp/ashare-prospective-coverage-20260919.json`、`/tmp/ashare-prospective-acceptance-20260919.json`。计划、命令、定时跟进及尚缺的正式来源条件见[前瞻采集指南](STRATEGY_PROSPECTIVE_COLLECTION.md)。这些临时验收文件不是运行前置条件。

```bash
.venv/bin/python -m pytest -q tests/test_strategy_prospective_plan.py tests/test_strategy_prospective_collection.py tests/test_strategy_prospective_replay.py tests/test_strategy_prospective_evidence.py tests/test_strategy_prospective_cli.py
.venv/bin/python tools/collect_strategy_prospective.py --plan-id strategy-templates-20260921-phase1 status
```

### 策略净收益比较与选择证据（2026-09-19）

- 38个相关模块首轮 **1,113项通过**；最终净执行/CLI补验122项、报告补验93项，工程7模块77项。四份JUnit去重共 **1,194个测试身份**，不重复累加。覆盖缺失/未成熟/空仓、同日重扫、固定非重叠锚点、三方共同日期、Holm校正、压力成本、T+1、整手与容量、退出受阻、未来执行证据和极端有限价格。
- 综合行/分支覆盖 **91.45%**。记录700个现行生产源文件的身份并与前轮比对，9个Python文件新增或改变；已有变更文件先清除旧覆盖再重测，未变模块沿用已验证记录。这是增量验收，不是本轮全库重测。后续极小价格修复和报告文字修复分别再次清除相应模块覆盖后复验。
- 离线报告校验冻结PIT证据时也不得隐式刷新交易日历；回归启用自动刷新条件并拦截刷新入口验证。正常在线日期查询保留默认刷新行为。
- 合成报告4类情景在桌面/移动Chromium、Firefox、WebKit共16项布局检查通过；真实报告1440px/390px无全页横向溢出，外部请求0。顶部明确区分毛收益可用分组与净选择失败合同。
- Ruff、Mypy（443个显式源文件）、运行环境、编译/Pyflakes、182个JS语法及工程结构检查通过。当前源码差异与新增源码脱敏秘密扫描通过，无新增依赖。
- 真实只读比较仍为15个原始批次：2个封存未通过、13个批次含`missing`状态行，准入日期0；严格官方执行证据不可用，采用策略为空。扫描49条、保存策略2条、执行3条未变，没有供应商请求、补扫描或策略替换。8010已空闲优雅重启并通过health/live/ready，供应商当日请求0→0，历史数据指针未变。

复验入口：

```bash
.venv/bin/python -m pytest -q tests/test_strategy_template_net_returns.py tests/test_strategy_template_selection.py tests/test_strategy_template_tracking.py tests/test_strategy_template_tracking_metrics.py tests/test_strategy_template_tracking_cli.py tests/test_strategy_template_tracking_report.py tests/test_trading_calendar_modules.py
.venv/bin/python tools/track_strategy_templates.py --database data/ashare_radar.sqlite3 --output-directory data/research/strategy_template_tracking --non-overlapping-signals
```

本机证据前缀为`/tmp/ashare-strategy-selection-`：`regression`、`final-backend`、`final-renderer`、`engineering`的20260919 XML及`acceptance-20260919.json`。临时证据不是运行前置条件；执行材料准备、比较规则与保留现有策略的原因见[策略模板对照](STRATEGY_TEMPLATE_TRACKING.md)。以下为此前验收，不与本轮数字相加。

### 新策略对照与独立调仓间隔（2026-09-19）

- 27个相关模块 **671项通过**；7个工程模块 **77项通过**。编排准入职责拆分后重新清除该文件覆盖记录并补验45项；三份JUnit去重 **748个测试身份**，不重复累加。
- 综合行/分支覆盖 **91.42%**：与前轮源码摘要比对，已有Python生产模块保持不变，沿用其覆盖证据，新增四模块纳入本轮回归；这是增量验收，不是本轮全库重测。编排、纯统计、CLI和HTML分别为93.88%、98.99%、96.15%、100%。
- 两个浏览器spec在桌面/移动Chromium、Firefox和WebKit共 **20项通过**，无失败、跳过或flaky。载入10/5、独立改期、缺失/小数拒绝、模板载入后筛选条件保持均通过。实际离线报告1440px/390px无横向溢出，失败详情可展开，外部请求0；截图已检查。
- Ruff、Mypy **440个显式源文件**、编译/Pyflakes、182个JS语法、运行版本、导入/链接、API/函数索引、复杂度与diff检查通过；当前源码差异及新增源码脱敏秘密扫描通过，无新增依赖。首轮编排函数分支超限经准入阶段拆分后通过原门槛。
- 真实只读报告：19个正式批次选取15个原始批次，排除4次重扫；2个封存未通过、13个冻结成员不完整，可用日期0，明确证据不足。原始扫描49条、策略2条、执行3条保持不变，未调用供应商、创建新扫描或训练模型。
- 本地8010在空闲时优雅重启，health/live/ready通过；静态版本 `20260919-strategy-comparison-v1`。供应商当日请求 **0→0**，历史数据指针未变；目录保持15项、可载入8项。

复验新增入口：

```bash
.venv/bin/python -m pytest -q tests/test_strategy_template_tracking.py tests/test_strategy_template_tracking_metrics.py tests/test_strategy_template_tracking_cli.py tests/test_strategy_template_tracking_report.py tests/test_frontend_strategy_rebalance.py
npm run test:e2e -- tests/e2e/strategy-rebalance.spec.js tests/e2e/strategy-template-catalog.spec.js
.venv/bin/python tools/track_strategy_templates.py --database data/ashare_radar.sqlite3 --output-directory data/research/strategy_template_tracking
```

本机证据前缀为 `/tmp/ashare-strategy-followup-`：`regression-20260919.xml`、`engineering-20260919.xml`、`final-backend-20260919.xml`、`browser-20260919.json`和`acceptance-20260919.json`。临时证据不是运行前置条件；比较口径及当前真实数据限制见[策略模板对照](STRATEGY_TEMPLATE_TRACKING.md)。以下为此前验收范围，不与本轮数量相加。


### 量化因子与策略迭代（2026-09-19）

- 因子/策略相关56模块 **1,412项测试及11个子测试通过**；评估职责拆分后补充13模块 **260项通过**；工程7模块 **77项通过**。三份JUnit去重共1,737个测试身份，包含子测试，不将重复执行相加。
- 综合行/分支覆盖 **91.37%**：沿用已核对的未变更模块覆盖记录，先清除本轮11个变更生产文件，重测后再清除拆分涉及的3个生产文件并补验；这不是本轮全库重测。
- 策略目录浏览器 **12项通过**，覆盖桌面/移动Chromium、Firefox与WebKit，零失败、跳过或flaky。实际后端15项目录另经桌面/手机视觉检查，两个新模板各载入一次，只调用dry-run编译替身，无保存、扫描和页面横向溢出。
- Ruff、Mypy **436个显式源文件**、编译/Pyflakes、181个JS语法、运行版本、导入/链接、API/函数索引及diff检查通过。发现的旧评估模块行数超限通过职责拆分修复，3735降至3517，原门槛保留。当前源码差异和新增源码的脱敏秘密扫描通过，无新增依赖。
- 本地8010服务在空闲时优雅重启，health/live/ready通过；静态版本 `20260919-quant-strategies-v2`，目录15项、可载入8项。供应商当日请求 **0→0**，历史数据指针不变，未启动新扫描或训练模型。

复验核心新增入口：

```bash
.venv/bin/python -m pytest -q tests/test_strategy_quant_metrics.py tests/test_volume_confirmation_session_basis.py tests/test_market_strategy_templates.py tests/test_strategy_automation.py tests/test_strategy_execution.py tests/test_frontend_strategy_templates.py
npm run test:e2e -- tests/e2e/strategy-template-catalog.spec.js
```

本机验收汇总 `/tmp/ashare-quant-acceptance-20260919.json`；JUnit为同前缀的 `regression`、`supplement`、`engineering` 三份20260919 XML，浏览器报告 `/tmp/ashare-quant-browser-20260919.json`。临时证据不是新检出的运行前提。本轮完成输入可比性、策略筛选和状态修复；默认阈值仍未证明收益优势，下一步见[量化因子与策略](QUANT_FACTORS_AND_STRATEGIES.md)。

### 冻结评分跟踪（2026-09-19）

- 16个相关测试模块 **386项通过**，其中纯诊断30项、HTML报告27项、CLI10项；另有7项同日配对反例。覆盖冻结分组、缺失不补位、同分、日期等权、同日重扫、版本指纹、未来缓存、价格版本/公司行动、日历覆盖和WAL并发读取。
- 纯诊断行/分支覆盖 **100%**，报告渲染 **97.08%**，CLI **96.08%**。全项目增量综合覆盖 **91.34%**：确认前次源码摘要中仅已有 `market_scan_evaluation.py` 改变，清除其旧记录后重跑相关测试，并纳入本轮新文件。此数字沿用未变化模块的覆盖证据，不是本轮重跑全部测试。
- HTML在1440px桌面和390px移动浏览器验证，无页面横向溢出、外部请求为0，分组详情可展开；新增计数另在移动端复核。原工作台前端无修改，本轮未声称重跑全浏览器矩阵。
- 架构、工具索引、仓库、API依赖与异常安全 **70项通过**；Ruff、Mypy（435个显式源文件）、编译/Pyflakes、181个JS语法、API/函数索引及链接检查通过，当前源码差异与新增文件的脱敏秘密扫描通过。
- 最终真实只读跟踪选取16个冻结扫描，按版本与周期形成18个分组；D+1和D+5各只有2个可配对日期，D+20为0，各版本均不足以验证预测优势，全部区间不可用。报告在实际浏览器打开检查通过，外部请求为0。生成过程不请求提供者、不训练或晋级模型；输出及使用口径见[评分跟踪](SCORE_TRACKING.md)。

复验新增入口可运行：

```bash
.venv/bin/python -m pytest -q tests/test_market_scan_score_tracking.py tests/test_market_scan_paired_groups.py tests/test_score_tracking_report.py tests/test_score_tracking_cli.py
.venv/bin/python tools/track_market_scan_scores.py --database data/ashare_radar.sqlite3 --output-directory data/research/score_tracking
```

以下为此前迭代的历史验收范围，不与本轮测试数量相加。

### 个股因子聚合v3（2026-09-15）

固定三组预算、风险只扣分、画像不改方向权重；量能连续对称、收盘同价和连续20交易日准入；实时趋势关闭不匹配的历史校准与百分位。Alpha读取独立校验注册ID/用途/唯一性和历史版本，多周期及诊断去除重复加成；v3模型与前端核实总分及分解一致。公式、反例和边界见[评分第三版](SCORING_V3.md)。

| 范围 | 最终结果 |
| --- | --- |
| Python | 79个相关模块/类，2,049项及13个子测试全部通过，无跳过；首轮6个工程/夹具问题解决后在冻结源码重新运行整个相关集合，不累计重复测试数；非整库9千余项重跑 |
| 覆盖 | app/tools 82,757条语句、30,276个分支，综合91.31%；上轮全量基础合并本轮相关回归，所有变更Python旧覆盖先清除 |
| 浏览器 | 3个spec、4个项目共36项通过，无失败/跳过/flaky；零值、缺失份额、风险标签、旧版、损坏报告、转义与移动端布局通过，最终截图已检查 |
| 工程 | Ruff、Mypy432源文件、编译/Pyflakes、181个JS语法、运行版本、导入/链接、API/函数索引、架构/复杂度/类型范围、diff及变更秘密扫描通过；未降低门槛，无新增依赖 |
| 服务和数据 | 本地8010优雅重启成功，health/live/ready正常，静态版本20260915-factor-score-v3；供应商请求0→0，8项历史输入/报告/模型/指针摘要不变 |

本机汇总 `/tmp/ashare-score-v3-acceptance-20260915.json`，最终JUnit `/tmp/ashare-score-v3-final-regression-20260915.xml`，浏览器 `/tmp/ashare-score-v3-browser-final-20260915.json`。冻结源摘要689项。没有重训或晋级概率模型，测试通过不等于预测效果提升。

### 评分预测能力与历史同口径（2026-09-15）

当前与历史量价共用v2公式；D+1开盘至D+5/D+10收盘因子标签拒绝不完整或持有期公司行动窗口。充分度改为质量、固定因子份额覆盖与样本支持三者最小值，历史场景解释不再择优；新增方向验证v2的同日AUC、日期等权损失、多基准成对区间与固定0.6/0.7选择集合。详细目标、论文判断及未完成的后续统计合同见[修改方案](PREDICTION_IMPROVEMENT_PLAN.md)。

| 范围 | 实际结果与边界 |
| --- | --- |
| 相关Python | 74个模块/类的广回归1,812项及13个子测试通过，补验210项、最终说明修正后232项通过；三份JUnit按classname/name去重 **1,909项**，不累加重复用例，未重新运行整库9千余项 |
| 关键反例 | 当前/历史同分、开盘实体反向、未来后缀、缺量/PIT/坏价格、2拆1、固定退出日、中间缺日/停牌、单样本/方向对称、丢失弱因子不提高充分度、规则版本错配、正负场景并列及多基准/缺日期区块拒绝通过 |
| 综合覆盖 | **91.32%**，沿用上轮全量覆盖，先清除本轮变更Python模块记录后回归，Protocol类型接口与末次后端说明变更分别再次清除并补验；不称本轮全量重测 |
| 前端 | 分层评分、价值研究、工作台绑定3个spec在4个浏览器项目共 **32项通过**，无失败、跳过或flaky；最终文字修正后32项重新通过，不重复计数。覆盖0/小样本、旧版/缺失不伪造v2、转义与移动端无横向溢出，截图已检查 |
| 工程 | Ruff、Mypy 430显式源文件、编译/Pyflakes、181个JS语法、运行环境、导入/链接、API/函数索引、diff和变更秘密扫描通过，无新增依赖 |
| 实际历史 | 本地Choice60股票、502源日期，固定测试信号2026-05-26至2026-08-18；D+1/D+2/D+5同日AUC为0.5069/0.5032/0.5121，概率误差未优于校准期常数。配方与逐预测摘要保持；已见窗口属于诊断回放，不是新的独立检验 |
| 服务与数据 | 空闲时优雅重启，health/live/ready正常；最终静态版本20260915-predictive-evidence-2，供应商请求0→0，原始输入、旧报告、4个在线模型及历史指针8项摘要不变 |

本机证据：`/tmp/ashare-predictive-acceptance-20260915.json`；JUnit为同前缀的 `regression`、`supplement`、`final-supplement` 三份20260915 XML；最终浏览器报告 `/tmp/ashare-predictive-browser-final-20260915.json`。冻结源摘要687项；最后index变更仅缓存版本。密钥未读取或提交，本轮未推送GitHub。测试与修复不证明预测收益提升。

离线复验使用已存在的Choice manifest/database，通过 `tools/validate_experimental_direction.py --choice-history-manifest <manifest> --choice-history-database <database> --output-dir <新目录>` 输出新产物，设置 `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0`、`TRADE_CALENDAR_AUTO_FETCH=0` 禁止日历自动获取。完整相关回归可按上述模块和新测试索引运行，正式全量门禁继续使用第2节命令。

### 个股评分规则第二轮（2026-09-15验收）

9月14日开始的第二轮优化固定四类方向证据预算，风险面单独扣分，事件基线归中并排除已识别重复类别；个股相对行业修正改为连续且中性恒等，量价方向去除成交额/换手规模奖励。收盘时刻、严格前5日量基线及价格/涨跌幅一致性均进入量价准入，容差内按价格重算方向，矛盾或溢出输入不可用。前端披露证据分、风险扣分、槽位覆盖与实际相对调整；旧记录读取及缓存绑定有回归。

| 范围 | 实际结果与边界 |
| --- | --- |
| 受影响Python回归 | 最终广回归收集1,503个测试身份，其中4个用例的旧夹具存在价格/涨幅矛盾；仅修正夹具并保留原断言后，对两个完整模块、新方向契约和服务smoke补跑111项、6个子测试全部通过。逐身份合并JUnit后，1,503个相关用例及2个服务用例均有通过记录，不累加重复补测；本轮未重新运行整库9千余项 |
| 反例与数值边界 | 缺失正证据不转移预算、无风险不稀释方向、中性事件不奖励、相对修正连续/保号/恒等、平价50、量比对称、绝对成交额不改变方向、未来/重复/坏日K、完整量窗口、正负误报、容差与计算溢出、缓存错配均通过 |
| 覆盖率 | app/tools共82,380条语句、30,115个分支，综合91.31%，通过90%门槛；基于上轮全量覆盖合并本轮回归，6个变更Python模块的旧记录先清除，stock_activity最后逻辑修改后再次清除重测，不把此数字称为本轮重新执行的全量覆盖 |
| 浏览器 | 分层评分、价值研究、工作台绑定3个spec，在桌面/移动Chromium、Firefox、WebKit共28项通过，无失败、跳过或flaky；最终仅修正新评分spec的合成数据算式后，8项再次通过，不重复计数；移动截图已检查 |
| 工程检查 | Ruff、Mypy 423源文件、编译/Pyflakes、181个JS语法、运行环境、仓库链接/导入、API/函数索引及diff通过；当前差异与10个新文件的秘密扫描通过，无新增依赖 |
| 本地服务 | 无活动采集、扫描或定时任务时优雅重启成功，health/live/ready正常；供应商当日请求0→0、历史版本指针未变，静态版本为20260914-market-context-2 |

最终广回归后生产源摘要685项保持不变；补测只修测试输入，未放宽数据准入。规则版本为 `current-stock-overview.v2`、`current-market-context.v2` 和 `current-price-volume.v2`。这些回归证明算式和输入合同，不证明预测准确率或收益提升；固定预算、乘数和阈值仍未经样本外校准。正式全市场v5及历史重放未改用这些个股规则；实际供应商行情也未由本轮离线测试证明可用。

本机验收摘要为 `/tmp/ashare-score-rules-acceptance-20260915.json`；最终广回归与补验JUnit分别为 `/tmp/ashare-score-rules-final-regression-20260915.xml`、`/tmp/ashare-score-rules-input-supplement-20260915.xml`，浏览器报告为 `/tmp/ashare-score-rules-browser-20260914.json`、`/tmp/ashare-score-rules-final-browser-20260914.json`。临时证据不是新检出复验的前置条件。

### 个股大盘与行业分层评分（2026-09-14）

本轮全量非smoke收集9,062项：9,061通过，唯一失败是旧测试仍要求龙头复合观察权重大于零，按新去重合同修正。独立复核随后修复风险等级和量价方向中的质量混合，并排除行业背景事件重复计分；两个变更生产模块的旧覆盖记录先清除，再执行受影响研究、个股、工作台和工程回归，1,118项全部通过，另有13个子测试。2项服务smoke独立通过。将最终收集身份与三份JUnit逐项核对，**当前9,069个Python测试身份全部有通过记录**，不把重叠补测次数相加；全量另有57个子测试。

| 范围 | 实际结果与边界 |
| --- | --- |
| 计算和数据合同 | 共同市场上涨、行业变化、偏空/抗跌、质量只约束可靠性、行业/龙头去重、坏数、身份与时间错配、冷暖缓存和取消清理均有回归；新增4个真实构建反例在旧逻辑全部失败，修后通过 |
| 行与分支覆盖 | app/tools共82,302条语句、30,074个分支，综合91.30%，通过原90%门槛；全量867.45秒，受影响补测111.09秒，独立smoke25.65秒 |
| 浏览器 | 新分层评分、价值研究和工作台绑定3个spec，桌面/移动Chromium、Firefox、WebKit共28项通过；0失败、跳过或flaky，20.22秒。合成API验证缺失、0/负值、来源转义及旧响应兼容；桌面和移动截图已检查 |
| 工程检查 | Ruff、Mypy 423源文件、编译/Pyflakes、181个JS语法、运行环境、仓库链接/导入、API/函数索引和diff通过；无新增依赖。当前差异和8个新文件的秘密扫描通过 |
| 正式全市场边界 | 正式v5评分及重放调用链未变更；把6个个股函数替换为必抛异常后，合成正式/盘前扫描和重放仍通过，说明该上下文尚未进入正式全市场排序 |
| 本地服务 | 确认无活动采集、扫描或定时任务后优雅重启，健康检查正常；供应商当日计数0→0，既有历史版本指针保持不变。静态版本为20260914-market-context-1 |

全量开始后，生产源摘要只有 `stock_activity.py` 和 `stock_overview.py` 改变，这两项均在最终补测范围内，未沿用其旧覆盖数据。测试使用合成数据与临时数据库；工程通过不证明准确率或收益提升。实际行业公开端点连接核验失败，不能声称真实来源已经端到端可用；缺失时页面解释未采用的层。正式全市场升级的输入冻结、版本和样本外验证方案见[评分研究](SCORING_RISK_RESEARCH.md)。

本机验收摘要为 `/tmp/ashare-market-context-acceptance-20260914.json`，全量、补测、服务smoke的JUnit分别为 `/tmp/ashare-market-context-pytest-20260914.xml`、`/tmp/ashare-market-context-final-supplement-20260914.xml`、`/tmp/ashare-market-context-smoke-20260914.xml`。浏览器报告为 `/tmp/ashare-market-context-browser-20260914.json`。临时证据不是新检出运行或复验的前置条件。

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
