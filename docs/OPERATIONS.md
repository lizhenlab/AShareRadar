# 运行手册

## 1. 环境与启动

在项目根目录执行。应用支持 Python 3.12；开发检查支持 Node.js 22/24 与 npm 10/11。

```bash
export PROJECT_ROOT="$PWD"
export PYTHON="$PROJECT_ROOT/.venv/bin/python"
export PYTHONNOUSERSITE=1
python3.12 -m venv .venv
$PYTHON -m pip install --require-hashes -r requirements-lock.txt
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --timeout-graceful-shutdown 5
```

后台本地开发可使用独立 screen 会话：

```bash
screen -dmS ashare_radar bash -lc 'cd "$PROJECT_ROOT" && exec env PYTHONNOUSERSITE=1 "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --timeout-graceful-shutdown 5 > /tmp/ashare_radar.log 2>&1'
tail -f /tmp/ashare_radar.log
```

保持单 worker：调度和扫描控制状态是进程内的。统一 runtime-leader 租约可阻止两个进程同时拥有正常后台任务，但不代表支持多 worker 部署。HTTP 优雅关闭预算与提供者工作停止是不同边界；取消抵抗的任务结束前租约不会释放。

## 2. 停止与升级

向本项目的 Uvicorn 进程发送 Ctrl-C / SIGTERM，等待进程退出，再启动新实例。使用 screen 时：

```bash
screen -S ashare_radar -X stuff $'\003'
lsof -nP -iTCP:8010 -sTCP:LISTEN
```

监听器必须消失；同时确认没有其他使用同一数据库的旧进程。不要只关闭终端、删除锁文件或启动第二个实例掩盖卡住的旧任务。

所有数据库迁移都在停机窗口进行：先备份并验证，停止全部旧服务，再运行新版本。UTC 审计时间迁移要求明确旧时区、有足够空间和未占用的租约；无效旧数据会回滚。结构迁移先建立兼容列/索引、验证封存图，再安装依赖它们的不可变触发器；迁移标记最后写入。旧来源无法核实时保持未验证状态，禁止手工补摘要或改状态伪造成功。

## 3. 数据、备份与恢复

`data/` 是本机运行状态，包含 SQLite/WAL/SHM、租约、交易日历、研究产物与备份，不提交到源码仓库。锁文件存在不等于锁被占用；运行中的文件不得删除或重建。

完整数据库备份使用 SQLite backup API，同时生成摘要、表计数及完整性清单：

```bash
$PYTHON tools/runtime_data.py backup
$PYTHON tools/runtime_data.py verify data/backups/ashare_radar_TIMESTAMP
```

使用命令输出的实际路径代替示例时间戳。管理目录默认保留两份备份；显式外部目标不参与自动轮转。验证要求摘要、SQLite integrity_check 与 foreign_key_check 全部通过。

Restore a backup while the service is stopped. 恢复工具先验证源、检查数据库与运行租约、创建目标回滚快照，原子替换后再次验证：

```bash
$PYTHON tools/runtime_data.py restore data/backups/ashare_radar_TIMESTAMP --confirm-service-stopped
```

SQLite 备份不包含研究侧文件。任何受管概率、来源、结果、拟合、未来区间或个股概率目录非空，或固定摘要存在时，DB-only restore 都会直接拒绝；即使文件看起来来自同一批次也不例外。工具不比较这些侧产物是否同源，也不提供成套恢复，应先独立核验并安排一致恢复方案。

Before deleting or replacing local data，先停止服务并创建、验证备份。备份和恢复都针对 Settings 解析后的同一数据库路径；不要把自定义数据库的备份与默认路径的删除命令混用。本手册不提供绕过目标检查的裸文件删除命令。常规维护使用工具页的清理预览与受检维护操作；需要彻底重置时，先核对实际配置、数据库及全部关联侧文件，再制定明确的备份与重置范围。

不要用复制活跃 WAL 文件代替一致性备份；保留研究文件也不意味着新建空数据库会使这些产物有效。

工具页用户数据导出只包含允许的自选、笔记、预警、研究历史、复盘与模拟记录，不包含凭证、缓存和完整运行来源图。导入支持 merge/replace，先 dry-run；文件、模式、时区或目标状态变化后必须重新预览。不可变历史同键异值拒绝整次导入；父子键重映射后仍验证摘要与外键。replace 前创建当前备份。

## 4. 日常工作与诊断

```bash
curl -sS http://127.0.0.1:8010/api/health/live
curl -sS http://127.0.0.1:8010/api/health/ready
curl -sS http://127.0.0.1:8010/api/data/status
curl -sS http://127.0.0.1:8010/api/system/reliability
```

liveness 只证明进程活着；readiness 与数据源健康分别检查可服务状态和实际可用性。网络失败先查看能力级冷却、准入压力、报价/K 线时间与市场覆盖，不能反复新建扫描绕过失败原因。

- 个股普通加载不发布正式建议；盘后研究队列要求完整收盘证据。概率面板只读取数据目录里的验证产物，无产物时应显示不可用。
- 全市场页面显式新建扫描；读取旧发布、筛选和导出不抓取提供者或修改分数。股票缺失可跳过；全链路故障保持 pending 并执行有界批次恢复。
- 自动扫描默认关闭；启用前检查交易日历、市场池覆盖、提供者预算与实际数据容量。
- 复盘计划修改/归档须使用当前 revision；历史修订和评估保留。模拟只有明确请求才运行，不连接券商。
- 浏览器通知依赖页面保持打开，没有后台 service worker。草稿失败时保留，切换股票不能让旧响应覆盖当前记录。
- AKShare/Tushare/Futu 为可选来源；未配置时应明确不可用。演示源不应参与真实研究。
- 内部模型构造、数据库和提供者错误返回脱敏不可用信息；请求字段错误仍为 422，完整性冲突保留独立类别。

离线研究输入、输出与授权边界见[研究工具](RESEARCH.md)。线上 canary 会访问真实提供者，不能把它混入离线 CI。

## 5. 环境变量

Use the `ASHARE_RADAR_*` namespace for new configuration. Legacy aliases are accepted where listed for local compatibility. Process environment values take precedence. For the five allowlisted `ASHARE_RADAR_LLM_*` names only, the application falls back to simple top-level assignments in `$HOME/.zshrc`; it parses that file without sourcing or executing it and ignores command substitutions, nested shell blocks, and unrelated names. When that file contains `ASHARE_RADAR_LLM_API_KEY`, it must be owned by the current user and have no group/other permissions; run `chmod 600 "$HOME/.zshrc"` before startup. It does not read `.env` files, project configuration, user-data imports, or browser storage for credentials. Settings are captured by the application container, and scheduler intervals/task registration are not hot-reloaded. Restart the single process after changing configuration.

| Variable | Default | Legacy alias | Notes |
| --- | --- | --- | --- |
| `ASHARE_RADAR_LLM_API_KEY` | empty | - | Secret; process environment first, then the allowlisted `$HOME/.zshrc` fallback. |
| `ASHARE_RADAR_LLM_BASE_URL` | empty | - | OpenAI-compatible absolute endpoint; HTTPS is required except for loopback development, and query/fragment/userinfo components are rejected. |
| `ASHARE_RADAR_LLM_MODEL` | empty | - | LLM explanation model; required together with API key and base URL. |
| `ASHARE_RADAR_LLM_ENABLED` | `1` | - | Set `0` to force rule-only answers. |
| `ASHARE_RADAR_LLM_TIMEOUT_SECONDS` | `30` | - | Positive finite total budget shared by initial generation and the optional validation-correction request. The browser allows 35 seconds so it does not abort before this server budget expires. |
| `ASHARE_RADAR_TUSHARE_TOKEN` | empty | `TUSHARE_TOKEN` | Secret for optional Tushare provider. |
| `ASHARE_RADAR_FUTU_ENABLED` | `0` | `FUTU_ENABLED` | Requires local Futu OpenD. |
| `ASHARE_RADAR_FUTU_HOST` | `127.0.0.1` | `FUTU_HOST` | Futu OpenD host. |
| `ASHARE_RADAR_FUTU_PORT` | `11111` | `FUTU_PORT` | Futu OpenD port. |
| `ASHARE_RADAR_DEMO_PROVIDER_ENABLED` | `0` | `DEMO_PROVIDER_ENABLED` | Demo data must stay disabled for real research. |
| `ASHARE_RADAR_CORS_ALLOW_ORIGINS` | local 8010 origins | `CORS_ALLOW_ORIGINS` | Comma-separated CORS origins; for browser mutations/refresh writes, both the Host-derived origin and supplied Origin/Referer must be in this list. |
| `ASHARE_RADAR_CACHE_PATH` | project `data/ashare_radar.sqlite3` | `CACHE_PATH` | Absolute path or project-root-relative SQLite path. |
| `ASHARE_RADAR_LEGACY_AUDIT_TIMEZONE` | `Asia/Shanghai` | - | IANA timezone used only to interpret legacy naive audit timestamps during the first UTC migration and user-data import. Set it before first startup when an old database was written in another host timezone; new audit timestamps are fixed-width UTC `Z`. |
| `ASHARE_RADAR_MINUTE_KLINE_CACHE_SECONDS` | `60` | `MINUTE_KLINE_CACHE_SECONDS` | Minute K-line cache TTL. |
| `ASHARE_RADAR_STOCK_POOL_AUTHORITATIVE_MIN_COUNT` | `1000` | `STOCK_POOL_AUTHORITATIVE_MIN_COUNT` | Fresh cache count needed to confirm an empty stock search. |
| `ASHARE_RADAR_STOCK_POOL_PROVIDER_TIMEOUT_SECONDS` | `60` | - | Timeout for one full stock-pool provider call; range 1-300 seconds. Kept separate from short quote/K-line calls because exchange-list fallbacks may require several pages. |
| `ASHARE_RADAR_TENCENT_KLINE_MAX_IN_FLIGHT` | `5` | - | Tencent daily-K async request capacity; range 1-16. This is isolated from blocking backup providers, which retain the generic two-call admission limit. Keep it no higher than the scan K-line concurrency unless a measured provider test justifies otherwise. |
| `ASHARE_RADAR_STOCK_CONCEPT_CACHE_SECONDS` | `21600` | `STOCK_CONCEPT_CACHE_SECONDS` | Stock concept cache TTL. |
| `ASHARE_RADAR_PROVIDER_FAILURE_COOLDOWN_SECONDS` | `90` | `PROVIDER_FAILURE_COOLDOWN_SECONDS` | Provider retry cooldown after failures. |
| `ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY` | `tencent,futu,akshare` | - | 报价来源优先级；仅使用可用且支持该能力的来源。 |
| `ASHARE_RADAR_KLINE_PROVIDER_PRIORITY` | `tencent,akshare,tushare,baostock` | - | 完整日线来源优先级。 |
| `ASHARE_RADAR_MINUTE_PROVIDER_PRIORITY` | `futu,akshare` | - | 分钟线来源优先级。 |
| `ASHARE_RADAR_STOCK_PROVIDER_PRIORITY` | `akshare,tushare,baostock,local` | - | 股票池来源优先级。 |
| `ASHARE_RADAR_PLATE_PROVIDER_PRIORITY` | `akshare,local` | - | 板块来源优先级。 |
| `ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_ENABLED` | `1` | - | 扫描前验证提供者能力与准备状态。 |
| `ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS` | `30` | - | preflight 总等待预算，范围 0.1–300 秒。 |
| `ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS` | `600,1800,3600` | - | 自动重试延迟序列，每项 1–86400 秒。 |
| `ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS` | `3` | - | 自动重试上限，范围 0–10。 |
| `ASHARE_RADAR_MARKET_SCAN_AUTO_ENABLED` | `0` | - | Enable the after-close full-market scan. |
| `ASHARE_RADAR_MARKET_SCAN_SCHEDULE_HOUR` | `16` | - | Automatic-scan local hour; the 15:15 daily publication floor still applies. |
| `ASHARE_RADAR_MARKET_SCAN_SCHEDULE_MINUTE` | `30` | - | Automatic-scan local minute. |
| `ASHARE_RADAR_MARKET_SCAN_BATCH_SIZE` | `50` | - | Symbols per persisted scan batch; range 1-500. |
| `ASHARE_RADAR_MARKET_SCAN_CONCURRENCY` | `5` | - | Maximum concurrent per-symbol K-line jobs; range 1-32. |
| `ASHARE_RADAR_MARKET_SCAN_KLINE_LIMIT` | `260` | - | Requested completed `qfq` daily rows per symbol; range 60-1000. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_HISTORY_ROWS` | `60` | - | Minimum complete daily rows required for ranking; range 60-260 and no greater than the K-line limit. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE` | `50` | - | Results below this 0-100 quality floor are skipped. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_UNIVERSE_COUNT` | `4000` | - | Reject a purported full-market pool below this total count. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_SH_COUNT` | `1800` | - | Reject a scan pool with fewer Shanghai A shares. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_SZ_COUNT` | `2500` | - | Reject a scan pool with fewer Shenzhen A shares. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_BJ_COUNT` | `200` | - | Reject a scan pool with fewer Beijing A shares. |
| `ASHARE_RADAR_MARKET_SCAN_SYMBOL_TIMEOUT_SECONDS` | `30` | - | Timeout for one symbol's K-line attempt; range 0.1-300 seconds. |
| `ASHARE_RADAR_MARKET_SCAN_QUOTE_BATCH_TIMEOUT_SECONDS` | `60` | - | Outer timeout for one quote batch; range 0.1-600 seconds. |
| `ASHARE_RADAR_MARKET_SCAN_RETRY_ATTEMPTS` | `2` | - | K-line attempts per symbol; range 1-5. |
| `ASHARE_RADAR_MARKET_SCAN_RETRY_BACKOFF_SECONDS` | `1` | - | Linear delay multiplier between K-line attempts; range 0-30 seconds. |
| `ASHARE_RADAR_MARKET_SCAN_BATCH_RETRY_ATTEMPTS` | `3` | - | Attempts for the pending subset of a batch after a system-wide quote/daily-K chain outage; range 1-5 and independent of per-symbol K-line retries. |
| `ASHARE_RADAR_MARKET_SCAN_PROVIDER_WAIT_BUDGET_SECONDS` | `120` | - | Cumulative actual provider-recovery sleep budget across one scan's pending work; range 0-600 seconds. Exhaustion fails the run while affected rows remain pending; `0` disables recovery sleeps. |
| `ASHARE_RADAR_MARKET_SCAN_NEW_STOCK_DAYS` | `120` | - | Calendar-day window used only for the new-stock tag; range 1-730. |
| `ASHARE_RADAR_MARKET_SCAN_OFFICIAL_EXECUTION_REGISTRY_PATH` | `data/research/market_scan_official_execution/source-registry.json` | - | Operator-reviewed licensed official-source registry. Its contents alone never grant authority. |
| `ASHARE_RADAR_MARKET_SCAN_OFFICIAL_EXECUTION_REGISTRY_DIGEST` | unset | - | Out-of-band pinned lowercase SHA-256 of the official-source registry. When unset, formal execution evidence stays explicitly unconfigured and public vendor data is never promoted. |
| `ASHARE_RADAR_MARKET_SCAN_OFFICIAL_EXECUTION_RAW_ROOT` | `data/research/market_scan_official_execution_raw` | - | Root containing immutable raw licensed delivery files referenced by official session receipts. Every file is read without symlink traversal and checked for exact size and SHA-256. |
| `ASHARE_RADAR_MARKET_SCAN_OFFICIAL_EXECUTION_SESSION_DIRECTORY` | `data/research/market_scan_official_execution/sessions` | - | Content-addressed normalized official execution-session artifacts. Loading also replays the pinned registration, license interval, source URI, raw-file bytes, unadjusted OHLCV/amount, effective trading rules, corporate actions, and complete decision coverage. |
| `ASHARE_RADAR_MARKET_SCAN_JOINT_EXECUTION_AUTHORIZATION_PATH` | `data/research/market_scan_joint_execution/authorization/active.json` | - | Operator-installed exact probability-filter authorization artifact. The file is non-authorizing until its digest is independently pinned and strict replay against the selected H5 study succeeds. |
| `ASHARE_RADAR_MARKET_SCAN_JOINT_EXECUTION_AUTHORIZATION_DIGEST` | unset | - | Out-of-band lowercase SHA-256 pin for the formal H5 filter authorization. Unset, stale, mismatched or superseded evidence keeps probability filtering disabled. |
| `ASHARE_RADAR_MARKET_SCAN_PROBABILITY_RANKING_CONTROL_PATH` | `data/research/market_scan_joint_execution/ranking-control/active.json` | - | Human-authored exact `promote` or `rollback` control for `full-market-score-v6`; automation must not create or silently replace it. |
| `ASHARE_RADAR_MARKET_SCAN_PROBABILITY_RANKING_CONTROL_DIGEST` | unset | - | Out-of-band lowercase SHA-256 pin for the ranking control. Promotion requires a qualified Shadow and applies only to later official runs; rollback is persisted durably. |
| `ASHARE_RADAR_SCHEDULER_ENABLED` | `1` | `SCHEDULER_ENABLED` | Local refresh scheduler switch. |
| `ASHARE_RADAR_SCHEDULER_QUOTE_INTERVAL_SECONDS` | `30` | `SCHEDULER_QUOTE_INTERVAL_SECONDS` | Quote refresh interval. |
| `ASHARE_RADAR_SCHEDULER_KLINE_INTERVAL_SECONDS` | `900` | `SCHEDULER_KLINE_INTERVAL_SECONDS` | K-line refresh interval. |
| `ASHARE_RADAR_SCHEDULER_PLATE_INTERVAL_SECONDS` | `300` | `SCHEDULER_PLATE_INTERVAL_SECONDS` | Plate refresh interval. |
| `ASHARE_RADAR_SCHEDULER_HEALTH_INTERVAL_SECONDS` | `45` | `SCHEDULER_HEALTH_INTERVAL_SECONDS` | Data-health check interval. |
| `ASHARE_RADAR_SCHEDULER_KLINE_SYMBOLS_LIMIT` | `5` | `SCHEDULER_KLINE_SYMBOLS_LIMIT` | Per-cycle K-line symbol cap. |
| `ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | `5` | `SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | Bounded scheduler stop wait; unified runtime leadership is not released while unfinished service work is still shutting down. |
| `ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS` | `120` | `MAX_QUOTE_HISTORY_ROWS` | Per-symbol daily quote-history cap; minimum `120`, matching the analysis window. |
| `ASHARE_RADAR_MAX_DAILY_KLINE_ROWS` | `260` | `MAX_DAILY_KLINE_ROWS` | Per-symbol and adjustment-mode daily K-line cap; must cover `ASHARE_RADAR_MARKET_SCAN_KLINE_LIMIT`. |
| `ASHARE_RADAR_MAX_MINUTE_KLINE_ROWS` | `20000` | `MAX_MINUTE_KLINE_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_STOCK_CONCEPT_ROWS` | `20000` | `MAX_STOCK_CONCEPT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_TASK_RUN_ROWS` | `2000` | `MAX_TASK_RUN_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_RELIABILITY_BUCKET_ROWS` | `10000` | - | Global retention cap for low-cardinality UTC-hour reliability aggregates; minimum `1`. |
| `ASHARE_RADAR_MAX_MARKET_SCAN_RUNS` | `30` | - | Newest-run keep-window target. Every unreferenced sealed graph outside the window is eligible and deleted as a verified result/run graph; active work and genuine database/file references may keep the physical count above target, and every noncandidate/directly protected retry root pins all reachable candidate ancestors. A run's outward `task_run_id` is not a pin. |
| `ASHARE_RADAR_MAX_MONITOR_EVENT_ROWS` | `3000` | `MAX_MONITOR_EVENT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_CACHE_EVENT_ROWS` | `5000` | `MAX_CACHE_EVENT_ROWS` | Runtime retention cap for cache/provider events. |
| `ASHARE_RADAR_MAX_ALERT_EVENT_ROWS` | `5000` | `MAX_ALERT_EVENT_ROWS` | Runtime retention cap for alert events. |
| `ASHARE_RADAR_MAX_ADVICE_HISTORY_ROWS` | `20000` | `MAX_ADVICE_HISTORY_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_DATABASE_SIZE_MB` | `2048` | - | Local SQLite and managed-backup capacity budget in MiB; sized for one full-market daily cache plus two backups, minimum `16`. Diagnostics warn at 80%. |
| `ASHARE_RADAR_RUNTIME_MAINTENANCE_INTERVAL_SECONDS` | `3600` | - | Minimum interval between automatic regenerable-data maintenance passes; range 60-604800 seconds. |
| `ASHARE_RADAR_MAX_RUNTIME_BACKUPS` | `2` | - | Managed runtime backup bundles retained per database; range 2-100. The default preserves two recovery points without multiplying the full-market cache footprint. API/CLI backup and restore operations pass this limit explicitly. |
| `ASHARE_RADAR_ADVICE_HISTORY_DEDUPE_SECONDS` | `180` | `ADVICE_HISTORY_DEDUPE_SECONDS` | Advice-history de-duplication window. |
| `ASHARE_RADAR_QUOTE_STALE_WARNING_SECONDS` | `900` | `QUOTE_STALE_WARNING_SECONDS` | Quote freshness warning threshold. |
| `ASHARE_RADAR_QUOTE_CONSISTENCY_WARNING_PCT` | `1.0` | `QUOTE_CONSISTENCY_WARNING_PCT` | Multi-source price-difference warning threshold. |
| `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH` | `0` | `TRADE_CALENDAR_AUTO_FETCH` | Non-blocking single-flight background refresh when runtime is missing, invalid, stale for the current date, or cannot cover a target. The triggering call uses the current bundle/closed decision; later calls see a successful atomic runtime update. |

Missing optional values use documented defaults. Present but malformed boolean, numeric, path, or LLM endpoint values fail configuration at startup instead of silently changing behavior. Restart after changing process variables or the allowlisted LLM assignments in `$HOME/.zshrc`.

### Provider Canary Variables

`tools/provider_canary.py` owns three tool-only environment variables. They are intentionally not `Settings` fields and are outside the application's configuration-document coverage contract:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASHARE_RADAR_CANARY_SH_SYMBOL` | `600519.SH` | Representative Shanghai symbol. |
| `ASHARE_RADAR_CANARY_SZ_SYMBOL` | `000001.SZ` | Representative Shenzhen symbol. |
| `ASHARE_RADAR_CANARY_BJ_SYMBOL` | `920066.BJ` | Representative Beijing symbol. |

Each value is normalized and must belong to the named market. Equivalent `--sh-symbol`, `--sz-symbol`, and `--bj-symbol` flags override the defaults. `--request-timeout` controls each representative quote/K-line probe and defaults to `provider_call_timeout_seconds`; `--stock-pool-timeout` independently controls the larger stock-pool refresh and defaults to `stock_pool_provider_timeout_seconds`. `--overall-timeout` remains the final deadline for the concurrent set.

The CLI creates and removes a temporary SQLite database, disables the scheduler for that isolated DataHub, and concurrently checks:

- one direct non-cached quote for each SH/SZ/BJ representative;
- one direct five-row completed daily-K request per representative; the response must contain exactly five ordered usable rows and pass finite OHLCV, cache/fallback, future-date, and staleness validation;
- a refreshed stock pool with valid unique identity rows and at least one SH, SZ, and BJ member.

Output is one sanitized JSON object. Exit `0` means every market and the stock-pool contract were available, `2` means at least one market remained available but the full contract was partial, and `1` means no market was available or provider cleanup failed. This is a live-provider diagnostic, not a required CI test:

```bash
$PYTHON tools/provider_canary.py
```

### LLM Remote Data Boundary

When LLM enhancement is enabled, the app sends an OpenAI-compatible chat-completion request to the configured remote endpoint. Chat messages contain the current question and topic; symbol and stock name; the deterministic rule answer; authoritative conclusion, confidence, support, resistance, actions, and invalidations; selected current quote/MA/trend/risk facts; data-quality score, level, source, and at most four notes; and at most six rule-evidence items. Local watchlists, stock notes, alert rules/events, advice history, provider credentials, full workbench payloads, and other local collections are not sent.

The transport also sends the configured model name, generation parameters, and `ASHARE_RADAR_LLM_API_KEY` as authentication to that endpoint. The API key is not inserted into chat messages, but the remote service necessarily receives it as a request credential. Use only an endpoint whose data-handling policy is acceptable, or set `ASHARE_RADAR_LLM_ENABLED=0` to keep Q&A rule-only.

The first model response is validated locally. If and only if that output fails local validation, the app may send one format-correction request with the same bounded context and a stricter instruction that the explanation contain no numbers or action words; it does not resend the previous raw model output. One outer timeout covers the first request, local validation, and correction together, so correction receives only the remaining `ASHARE_RADAR_LLM_TIMEOUT_SECONDS` budget rather than a new full timeout. The SDK's automatic retries are disabled. A request error, total-budget expiry, or second validation failure returns the deterministic rule answer without another remote attempt.


## 6. 验证与依赖维护

完整本地交付命令见[测试计划](TEST_PLAN.md)。以下安全与供应链检查需要相应工具/网络，应明确记录实际执行结果。

The Security workflow is an additional required gate. Its local-equivalent dependency and reproducible-SBOM checks are:

```bash
$PYTHON -m pip install --only-binary=:all: --require-hashes -r requirements-security-lock.txt
$PYTHON -m pip check
npm ci --ignore-scripts
$PYTHON -m pip_audit --require-hashes --disable-pip --strict --progress-spinner off --requirement requirements-lock.txt
$PYTHON -m pip_audit --require-hashes --disable-pip --strict --progress-spinner off --requirement requirements-dev-lock.txt
$PYTHON -m pip_audit --require-hashes --disable-pip --strict --progress-spinner off --requirement requirements-security-lock.txt
npm audit --audit-level=high
first_dir="$(mktemp -d)"
second_dir="$(mktemp -d)"
$PYTHON tools/generate_sbom.py --output-dir "$first_dir"
$PYTHON tools/generate_sbom.py --output-dir "$second_dir"
diff -ru "$first_dir" "$second_dir"
rm -rf "$first_dir" "$second_dir"
```

CI additionally installs a checksum-verified Gitleaks binary, scans the current source and complete Git history with `--redact=100`, and uploads the normalized CycloneDX artifacts. Do not replace that history scan with a latest-tree grep. `tests/test_supply_chain.py` guards SHA-pinned actions, disabled checkout credential persistence, both Python lock audits, npm audit, redacted current/history scans, Dependabot ecosystems, and two-run SBOM comparison.

### 更新依赖与生成文档

Keep direct dependencies in the appropriate input file. Runtime libraries belong in `requirements.txt`; test, lint, type, and lock-compilation tools belong in `requirements-dev.txt`; only vulnerability-audit and SBOM generators belong in `requirements-security.txt`. These inputs are for lock generation: reproducible installs use `--require-hashes` and the generated locks. A runtime-input change requires rebuilding the runtime and development locks because `requirements-dev.txt` includes `requirements.txt`; a development-only or security-tool change requires rebuilding only its matching lock. Do not edit generated locks by hand. Verify the locks in clean Python 3.12 environments:

```bash
$PYTHON -m piptools compile --generate-hashes \
  --output-file=requirements-lock.txt requirements.txt
$PYTHON -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements-dev-lock.txt requirements-dev.txt
$PYTHON -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements-security-lock.txt requirements-security.txt
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
$PYTHON -m pip install --only-binary=:all: --require-hashes -r requirements-security-lock.txt
$PYTHON -m pip check
```

After any dependency lock changes, audit all three Python locks, run `npm audit`, and regenerate both SBOMs. `tools/generate_sbom.py` consumes `requirements-lock.txt` and `package-lock.json`, validates CycloneDX JSON, removes volatile serial/timestamp fields, imposes deterministic ordering, and writes `python.cdx.json` plus `npm.cdx.json` atomically. The Security workflow generates twice and compares bytes before artifact upload. Its separate tool lock prevents an audit-only Linux runner from building optional provider source distributions. A reproducible SBOM is an inventory aid; it is not a signed release or provenance attestation.

Dependabot runs weekly for pip, npm, and GitHub Actions. Review generated changes through the same tests instead of merging solely because a version is newer. Keep every `uses:` reference pinned to a reviewed 40-character commit SHA and preserve `persist-credentials: false` for checkout.

Regenerate inventory files only when accepting their source changes. CI and review should use the non-mutating checks:

```bash
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
```
