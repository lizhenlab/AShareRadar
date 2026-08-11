# AShareRadar

AShareRadar is a local A-share research workbench. It combines trustworthy full-market SH/SZ/BJ scanning, replayable deterministic ranking, saved discovery presets, and a local research/review loop with code/name lookup, adjustment-aware daily K-lines, inspectable intraday charts, trend and risk analysis, versioned advice history, fixed-condition watchlist scans, notes, alerts, local data portability, and system diagnostics in a Chinese FastAPI web application backed by SQLite.

It is a research assistant, not an automated trading system. It does not connect to brokerage accounts, place orders, or provide investment advice.

## Quick Start

Python 3.12 is the supported runtime. Set `PROJECT_ROOT` to the checkout and use a project-local virtual environment so dependencies remain isolated when user site-packages are disabled.

```bash
export PROJECT_ROOT="$(pwd)"
python3.12 -m venv "$PROJECT_ROOT/.venv"
source "$PROJECT_ROOT/.venv/bin/activate"
export PYTHON="$PROJECT_ROOT/.venv/bin/python"
export PYTHONNOUSERSITE=1
$PYTHON -m pip install --require-hashes -r requirements-lock.txt
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --timeout-graceful-shutdown 5
```

Open `http://127.0.0.1:8010`.

The five-second Uvicorn graceful-shutdown bound prevents an open SSE stream or in-flight data request from keeping request draining pending indefinitely. Application/provider cleanup follows with its own bounded waits; the daemon provider-worker boundary described below handles an SDK call that cannot be interrupted.

For development, install `requirements-dev-lock.txt` directly in the virtual environment; it already contains the complete runtime resolution plus the engineering tools. Node.js and npm are development tools and are not required to run the web application. The checked contract is Python 3.12.x, Node.js 22.x or 24.x, and npm 10.x or 11.x; `.node-version` selects Node 22 as the local default while CI keeps a separate Node 24 compatibility smoke job:

```bash
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
npm ci
$PYTHON tools/runtime_contract.py
npm run check
$PYTHON -m ruff check app tests tools
$PYTHON -m mypy
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
npx --no-install playwright install chromium firefox webkit
npm run test:e2e
```

The three `requirements*.txt` inputs define runtime, development, and isolated security-tool profiles; their matching `*-lock.txt` files are the reproducible installation entrypoints. Install the application from `requirements-lock.txt` and development/CI from `requirements-dev-lock.txt`. The development lock includes runtime dependencies, while `requirements-security-lock.txt` deliberately contains only vulnerability-audit and SBOM tooling so the Linux Security runner never builds optional market-provider SDKs. JavaScript development dependencies are pinned by `package-lock.json` and installed with `npm ci`. `npm run check` remains the convenient local regression command; CI additionally enforces the runtime declaration, Ruff, ratcheted mypy, 90% branch coverage, `pip check`, JavaScript syntax, browser regression, and generated-document drift.

## Documentation

- [Requirements Specification](docs/REQUIREMENTS.md)
- [Software Design Description](docs/DESIGN.md)
- [API Reference](docs/API_REFERENCE.md)
- [Test Plan and Test Report](docs/TEST_PLAN.md)
- [Operations Guide](docs/OPERATIONS.md)
- [Maintenance and Refactor Guide](docs/MAINTENANCE.md)
- [Function Inventory](docs/FUNCTION_INVENTORY.md)
- [Engineering Quality Audit (verified 2026-07-24)](docs/research/ENGINEERING_QUALITY_AUDIT_2026.md)
- Research: [2026 Core Feature Study](docs/research/COMPETITOR_CORE_FEATURES_2026.md), [Current Capability Audit](docs/research/CURRENT_CAPABILITY_AUDIT.md), and [Product Gap and Roadmap](docs/research/PRODUCT_GAP_AND_ROADMAP.md)

Competitor, capability, gap, and roadmap research dated July 15-16, 2026 is retained as historical decision context. It is not a current implementation contract; use the design, operations, test plan, and dated engineering-quality audit for the current worktree.

Regenerate generated references after moving routes or Python functions:

```bash
$PYTHON tools/architecture_inventory.py
$PYTHON tools/api_inventory.py
```

Verify generated references without rewriting them:

```bash
$PYTHON tools/architecture_inventory.py --check
$PYTHON tools/api_inventory.py --check
```

## Configuration

Settings are read from process environment variables at startup. For the five `ASHARE_RADAR_LLM_*` variables only, a simple top-level assignment in `$HOME/.zshrc` is also accepted when the process environment does not define it; the profile is parsed, never executed. Project files do not store LLM credentials. Invalid boolean, numeric, path, or LLM endpoint values stop startup with a readable configuration error.

```bash
export ASHARE_RADAR_LLM_API_KEY="your OpenAI-compatible key"
export ASHARE_RADAR_LLM_BASE_URL="https://your-openai-compatible-endpoint/v1"
export ASHARE_RADAR_LLM_MODEL="your model name"
export ASHARE_RADAR_LLM_ENABLED=1
chmod 600 "$HOME/.zshrc"
```

Optional provider settings:

```bash
export ASHARE_RADAR_TUSHARE_TOKEN="your token"
export ASHARE_RADAR_FUTU_ENABLED=1
export ASHARE_RADAR_FUTU_HOST=127.0.0.1
export ASHARE_RADAR_FUTU_PORT=11111
```

Provider order can be overridden with comma-separated names:

```bash
export ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY="tencent,futu,akshare"
export ASHARE_RADAR_KLINE_PROVIDER_PRIORITY="tencent,akshare,tushare,baostock"
```

When configured and available, Futu joins the quote fallback chain and Tushare
joins the daily-K fallback chain. Missing optional SDKs, a disabled Futu OpenD,
or a missing Tushare token are skipped by capability rather than treated as
provider failures.

Legacy variables such as `TUSHARE_TOKEN`, `FUTU_ENABLED`, and `SCHEDULER_*` remain accepted as aliases. New configuration should use the `ASHARE_RADAR_*` namespace.

## Full-Market Ranking

The browser UI has four top-level work areas: **个股研究**, **全市场选股**, **复盘工具**, and **自选监控**. Each area exposes only its relevant controls; stock research keeps its focused secondary tabs, while the full-market leaderboard uses the entire main workspace instead of appearing below the stock chart.

Open **全市场选股** and click **开始扫描**. `POST /api/market-scans` accepts `mode=official|intraday` and defaults to `official` when `mode` is omitted. The API immediately creates a background run; the page then polls real progress and publishes a stable, pageable snapshot only after that run reaches a terminal state.

- The universe is the current listed A-share pool across SH, SZ, and BJ. It has no fixed 5,000-row truncation, rejects Shanghai/Shenzhen B shares, excludes delisted rows, and retains explicit ST/new-stock tags. Provider rows are normalized and de-duplicated once; coverage checks and the atomic cached-pool replacement use that same candidate set. Guards enforce both a 4,000-name total and configurable minimums for each market, require at least 98% retention against the latest authoritative snapshot, and reject a large per-market drop before per-stock work begins. Industry and listing-date completeness are reported separately instead of silently turning unknown metadata into a confident classification.
- Suspended stocks, short histories, stale quotes/K-lines, malformed rows, and genuine per-symbol coverage misses remain visible as `skipped` or `missing`; they never enter the ranking as zero-score stocks. A system-wide quote or daily-K provider-chain outage is different: the affected rows stay `pending`, the batch receives bounded retries within one cumulative provider-wait budget, and an unrecovered run fails in a retryable state instead of bulk-writing false `missing` results.
- Each symbol refresh asks DataHub for up to 260 completed `qfq` daily bars and requires a real provider response. A compatible cache is only an incremental-refresh base or an explicitly marked fallback; it cannot satisfy that freshness requirement by itself. The default chain is Tencent's current SH/SZ/BJ `newfqkline` interface, AKShare (with its internal Eastmoney-direct and final Sina-forward-adjusted fallbacks), optional token-backed Tushare, and BaoStock. Unavailable optional providers are skipped by capability. Overlap verification detects adjustment rebases and triggers a full refresh. Cache persistence retains quote/K-line fallback provenance, and older K-line vintages cannot replace newer equal-length snapshots.
- `full-market-scan-v6` has a strict two-phase snapshot boundary. It first fetches and freezes every quote chunk, records one provider-response `quote_observed_at` on every row in that chunk, and seals the run-level capture envelope (`started_at`, `finished_at`, duration, and exact symbol count). No K-line prefetch/request, scoring, or result persistence begins before that envelope is sealed. The later K-line phase retains bounded concurrency and one-batch preservation-cache prefetch without weakening this boundary.
- `full-market-score-v4` is deterministic and trend-led. Moving-average distance and slope use bounded continuous contributions with a neutral band, and the aggregate trend score is soft-clipped around 50 so finite rules do not saturate at 0/100. Data quality is penalty-only; it never creates strength. A bounded medium-term refinement of less than 0.05 breaks otherwise equal base scores at six-decimal precision, followed only by the stock symbol for deterministic ordering. Change, volume, turnover, and amount are not reused as hidden tie-breakers. Ranking admission now rejects malformed OHLC/liquidity fields and a reported daily return that differs materially from the value derived from price and previous close. Cache origin alone is quality-neutral, while fallback, staleness, malformed fields, K-line anomalies, conflicting same-day bars, quote/K-line mismatch, future quote timestamps, and uncertain single-price execution remain explicit penalties or eligibility failures. The canonical specification and every replay input/component are persisted with a stable SHA-256 hash. The temporal mode contract is part of that fingerprint, so otherwise identical `official` and `intraday` runs have different rule versions.
- Runs and per-stock results are persisted with `as_of`, `mode`, `quote_date`, `data_date`, sources, metrics, structured degradation provenance, status, rank, coverage, and duration. `quote_date` identifies the required market-quote session, while `data_date` is the last completed daily-K trading date. Active runs are de-duplicated, can be cancelled, and become `interrupted` rather than falsely remaining active after a restart. One repository-generated retry plan controls validation and atomic derived-run creation, and the original stays immutable. Every v6 retry resets the complete universe and performs a fresh all-quote capture plus full recomputation; it never carries successful rows across capture envelopes. Selective clean-row reuse remains a legacy pre-v6 compatibility behavior only. A retry inherits the source mode and must keep the same `quote_date`, `data_date`, and rule fingerprint; it cannot cross a session, date, or mode boundary.
- The workspace shows the selected leaderboard's scoring completion timestamp to the second. **更新 TOP100 评分** creates a linked derived run from the published source ranks, fetches fresh quotes and provider-confirmed daily K-lines only for those leaders, recalculates and reranks them, and leaves the original full-market snapshot immutable. The source run ID, refresh run ID, progress, and completion time remain auditable; derived TOP100 runs are excluded from automatic full-market scheduling, strategy-latest resolution, and full-market reliability denominators.
- `latest`, `latest-published`, and run history accept an optional `mode`; omission keeps the legacy global behavior. The browser uses one mode choice for the next requested scan and the leaderboard being browsed, while showing the globally active task separately. A history selector binds paging, filters, export, and discovery provenance to one published run. Rank movement is limited to the prior published run with the same mode, scope, and rule version.
- Each result row exposes two separate actions. **查看扫描快照** expands only persisted score components, tie-break values, dates, sources, adjustment mode, fallback/degradation flags, rule version, and run ID; it never requests data or recalculates. **打开当前个股分析** enters the live workbench with a visible warning that current data is not the historical snapshot.
- The leaderboard UI explicitly identifies `上海A股（主板）`, `科创板`, `深圳A股（主板）`, `创业板`, and `北交所`, and supports multiple markets, up to 20 industry fragments, min/max score/trend/change/turnover/amount/quality ranges, and up to three unique sort levels. The same normalized query drives paging and XLSX export, including the explicit listing-board label. A published leaderboard exports `榜单`, `评分明细`, and `导出信息` sheets, preserves six-digit codes as text, blocks formula injection, and performs no provider or scoring work.
- Runs persist the current stage, wall/work durations, calls/items, and SH/SZ/BJ progress. The UI shows elapsed time, effective throughput, a sample-gated ETA (`估算中` before enough evidence), and separates terminal evidence into **发布阻断**, **已通过门禁**, and **数据源告警**. A passed score-distribution gate stays green even when a snapshot-time gate blocks publication; legacy combined messages remain readable. Warm-cache K-line batches reuse one SQLite connection while every stock still requires a provider response; see [`docs/research/FULL_MARKET_SELECTION_PERFORMANCE_2026.json`](docs/research/FULL_MARKET_SELECTION_PERFORMANCE_2026.json) for the repeatable cold/warm evidence and provider-bottleneck limit.
- Offline evaluation starts from frozen published ranks, separates mode/scope/rule-version cohorts, deduplicates repeated same-session scans, and never mutates production weights. Evaluation v2 requires both stock observations and independent scan sessions, aggregates confidence intervals by session, reports Rank IC/deciles, T+1/cost-aware execution, buy/hold hysteresis and board/industry/liquidity exposure audits. A bad symbol or run is a structured exclusion instead of aborting the report. Every new production snapshot persists digest-verified 61-session point-in-time feature and quote/metadata evidence plus separate ordinal 1/5/20-day Alpha, confidence, risk, tradability and profile utility scores; these are not probabilities and do not change the v4 rank. Shadow v5.3 remains replay-compatible. Shadow v5.4 adds sequential shrunk market/board/quality-gated-industry/liquidity residualization, explicit capacity/tradability constraints, and a time-aligned volume policy: intraday volume contribution is neutral unless comparable intraday volume evidence exists. Promotion is manual and preregistered across coverage, 5-day Rank IC, Top100 cost-aware net excess, monotonicity, drawdown, turnover, exposure, independent sessions and PBO/multiple-testing readiness; no candidate can automatically replace `full-market-score-v4`. The retained [2026 evaluation summary](docs/research/FULL_MARKET_SELECTION_EVALUATION_2026.md), [production report](docs/research/FULL_MARKET_SELECTION_EVALUATION_2026.json), and [historical v4/v5 comparison](docs/research/FULL_MARKET_SELECTION_SHADOW_V5_2026.json) remain historical evidence snapshots rather than promotion decisions.
- A v6 run is publishable only when global and SH/SZ/BJ success coverage over eligible rows reaches 95%, at least 90% of each market remains eligible, the capture envelope is sealed with the exact universe count, every row has a parseable `quote_observed_at`, and each of three independent time gates stays within 20 minutes: run-level quote-capture duration, global observation-time span, and event-time span inside each market. The event-time difference across SH/SZ/BJ is retained as a diagnostic only because different market/provider feeds need not stamp events on the same clock edge. No systemic same-day K-line lag cluster may be present. A versioned score-distribution gate also audits distinct-score coverage, the largest tie group, boundary saturation, and top-100 ties so a constant or collapsed ranking cannot be published as healthy. Deterministically ineligible rows such as suspended, single-price, or too-short-history stocks remain visible but do not dilute the eligible coverage denominator; the separate 90% guard prevents a systemic data problem from being hidden as mass skipping. A 30-minute whole-run wall-clock budget and repeated completed-trading-date checks stop stale work without permitting partial-snapshot reuse.
- Automatic after-close scans create `official` runs only. They run a bounded SH/SZ/BJ stock-pool, quote, and five-row completed-daily-K preflight before creating a formal batch. Retryable scheduled failures create a fresh linked v6 full-universe recomputation after configured 10/30/60-minute delays, stop after the configured limit or trading-date change, and never restart a user-cancelled, manual, intraday, or merely degraded batch.
- Batch pressure control starts at the configured concurrency, halves it on provider busy/timeout/retry-after or systemic unavailable signals, and restores one slot per healthy batch. All waits still consume the scan-wide provider budget; ordinary per-symbol coverage misses do not slow the whole market.
- Full-market scoring is local and deterministic. It never calls an LLM per stock; LLM use remains an optional, on-demand explanation path for a selected stock.

The scan workspace validates API response contracts, uses a single bounded exponential-backoff poller, tracks the active task separately from the selected-mode published result, and keeps the last publishable leaderboard visible while a new run is running or fails. It resets pagination only when the displayed result run changes, cancels obsolete discovery requests, and resumes immediately when the browser returns online. Static assets are revalidated with `no-cache`, and the scan ES modules share one version mapping.

On a trading day, `intraday` is available from 09:30 inclusive until 15:15 exclusive. It requires quotes from the current trading day, uses completed daily K-lines only through the previous trading day, and validates each quote's previous close against that final K-line close. Its output is a provisional intraday leaderboard and may change with the live market. `official` is available from 15:15 onward, when `quote_date` and `data_date` are the same completed trading day and the quote close is checked against the same-day K-line close. On a non-trading day, an `official` run resolves both dates to the latest completed trading day. Optional after-close scheduling, concurrency, timeouts, retention, degraded behavior, and troubleshooting are documented in the [Operations Guide](docs/OPERATIONS.md).

### Strategy Decision Laboratory

The expandable strategy laboratory inside **全市场选股** turns a frozen published scan into an evidence-first research workflow without changing `full-market-score-v4`.

- A strict, immutable `StrategySpec v1` carries the Shanghai main board, STAR, Shenzhen main board, ChiNext, and Beijing board universe; exclusions and typed hard filters; independent Alpha 1/5/20-day, confidence, risk, and tradability objectives; portfolio, rebalance, execution, and evidence policies; a revision; and a stable semantic SHA-256 fingerprint. Saved strategy roots and all immutable revisions are included in local user-data portability.
- The Chinese entrypoint produces a structured draft, explicit defaults, ambiguities, unsupported clauses, and a deterministic dry-run plan. It never executes generated SQL or user code, does not require an LLM, and requires an explicit confirmation before save or execution.
- Executions bind the exact strategy revision/fingerprint, published market-scan run, rule version, `data_as_of`, data date, cost fingerprint, and result digest. Optional selectors such as `revision`, `run_id`, or `data_date` are resolved before the execution fingerprint is built, so two requests that identify the same immutable semantics replay identically. Latest-scan and exact historical replay keep the original production rank visible, paginate candidates, expose independent objectives and Pareto membership, and can legitimately return `no_trade` when evidence or constraints fail.
- Portfolio drafts execute—not merely serialize—equal, inverse-risk-constrained, and explicit custom weights. They apply buy/hold hysteresis, evidence-source completeness/allowlists, A-share lots, effective-dated board price limits, suspension/zero-volume checks, T+1, costs, slippage, capacity, and stock/industry/board limits. Counterfactuals and one-weight-at-a-time sensitivity use persisted deterministic fields only. Simulation output is an idempotent, readable paper plan and never reaches a broker.
- The evidence center compacts the retained offline evaluation report rather than running a multi-minute cross-date evaluation in an HTTP request. It distinguishes a global production/Shadow baseline from custom-strategy execution evidence, displays report generation time and digest, and keeps insufficient sample, point-in-time-integrity, and PBO blockers visible. Generate a newer baseline explicitly with `tools/evaluate_market_scan_shadow.py`; there is no automatic promotion control.
- Version-pinned schedules run at most once for a published scan, isolate failed attempts, and emit fingerprinted new-entry, removal, utility-threshold, stale-data, or invalid-evidence events. Archiving disables an active schedule at its next claim boundary, and an archived strategy cannot start a new latest-scan execution or be re-enabled. They create research executions and alerts only—never orders.

The browser editor exposes named and custom objective profiles, all three weighting methods, custom symbol weights, portfolio exposure/capacity limits, and buy/hold thresholds. It keeps invalid compile state and save availability consistent, clears a stale execution when the selected strategy or revision changes, renders simulation/schedule lineage, and compares immutable `StrategySpec` revisions separately from execution-result comparisons.

Future event timelines, crowding/fundamental divergence, evidence debate, personalized review, strategy sharing, AutoML, and broker integrations are extension boundaries only. They are not enabled and cannot bypass deterministic compilation, point-in-time evidence, or manual review.

## Paper-Trading Review

Open **复盘工具 → 模拟交易** to validate frozen review plans against completed historical daily bars. This is a deterministic paper-trading model, not a broker connection: it never submits a real order and cannot reproduce intraday queue position, liquidity depth, or every exchange exception.

- A strategy becomes eligible only after its activation date and enters at the next tradeable daily open. Priority, allocation, and a 1–60 session entry expiry are explicit; a target or stop reached while waiting invalidates the late entry.
- Stock T+1 is enforced. An entry-day target/stop signal is latched and exits at the next sellable session open; suspension, zero volume, locked limit-up buys, and locked limit-down sells remain unfilled and appear in the event ledger.
- Effective-dated main-board, STAR, ChiNext, and Beijing-board quantity/price-limit profiles replace a universal 100-share assumption. Historical ST state is applied only from its own effective date; missing or future-dated ST/listing metadata is surfaced as degraded rule quality rather than silently treated as certain.
- Versioned base, conservative, and stress cost profiles split commission, minimum commission, sell-side stamp duty, transfer fee, and buy/sell slippage. Net/gross return, cost drag, benchmark/excess return, drawdown duration, win/loss quality, turnover, exposure, and sample-gated risk metrics are reported separately. Benchmark return starts at the first simulated session open so it shares the portfolio's investment clock.
- Only completed canonical trading-day bars at or before `as_of` can affect price rebasing, execution, benchmark returns, fingerprints, or provenance. Conflicting same-day bars inside that visible window are rejected; later rows cannot invalidate an earlier replay.
- Every simulation appends an immutable run with strategy, market-data, configuration, rule, cost, source, and SHA-256 fingerprint provenance. Historical runs can be reopened, compared, exported as JSON or formula-safe UTF-8 CSV, and included in local user-data backup/import.

## Discovery and Research Loop

- Saved discovery presets filter only fields already persisted by a completed scan: multiple markets/industries, ST/new-stock state, all six min/max ranges, and up to three sort levels. The UI can create, fully edit, apply, rename, delete, import, and export these definitions with optimistic revision checks. Applying a preset never fetches another data source.
- One row, the current page, or all pages of the current filtered result can enter the research queue. Requests are chunked to the 100-symbol API cap and retain immutable source run/preset/revision provenance. Adjacent published runs are comparable only when mode, scope, and rule version all match.
- Historical watchlist-condition scans render read-only evidence with the requested `as_of`, rule version, condition results, and stored metrics. Opening the current stock workbench is a separate explicit action, so present data is never presented as a historical snapshot.
- After the daily-bar publication boundary, bounded scheduler tasks refresh advice for active, non-excluded watchlist rows without an LLM and evaluate due review plans. Each stock is isolated; stale dates, weak quality, invalid contracts, and unchanged conclusions do not create misleading snapshots. Mark-viewed uses the displayed advice ID as a watermark, preserving changes produced later.
- Review plans merge snapshot-derived structured evidence with user notes. Evidence records carry an ID, value, direction, data date, nature, and rule version. Evaluation records executable trigger/invalidation bases, target/stop outcomes, return, MFE, and MAE, while `GET /api/reviews/summary` exposes a global local-only aggregate.

Offline ranking research is available without touching production scores:

```bash
python tools/evaluate_market_scan.py --database data/ashare_radar.sqlite3 --output evaluation.json
python tools/evaluate_market_scan_shadow.py --database data/ashare_radar.sqlite3 --output shadow-comparison.json
python tools/evaluate_market_scan_probability.py --database data/ashare_radar.sqlite3 --output-dir data/market-scan-probability
python tools/evaluate_market_scan_future_range.py --database data/ashare_radar.sqlite3 --output-dir data/research/market_scan_future_range
python tools/benchmark_market_scan.py --database data/ashare_radar.sqlite3 --output performance.json
```

The evaluator reads published frozen ranks and only later persisted complete trading days. It reports Top 20/50/100 gross/net returns, equal-weight excess return, session-block bootstrap intervals, session maximum drawdown, adverse excursion, Rank IC/ICIR, deciles, monotonicity, stability, turnover, execution status, and board/market/segment/liquidity/scan-time/quality strata. Cohorts below either the stock floor or the independent-session floor are `insufficient_data`; reports never rewrite score weights or a historical `rule_version`.

The probability command is also SQLite-read-only, but persists one immutable, content-addressed JSON artifact per run while retaining every run/symbol/target/horizon result, including `probability=null` when evidence is insufficient. It studies 1/5/20-session Shadow probabilities, with 5-session cost-adjusted equal-weight market outperformance as the primary target. Labels enter at the first tradeable session open after the scan, treat that entry session as holding day 0, and exit at the close of the H-th later tradeable session; locked limits, suspension, capacity, incomplete horizons, A-share T+1, and commission/tax/transfer/slippage are explicit. Registered point-in-time features cover production-score decomposition, raw momentum/ATR/downside-volatility/drawdown/liquidity inputs, market/board/industry context, status and direction-specific price-limit context; post-outcome feature prefixes are rejected. Grouped-date walk-forward evaluates every complete, non-overlapping OOS test fold with independent train → gap H → calibration → gap H → test fitting; a trailing partial test window is never promoted to a fold. Metrics aggregate all OOS folds and Brier Skill uses each fold's calibration-period base rate, while only the final complete fold's model and Platt calibrator serve later Shadow forecasts. Every fold, prediction `fold_id`, component digest and aggregate metric is replay-checked. Coverage/session/bin gates, an empirical-Bayes baseline, a sample-gated comparison-only Isotonic candidate, calibration/outcome/stability slices, full-input deterministic replay, and date-block intervals fail closed to `null`. The API lazily loads only the requested run's schema- and digest-verified artifact; its SHA-256 integrity seal detects payload changes but is not a digital signature or authenticity proof. The full-market UI defaults to the 5-session net-excess target, switches among 1/5/20 sessions, and renders probability, 95% interval, base rate and frozen evidence only for the selected batch. Old or insufficient batches say evidence is insufficient without substituting 0% or 50%. Only `calibrated_shadow` evidence enables `probability_horizon` plus `min_upside_probability` filtering; filtering narrows rows while preserving persisted production ranks. These values remain Shadow, never change `full-market-score-v4`, and cannot enter production sorting automatically.

Probability fitting is additionally isolated by the exact `(mode, scope, rule_version)` contract: dates, labels, equal-weight benchmark, train/calibration/test folds, models and review gates are never borrowed across cohorts. For one cohort and quote date, the read model keeps only the final published run by `(as_of, id)`; the research layer rejects any conflicting duplicate that survives that boundary. When several cohorts are present, top-level horizon data is an index of cohort summaries only: it has no pooled probability, base rate, benchmark, model or promotion decision, and human review remains attached to one named cohort. Label contract v2 fails closed when the effective-dated trading-rule profile is unavailable or degraded, so that outcome cannot enter the benchmark or model. Ordinary daily-bar execution ambiguity remains separately persisted as `daily_bar_model_limited`; it may stay in the preregistered open/close model when the rule profile itself is verified and is never presented as exact intraday-fill evidence. The CLI rebuilds every target/horizon independently from the complete artifact set for each cohort; only a successful artifact-set replay sets `full_input_replay_verified=true`. Stdout and optional `--report` output use the machine-readable evaluation-summary contract.

The future-range command is a separate, official-only diagnostic over canonical published snapshots. It binds the signal day to the run's digest-verified 61-session point-in-time evidence, maps D+1/D+2/D+3 to fixed trusted exchange sessions, and never shifts a suspended or missing stock to a later convenient date. For each offset it compares low, high, close, and HLC3 against the same D field, D close, and the fixed D+1 open. HLC3 is always labelled a typical-price proxy, never VWAP. High/low, MFE, MAE, and the D+1-open path remain exploratory because daily OHLC cannot recover intraday ordering. The separate executable projection enforces A-share T+1 and the registered cost/rule model: D+1 has no same-session executable exit, D+2 corresponds to the existing probability label's H=1 exit, and D+3 corresponds to H=2. Modelled rows retain gross return, cost drag, net return, same-run equal-weight executable-market return, and net excess; because future daily bars lack amount, capacity remains an explicit proxy based on frozen signal-day amount. Missing, immature, suspended, or version-conflicting row outcomes remain `null` with an explicit reason; an underpowered aggregate may retain clearly marked descriptive mean/median/positive-rate values, but inference intervals and pass/effectiveness conclusions stay `null` and it must not be read as validation.

Artifacts are immutable, content-addressed JSON outside SQLite and retain Top20/50/100/all plus decile summaries, mean/median/positive rate, trend Rank IC, monotonicity, and date-based uncertainty with a three-session moving-block length. The contract also fixes `validation_gap_sessions=3` as the minimum for any future train/test split; it does not thin the current descriptive cohort aggregation. Existing 1/5/20-session probability semantics and production ranking are unchanged; only supplied, persisted OOS `calibrated_shadow` evidence may appear as an explanatory comparison. `GET /api/market-scans/{run_id}/future-range-research` is read-only and returns `generation_status`, artifact identity, optional aggregate `research`, and a pageable `record_page`; `include_research=false` supports light follow-up pages. The lazy **未来区间验证** panel provides D+1/D+2/D+3, group, symbol, and page controls without replacing the leaderboard. XLSX export adds a formula-safe **未来区间验证** sheet and leaves unavailable numeric cells blank rather than inventing zeroes.

## Current Architecture

```text
Browser UI
  -> FastAPI routes
  -> workflows
  -> services and provider adapters
  -> repositories and SQLite
```

Key runtime areas:

- `app/`: lifecycle, API, workflows, providers, analysis, models, and local persistence.
- `static/`: browser orchestration, rendering, charts, styles, and interactions.
- `data/ashare_radar.sqlite3`: local runtime cache and user data.

See [Software Design Description](docs/DESIGN.md) and [Maintenance and Refactor Guide](docs/MAINTENANCE.md) for the detailed module map.

## Runtime Boundaries

- The app is local, single-user software. Browser writes and explicit refreshes enforce the configured same-origin boundary; ordinary reads and metadata-free non-browser clients remain supported.
- Time has three explicit meanings: market events and calendar rules use aware `Asia/Shanghai`; audit/persistence timestamps use fixed-width UTC ISO 8601 text ending in `Z`; TTLs, deadlines, and elapsed durations use monotonic/performance clocks. The one-time SQLite migration interprets legacy naive audit text as Shanghai local time by default and converts it to UTC without changing market-event fields; set `ASHARE_RADAR_LEGACY_AUDIT_TIMEZONE` before first startup when an old database came from another host timezone. This is an offline upgrade: create a verified runtime backup, stop every old process, and follow the disk-space and validation procedure in `docs/OPERATIONS.md` before starting the new version.
- `GET /api/health/live` is a process-only liveness probe. `GET /api/health/ready` is an admission probe: it requires completed application startup and a bounded read-only SQLite check, but does not make provider-network availability a readiness dependency. Both probes use `Cache-Control: no-store`; a standby runtime can be ready and reports its role explicitly.
- `GET /api/system/reliability` summarizes low-cardinality hourly observations. Workbench and provider indicators use a seven-day window whose start is aligned to the containing UTC hour; ordinary tasks use an exact seven-day window, while full-market success, coverage, and duration use 30 days. Every objective reports its target, sample floor, ratio or percentile, and `insufficient_data` instead of treating a small sample as success.
- SQLite is the local persistence layer. One database-adjacent `runtime-leader` lock owns the scheduler and full-market scanner as a unit; bounded stop may return before a non-cooperative task, but leadership is released only after both services are truly idle. A standby then takes over both together. The supported Uvicorn topology remains one worker because status and controls are process-local.
- Daily research uses an explicit `qfq` K-line contract with adjustment, as-of, data-version, and contract-version provenance. Cache keys isolate other adjustment modes and legacy `unknown` rows.
- Provider failures and cache fallback stay visible, and provider errors are sanitized before persistence or response rendering.
- Blocking provider SDK work runs in a bounded runtime-owned daemon executor. Shutdown rejects new calls, cancels queued work, and waits only for its configured budget; an already-running uncooperative SDK call cannot be force-stopped, but its daemon worker does not keep Python alive at process exit.
- Code/name autocomplete calls the existing stock-search endpoint only for a debounced, uncached, non-complete query. Chart inspection and local research-activity filtering use data already loaded in the browser and issue no requests.
- LLM wording is optional and explanatory. Failure falls back to deterministic rule text, and local watchlists, notes, alerts, advice history, and provider credentials are excluded from its prompt.
- User-owned records can be exported and transactionally imported as versioned JSON. Import commits require a matching server preview and verified rollback backup; backup creation, verification, rotation, and restore share a bounded cross-process operation lease so an in-use bundle cannot be pruned. Scheduled cleanup removes only regenerable/runtime rows through throttled set-based retention. Active scans and the direct parent of each retained retry are safety exceptions; older retry ancestry can expire in the same pass. Large successful cleanups compact SQLite only when reclaimed pages are material, and compaction failure never rolls back logical retention. Deleting an advice-review plan is irreversible and also deletes its evaluation history, while the source advice snapshot remains intact. Full backup/restore and manual retention cleanup are documented in the [Operations Guide](docs/OPERATIONS.md).
- Browser notifications can be enabled or disabled explicitly, and that preference survives page reloads. Alert-event pagination uses the event ID as its authoritative cursor. Enabling or re-enabling establishes a fresh baseline, so historical events and events created while notifications were disabled are not replayed; a failed delivery remains behind the cursor for ordered retry.
- Diagnostics apply freshness checks to quote and K-line data as well as stock-pool and plate metadata, and report SQLite/managed-backup bytes plus quote, K-line, and full-market-scan row groups.
- Files under `data/`, including runtime-leadership and compatibility lock files, are local runtime state and must not be committed; `data/.gitkeep` only preserves the directory.

## Engineering Verification

Required CI is hermetic and does not call live market providers. The optional canary uses an isolated temporary SQLite database and probes representative SH/SZ/BJ symbols with a non-cached quote request, a five-row completed daily-K request, and a three-market stock-pool request. Market probes and the larger stock-pool refresh use their corresponding independent DataHub timeout settings under one overall deadline:

```bash
$PYTHON tools/provider_canary.py
```

Exit `0` means all three markets and the stock-pool contract are available, `2` means partial availability, and `1` means no market is available or provider cleanup failed. The command may use network/provider credentials and is intentionally outside required pull-request CI.

The separate Security workflow audits both hashed Python runtime/development locks and the npm graph, scans both the current tree and complete Git history for secrets with redacted output, generates Python/npm CycloneDX SBOMs twice and compares them byte-for-byte, and uploads the normalized artifacts. Dependabot covers pip, npm, and GitHub Actions; every third-party workflow action is pinned to a full commit SHA and checkout persistence is disabled. These controls improve traceability but do not claim artifact signing or a SLSA level.

## License

MIT. See [LICENSE](LICENSE).
