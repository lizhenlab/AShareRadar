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
- [Maintainability Audit and Incremental Refactor Plan (verified 2026-08-12)](docs/research/ENGINEERING_MAINTAINABILITY_AUDIT_2026_08_12.md)
- [Individual D+2/D+3/D+4 Probability Frontier and Optimization Plan (reviewed 2026-08-13)](docs/research/INDIVIDUAL_SHORT_HORIZON_PROBABILITY_2026.md)
- [Full-Market Strategy Archetypes and Optimization Plan (2026-08-12)](docs/research/FULL_MARKET_STRATEGY_ARCHETYPES_2026.md)
- [Review-Tool Trust Audit and Remediation Record (2026-08-13)](docs/research/REVIEW_TOOL_TRUST_AUDIT_2026_08_13.md)
- Research: [2026 Core Feature Study](docs/research/COMPETITOR_CORE_FEATURES_2026.md), [Current Capability Audit](docs/research/CURRENT_CAPABILITY_AUDIT.md), and [Product Gap and Roadmap](docs/research/PRODUCT_GAP_AND_ROADMAP.md)

Competitor, capability, gap, and roadmap research dated July 15-16, 2026 is retained as historical decision context. It is not a current implementation contract; use the design, operations, test plan, and dated engineering-quality or maintainability audits for the current worktree.

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

## Individual Workbench Trust Boundary

`GET /api/stock/workbench?symbol=...` now returns strict `stock-workbench-v2`
evidence. The response is an `interactive_shadow` research cohort with
`production_effect=none`, `diagnosis_production_effect=none`, and
`advice_persistence=disabled`. The backend and browser both bind the requested
A-share identity to the quote, every owned research/local-state panel and every
strategy card, quote event time, signal date, completed daily-bar cutoff, and
context decision time. Every research child also carries an `updated_at` no later
than that decision time and on the same Shanghai signal date.
A cross-symbol, cross-session, stale-cache, or malformed cohort is rejected
instead of being rendered. The short-lived server cache is additionally keyed by
the current market phase plus expected quote/daily-bar dates, so an 8-second TTL
cannot cross a trading-session boundary; independent child routes enforce the
same owner/time contract and cannot bypass the aggregate workbench.

Concept membership in this interactive response is cache-only: an ordinary
workbench read never starts an online concept-board membership scan. Only fresh,
non-static provider-cache rows may appear as optional research background. A
missing, stale, static-only, or unreadable cache is reported as unavailable and
is excluded from theme and leadership scoring. This cache has no receipt or
integrity digest, is not authorization evidence, and cannot enable a scan,
probability, filter, or action.

Opening or refreshing the interactive workbench never appends a formal advice
snapshot. Formal advice persistence is owned only by the after-close active
research queue at or after 15:15, after it receives an official quote event in
the closed-session window 14:55:00 through 15:00:00, at least 60 valid completed
daily bars, and passing trading-date, quality, fallback and contract evidence.
The on-page diagnosis is explicitly a Shadow research view,
not a second persisted advice authority. Pre-open and intraday analysis exclude
unpublished daily bars and cannot use live partial-session volume as completed
daily-volume confirmation.

Missing evidence is typed rather than converted into a bullish neutral default.
Unavailable volume, structure/chip, valuation, order-pressure, fund-flow, risk or
calibration evidence cannot contribute a directional/current aggregate score or
leave a stale number in the DOM. Missing structural/ATR inputs keep target, stop
and reward/risk ratio unavailable. Historical factor calibration participates
only when every fixed exchange session has unique continuous PIT `qfq` evidence
with supported data, suspension, open-execution and corporate-action metadata.
The sensitive `/api/analyze`, `/api/stock/workbench`, and
`/api/stock/upside-probability` surfaces apply `Cache-Control: no-store` to every
response path; dependency, transport and response-validation 5xx failures expose
only the generic individual-research unavailable message.

## Individual Short-Horizon Probability Research

The individual-stock workspace exposes an independent read-only D+2/D+3/D+4
upside-probability research panel. Its fixed contract observes only a completed
signal session D, uses the fixed D+1 official daily-K `open` as a no-shift price
proxy, and evaluates whether the daily-bar proxy net return after declared costs
is positive at the fixed D+2, D+3, or D+4 `close`, corresponding to one, two, or
three holding sessions. The open proxy does not prove or guarantee an executable
fill. Each horizon has its own label, model, calibration, out-of-sample evidence,
confidence interval, and gate reasons; no horizon is derived from a trend score
or another horizon.

`GET /api/stock/upside-probability?symbol=...` returns a typed
`IndividualUpsideProbabilityReport`. A non-null percentage is allowed only when
the report and that horizon are both `calibrated_shadow`, report-level
`evidence.selection_qualified=true`, and the point estimate plus ordered 95%
interval are present. The artifact verifies each horizon's gates independently
before mapping the child `status`; the public horizon object does not expose a
separate `selection_qualified` field. Otherwise the value and interval remain
`null`, and the UI shows the evidence status rather than substituting 0%, 50%, a
trend score, or a stale value from another stock. `not_generated` and
`insufficient_data` are research states, not bearish forecasts.

The current builder schema is
`individual-upside-probability-assessment-v4-source-intake-bound`; v3 is
superseded and rejected. V4 retains the exact estimator, target and split
bindings and admits a named official input only after fully loading current
source-artifact-v3 / snapshot-v3 records with estimator feature-v3 and
PIT-evidence-v4 under an original `publication` seal. Source intake requires a
trusted trading date and the aware sequence `quote event <= quote_observed_at <=
run.as_of (decision) <= captured_at <= assessment.generated_at`, validates every
bar's date/aware-`as_of` boundary and nondecreasing observation order. The quote
and bars stay on the source session. For current source v3, `run.as_of` may be on
a later weekend or exchange holiday only when the official temporal contract
still resolves that source date as the latest completed session; capture may be
later but never before the decision. A pre-15:15 decision, a stale source after
another session matures, a future/reversed timestamp, or unavailable calendar
coverage fails closed. Frozen source v1/v2 readers retain their original
same-date rule. It binds the current writable v5 production-score rule/spec hash, replays every row's
score details against persisted score inputs/components, and freezes the complete
ALL/SH/SZ/BJ envelope. The population floors are 4,000/1,800/2,500/200; every
scope requires at least 90% eligibility and 95% success coverage, all three
markets must be present, and the assessment intake additionally requires 98%
success-to-total coverage with complete records. Cross-date run IDs and source
contracts cannot conflict. Assessment v1 and v3 are rejected; assessment v2
remains audit-readable under its exact legacy shape, but its run/date/digest-only
sources are not current evidence.

V4 binds that complete intake identity through build and compact verification,
but the compact file still does not retain source locators/records for a later
runtime replay. Its SHA-256 therefore proves content consistency, not source
authenticity. The public runtime projection consequently forces
`official_pit_session_count=0`, `signal_date=null`, every horizon to
`insufficient_data`, and all percentages/intervals to null; a non-empty current
source declaration adds `official_pit_source_artifacts_not_runtime_replayed`.
Until locator-bound source replay exists, compact metadata cannot establish an
independently attested PIT corpus.

The tracked compact baseline is that legacy v2 artifact. It retains run 71
(`150cc48d…`) and run 77 (`c085b6fa…`) for audit, but the current projection is
therefore **0/288**, with `signal_date=null` and D+2/D+3/D+4 probabilities and
intervals all null. Its retained digest is
`517691b101dcb2142693a74f6e5ac9ef10f386c545572b6bacfe161f186ba677`
and the canonical JSON is 8,733 bytes. The report explicitly carries
`legacy_official_pit_sources_audit_only_not_current_evidence` and
`compact_horizon_metrics_not_independently_replayable`. The separately attested
history replay is non-official and failed its actual selection gates, so it also
cannot populate the current-stock projection. A future public calibrated result
requires 288 official sessions overall; H1/H2/H3 independently require
284/286/288 sessions, at least two complete folds and 60 OOS sessions per fold,
at least two calibration bins with 20 sessions in the sparsest bin, positive
Brier skill in every fold, deterministic Brier/reference-Brier identity, exact
versions/digests, a trusted exchange signal date and an earlier trusted training
cutoff. This research surface has
`production_effect=none`: it does not change existing stock analysis, advice,
full-market ranking, screening, automation, or the separate 1/5/20-session
full-market probability contract. See the
[dated research and implementation boundary](docs/research/INDIVIDUAL_SHORT_HORIZON_PROBABILITY_2026.md).

## Full-Market Strategy Template Catalog

The full-market Strategy Lab exposes a strict, read-only catalog at
`GET /api/strategy-lab/templates`. The catalog is fixed to the complete
SH/SZ/BJ A-share research surface; it has no request-selectable stock or custom
scope and does not belong to the individual-stock workbench. Its schema is
`full-market-strategy-template-catalog-v1`, selection is `exclusive`, production
baseline identity is the frozen historical `full-market-score-v4`, and
`production_effect=none`. That dated field does not describe or override the
current writable `full-market-score-v5` contract. The response is
`Cache-Control: no-store` and binds its semantics with catalog and per-template
SHA-256 digests. The frozen 2026-08-12 catalog contains 14 version-1 templates
and has catalog digest
`0038e66d4ce6c13bafb51e3fddf990c11f5f8f38c001343f5f4523ff255a9d1f`.

Six templates are `available_for_draft`: `balanced_multi_horizon`,
`bounded_medium_trend`, `capacity_first`, `daily_continuation`,
`defensive_liquidity`, and `pullback_continuation`. Each provides a strict custom
`StrategySpec` with a 61-session formation contract and can be dry-run compiled;
its `contract_status=verified` and `efficacy_status=not_generated`. Each exposes
the compiler execution plan's complete 16–18 `required_fields`, including the
default exclusions plus board, industry, price, status, amount, 61-bar evidence,
score dimensions and any return input used by that draft. The catalog models
recompute both digest layers and require globally unique template IDs; a
well-shaped but semantically changed payload is rejected. Every loadable spec
uses `profile=custom`, so a named profile cannot overwrite its frozen objective
weights. Three routes
freeze holding/rebalance at 5/5 sessions (`balanced_multi_horizon`,
`capacity_first`, `pullback_continuation`), `daily_continuation` uses 1/1, and
`bounded_medium_trend` plus `defensive_liquidity` use 10/10 so the current editor,
which has no independent rebalance control, cannot drift the loaded semantics.
Three 61-session routes are `shadow_only` with `strategy_spec=null` and
`efficacy_status=insufficient_data`: `industry_relative_strength`,
`medium_momentum`, and `short_reversal`. Five are `unavailable` with a null spec,
explicit `missing_fields`, and unavailable contract/efficacy states:
`crowding_risk`, `dividend_low_vol`, `event_revision`, `quality_growth`, and
`value_garp`. Existing amount/tradability fields do not make crowding available;
the PIT common-holdings, fund-flow, crowding, and capacity fields are still
missing. Every entry also discloses that suspension and one-price-limit states
currently use frozen daily-K amount and reason-text proxies, not exchange
event-time or order-queue evidence.

Opening the full-market workspace activates the Strategy Lab and lazily reads
the catalog. The UI allows one template selection at a time. Only
`available_for_draft` entries can load the editor, and loading first performs a
dry-run compile; Shadow and unavailable entries remain visible with their gate
reasons but cannot be loaded. Loading a template does not save a strategy, start
a scan, alter the production leaderboard, promote evidence, or place an order.
The catalog's `official_session_count=2` refers only to official PIT run 71 on
2026-08-11 and run 77 on 2026-08-12. Two sessions can verify contracts and
deterministic projections, but cannot establish efficacy, calibration, regime
fitness, multiple-testing results, or production readiness. See the
[dated strategy-archetype research report](docs/research/FULL_MARKET_STRATEGY_ARCHETYPES_2026.md).

## Full-Market Ranking

The browser UI has four top-level work areas: **个股研究**, **全市场选股**, **复盘工具**, and **自选监控**. Each area exposes only its relevant controls; stock research keeps its focused secondary tabs, while the full-market leaderboard uses the entire main workspace instead of appearing below the stock chart.

Open **全市场选股** and click **开始扫描**. `POST /api/market-scans` accepts `mode=official|intraday|preopen` and defaults to `official` when `mode` is omitted. The API immediately creates a background run; the page then polls real progress and publishes a stable, pageable snapshot only after that run reaches a terminal state.

- The exact full-market scope is `沪市 + 深市 + 北交所当前上市A股`: the current listed A-share pool across SH, SZ, and BJ. It has no fixed 5,000-row truncation, rejects Shanghai/Shenzhen B shares, excludes delisted rows, and retains explicit ST/new-stock tags. Provider rows are normalized and de-duplicated once; coverage checks and the atomic cached-pool replacement use that same candidate set. Guards enforce both a 4,000-name total and configurable minimums for each market, require at least 98% retention against the latest authoritative snapshot, and reject a large per-market drop before per-stock work begins. Industry and listing-date completeness are reported separately instead of silently turning unknown metadata into a confident classification.
- Unrankable, stale, suspended, malformed, low-quality, or otherwise incomplete rows remain `missing`; they never enter the ranking as zero-score stocks. A current official v6 run may use `skipped` only for two replayable PIT exclusions: a trusted 61-bar window with an exchange-session gap, or a genuinely new listing whose complete trusted listing-to-D session history is still shorter than 61. Both require fresh non-fallback quote/bar evidence, canonical reason-specific facts, and a bound evidence digest; every other exclusion is `missing`. A system-wide quote or daily-K provider-chain outage is different: the affected rows stay `pending`, the batch receives bounded retries within one cumulative provider-wait budget, and an unrecovered run fails in a retryable state instead of bulk-writing false `missing` results.
- The two current official skip reason codes are exactly `official_session_gap` and `new_listing_insufficient_history`; both persist `market-scan-skip-evidence-v1` plus replayable `market-scan-skip-pit-v1`. No third reason may authorize `skipped`. Only inside this skip-PIT parity check, a K-line snapshot `as_of` that is exactly canonical `YYYY-MM-DD` is normalized to `Asia/Shanghai` 00:00; a future date, non-canonical date text, or timestamp that violates bar/decision ordering fails closed. This compatibility rule does not relax any other timestamp contract.
- Production run `#86` remains immutable failed/unpublished negative evidence: its persisted terminal counts are 5,389 success / 154 missing / 0 skipped. Its skip-PIT parity rejected the provider's canonical date-only K-line `as_of`, so otherwise replayable exclusions fell back to `missing` and the run could not publish. A read-only replay over the frozen inputs identifies 146 typed exclusions—38 `new_listing_insufficient_history` plus 108 `official_session_gap`, bound to 7,673 bars—and therefore the expected conservation for a newly executed corrected run is 5,389 success / 8 missing / 146 skipped. These replay counts neither repair nor reseal `#86`; only a new scan can publish them.
- Each symbol refresh asks DataHub for up to 260 completed `qfq` daily bars and requires a real provider response. A compatible cache is only an incremental-refresh base or an explicitly marked fallback; it cannot satisfy that freshness requirement by itself. The default chain is Tencent's current SH/SZ/BJ `newfqkline` interface, AKShare (with its internal Eastmoney-direct and final Sina-forward-adjusted fallbacks), optional token-backed Tushare, and BaoStock. Unavailable optional providers are skipped by capability. Overlap verification detects adjustment rebases and triggers a full refresh. Cache persistence retains quote/K-line fallback provenance, and older K-line vintages cannot replace newer equal-length snapshots.
- Industry plate ranking does not enter AKShare SDK pagination. The adapter makes one bounded HTTPS Eastmoney request for at most 100 rows, disables redirects, requires exact HTTP `200`, caps the response at 512 KiB and validates the advertised total, complete row set, unique `BK` codes/names and descending order before the old cache may be replaced. A timeout, redirect, oversized or paginated response, or malformed row is a provider failure; analysis may keep using the previous local plate cache as optional context.
- `full-market-scan-v6` has a strict two-phase snapshot boundary. It first fetches and freezes every quote chunk, records one provider-response `quote_observed_at` on every row in that chunk, and seals the run-level capture envelope (`started_at`, `finished_at`, duration, and exact symbol count). No K-line prefetch/request, scoring, or result persistence begins before that envelope is sealed. The later K-line phase retains bounded concurrency and one-batch preservation-cache prefetch without weakening this boundary.
- New `full-market-scan-v6` runs write deterministic, trend-led, ordinal `full-market-score-v5`; historical `full-market-score-v4` rows remain exact read/replay compatibility evidence and are never rewritten or silently upgraded. The single history minimum is 61 complete daily bars. Shared 1/5/20/60-day and skip-return windows remain mode-aware. Data quality is penalty-only. V5 promotes the existing bounded medium-term PIT composite from a sub-tick v4 discount into a material continuous-trend component: `continuous_trend_adjustment = (2 * continuous_trend_score - 1) * 4`, so `base = clamp(leader_score - quality_penalty + adjustment, 0, 100)` and `raw_score = base`; the integer score is only the rounded display value. Change, volume, turnover, amount and quality are not hidden tie-breakers. Distribution policy `score-layer-distribution-v4` still requires complete base/integer/final and component evidence and rejects collapsed or decimal-jitter pseudo-diversity. Current point-in-time evidence stays `market-scan-point-in-time-feature-evidence-v4-bar-as-of-bound`, with every real bar and its `as_of`, quote event/observation/decision ordering, and a sealed `market-scan-session-coverage-v1` grid; no missing exchange session is synthesized. Each new run registers its exact full rule JSON plus production score rule/hash in immutable `market_scan_rule_contract`; start, retry, TOP100 refresh, result writes, publication and later action reads must match that registry. Historical v4 snapshots remain read-only even when no retroactive registry row exists.
- Runs and per-stock results are persisted with `as_of`, `mode`, `quote_date`, `data_date`, sources, metrics, structured degradation provenance, status, rank, coverage, and duration. `quote_date` identifies the required market-quote session, while `data_date` is the last completed daily-K trading date. Active runs are de-duplicated, can be cancelled, and become `interrupted` rather than falsely remaining active after a restart. One repository-generated retry plan controls validation and atomic derived-run creation, and the original stays immutable. Every v6 retry resets the complete universe and performs a fresh all-quote capture plus full recomputation; it never carries successful rows across capture envelopes. Selective clean-row reuse remains a legacy pre-v6 compatibility behavior only. A retry inherits the source mode and must keep the same `quote_date`, `data_date`, and rule fingerprint; it cannot cross a session, date, or mode boundary.
- The workspace shows the selected leaderboard's scoring completion timestamp to the second. **更新 TOP100 评分** creates a linked derived run only from source ranks that pass the complete shared action gate—original publication, exact full scope and consistent diagnostics-v1 score evidence—fetches fresh quotes and provider-confirmed daily K-lines only for those leaders, recalculates and reranks them, and leaves the original full-market snapshot immutable. The source run ID, refresh run ID, progress, and completion time remain auditable; derived TOP100 runs are excluded from automatic full-market scheduling, strategy-latest resolution, and full-market reliability denominators.
- `latest`, `latest-published`, and run history accept an optional `mode`; omission keeps the legacy global behavior. The browser uses one of the three independent modes for the next requested scan and the leaderboard being browsed, while showing the globally active task separately. A history selector binds paging, filters, export, and discovery provenance to one published run. Action-capable rank movement requires both current and prior runs to pass the complete shared action gate and to share the same mode, exact scope, and rule version.
- An idle browser polls `GET /api/market-scans/polling-identity` instead of repeatedly verifying the full leaderboard graph. The response contains only domain-separated opaque change tokens for the global latest slot and the selected mode's latest-published slot; it carries no result rows and cannot authorize a leaderboard, probability filter, export, or action. An unchanged fingerprint performs no trusted snapshot read. Initial load, an opaque-token change, mode/database/schema change, or an explicit refresh still calls the existing fully verified `latest` and mode-scoped `latest-published` selectors before reading results from the trusted published ID. The controller commits a polling baseline only when identity-before and identity-after match, coalesces concurrent ticks, and invalidates stale work on mode/history/surface/query changes. Active-run batch progress is intentionally omitted from the token so the browser switches once to the existing two-second run tracker instead of rehashing on every `updated_at`; status and terminal/seal transitions still change the token. If one stable fingerprint fails a deterministic contract/integrity/HTTP read or a dispatched request times out, ordinary ticks remember that fingerprint and return to identity-only polling; a token change or explicit refresh permits one new trusted attempt. The exact admission-busy `503` is transient and follows `Retry-After` instead of poisoning that fingerprint.
- Each result row exposes two separate actions. **查看扫描快照** expands only persisted score components, tie-break values, dates, sources, adjustment mode, fallback/degradation flags, rule version, and run ID; it never requests data or recalculates. **打开当前个股分析** enters the live workbench with a visible warning that current data is not the historical snapshot.
- The leaderboard UI explicitly identifies `上海A股（主板）`, `科创板`, `深圳A股（主板）`, `创业板`, and `北交所`, and supports multiple markets, up to 20 industry fragments, min/max score/trend/change/turnover/amount/quality ranges, and up to three unique sort levels. The same normalized query drives paging and XLSX export, including the explicit listing-board label. A published leaderboard exports `榜单`, `评分明细`, and `导出信息` sheets, preserves six-digit codes as text, blocks formula injection, and performs no provider or scoring work.
- Runs persist the current stage, wall/work durations, calls/items, and SH/SZ/BJ progress. Canonical per-market success coverage is `success / (total - skipped)` (zero when that eligible denominator is zero), while eligibility remains a separate guard. The API normalizes the frozen pre-receipt legacy shape, including production `#85`; current writes and arbitrary count/percentage mismatches still fail closed. The UI shows elapsed time, effective throughput, a sample-gated ETA (`估算中` before enough evidence), and separates terminal evidence into **发布阻断**, **已通过门禁**, and **数据源告警**. A passed score-distribution gate stays green even when a snapshot-time gate blocks publication; legacy combined messages remain readable. Warm-cache K-line batches reuse one SQLite connection while every stock still requires a provider response; see [`docs/research/FULL_MARKET_SELECTION_PERFORMANCE_2026.json`](docs/research/FULL_MARKET_SELECTION_PERFORMANCE_2026.json) for the repeatable cold/warm evidence and provider-bottleneck limit.
- Offline evaluation starts from frozen published ranks, separates mode/scope/rule-version cohorts, deduplicates repeated same-session scans, and never mutates production weights. The retained v5.5 report is explicitly a historical `full-market-score-v4` baseline. A current v5 execution is therefore reported as contract-incompatible until a separately reviewed v5 evaluation artifact exists; its old metrics cannot authorize or promote the v5 run. Every production snapshot persists digest-verified 61-session point-in-time feature and quote/metadata evidence plus separate ordinal Alpha/confidence/risk/tradability dimensions; these are not probabilities. PBO and DSR remain `not_computed`, promotion is never automatic, and the retained [frontier study and verified run71 boundary](docs/research/FULL_MARKET_SCORING_FRONTIER_2026.md), [compact v5.5 report](docs/research/FULL_MARKET_SELECTION_SHADOW_V55_2026.json), [2026 evaluation summary](docs/research/FULL_MARKET_SELECTION_EVALUATION_2026.md), [production report](docs/research/FULL_MARKET_SELECTION_EVALUATION_2026.json), and [historical v4/v5 comparison](docs/research/FULL_MARKET_SELECTION_SHADOW_V5_2026.json) remain dated research snapshots rather than current-v5 promotion decisions.
- A v6 run is publishable only when global and SH/SZ/BJ success coverage over eligible rows reaches 95%, at least 90% of each market remains eligible, the capture envelope is sealed with the exact universe count, every row has a parseable `quote_observed_at`, and each of three independent time gates stays within 20 minutes: run-level quote-capture duration, global observation-time span, and event-time span inside each market. The event-time difference across SH/SZ/BJ is retained as a diagnostic only because different market/provider feeds need not stamp events on the same clock edge. No systemic same-day K-line lag cluster may be present. A versioned score-distribution gate also audits distinct-score coverage, the largest tie group, boundary saturation, and top-100 ties so a constant or collapsed ranking cannot be published as healthy. Deterministically ineligible rows such as suspended, single-price, or too-short-history stocks remain visible but do not dilute the eligible coverage denominator; the separate 90% guard prevents a systemic data problem from being hidden as mass skipping. A 30-minute whole-run wall-clock budget and repeated completed-trading-date checks stop stale work without permitting partial-snapshot reuse.
- Automatic after-close scans create `official` runs only. They run a bounded SH/SZ/BJ stock-pool, quote, and five-row completed-daily-K preflight before creating a formal batch. Retryable scheduled failures create a fresh linked v6 full-universe recomputation after configured 10/30/60-minute delays, stop after the configured limit or trading-date change, never start or retry `preopen`, and never restart a user-cancelled, manual, intraday, or merely degraded batch.
- After one full verification establishes a same-day terminal no-action state, one-second scheduler ticks compare only its lightweight persisted header and database identity while the leadership epoch is unchanged. Unrelated monitor-event writes therefore do not rehash the snapshot graph. A market-scan header/database identity or leadership change, or five minutes since the last full audit, forces complete verification again. This hint never authorizes publication or an action: every API snapshot read still verifies immediately and fails closed on tamper, even during the five-minute scheduler interval.
- Browser polling has the same non-authorizing character but a different detection window. A result-only mutation leaves the lightweight polling token unchanged by design; it is caught whenever the browser performs a trusted selector/result read and by the scheduler's full audit no later than its five-minute interval. A selected run header, seal, database file, or schema change alters the opaque identity immediately. Dropping immutability triggers or forging a stable/old identity never grants trust because results, probability, export, and action boundaries still run full verification and canonical replay.
- Batch pressure control starts at the configured concurrency, halves it on provider busy/timeout/retry-after or systemic unavailable signals, and restores one slot per healthy batch. All waits still consume the scan-wide provider budget; ordinary per-symbol coverage misses do not slow the whole market.
- Full-market scoring is local and deterministic. It never calls an LLM per stock; LLM use remains an optional, on-demand explanation path for a selected stock.
- Every strategy/research consumer that requires an action-capable formal frozen leaderboard revalidates the shared action-source gate. For current v6/v5 runs, a green `score_distribution.pass` label is necessary but no longer self-authorizing: publication itself must replay the fixed capture/count/time/stale/coverage/distribution policy from persisted evidence, generate exactly one repository-owned `publication.canonical_replay.v1` receipt, and bind that receipt to the immutable rule registry. Later action reads replay the same receipt and every justified skip. Result/probability reads use one request-local, query-only SQLite snapshot that verifies the whole seal once, fuses its canonical result observations into exactly one receipt replay, captures outbox and score-contract state in that same transaction, permits one page read, and cannot cross requests, transactions or threads. The four ordinary dashboard trust reads (`latest`, `latest-published`, run detail, and results) share one process-local, capacity-one, non-queueing admission slot: saturation returns exact no-store `503` plus `Retry-After`, never cached authority. A cancelled caller does not release the slot until its owned worker actually ends, and shutdown drains those workers before closing runtime or storage. Persisted snapshot and artifact integrity failures fail closed with non-disclosing HTTP `409` responses, and every success, validation, dependency and error response under `/api/market-scans`, `/api/discovery`, and `/api/strategy-lab` is `Cache-Control: no-store`.

### Published Snapshot Trust Boundary

Every newly successful or degraded publication is sealed inside its final write transaction with `market-scan-snapshot-digest-v2`. The canonical digest covers every persisted public run field except the digest itself and every persisted public result field, including the full public `metrics` and `score_details` projections, in symbol order under strict finite canonical JSON. The verifier feeds that exact v2 byte stream to SHA-256 directly from the ordered SQLite cursor, canonicalizing one result at a time; it never materializes the complete result list, so additional memory scales with the largest canonical row rather than the snapshot row count. Empty arrays, Unicode, float encoding, ordering and strict duplicate-key/non-finite rejection remain byte-compatible with the previous materialized v2 algorithm. This is an implementation optimization, not a new seal contract: existing digests need no migration, graph rewrite or reseal. Derived progress, coverage, elapsed, throughput and ETA values are reconstructed from those sealed inputs. Publication also proves `finished_at <= updated_at <= snapshot_sealed_at` and every result `updated_at <= run.updated_at`.

On the frozen production verifier probe, streaming reduced observed peak RSS from about 1.067 GiB to 22 MiB and wall time from about 3.65 seconds to 2.63 seconds while returning the same stored v2 digest. These are one-machine observations, not memory or latency SLAs; correctness and fail-closed behavior remain the admission boundary.

SQLite enforces the sealed graph, not only application convention. Five triggers reject update/delete of a sealed run and update/delete/insert of any child result: `trg_market_scan_published_run_immutable`, `trg_market_scan_published_run_no_delete`, `trg_market_scan_published_result_no_update`, `trg_market_scan_published_result_no_delete`, and `trg_market_scan_published_result_no_insert`.

The only deletion exception is the configured retention gateway. `ASHARE_RADAR_MAX_MARKET_SCAN_RUNS` is a keep-window target: every unreferenced sealed graph outside that newest-run window is eligible, so unreferenced published history is bounded; genuine database, research-artifact, or retained-retry-graph references may keep the physical count above the target. Retry reachability starts from every retry child outside the deletion candidate set—including newest-window, active and referenced rows—and every directly protected candidate, then follows parents across both candidate and noncandidate rows and pins every reachable candidate ancestor; a visited set bounds a cycle. This keeps a retained lineage intact instead of letting `ON DELETE SET NULL` detach it. Before deleting a published graph, cleanup snapshots the closed probability/future/individual artifact catalog, deeply verifies bounded sources and stream-verifies large probability sources under their exact contracts, and uses the tracked individual fallback only when the strictly enumerated runtime primary is empty; the fixed future summary must conserve schema/count and carry valid item digests plus `offline_replay_verified=true`. Cleanup then begins one explicit write transaction, rechecks those file identities, discovers database references, verifies the v2 seal, and temporarily drops all five triggers only long enough to delete the complete result/run graph. It restores and verifies all five triggers before commit, requires exact deletion counts, and rolls the transaction back on tamper, ambiguous artifacts, reference drift, or any partial failure. A run's own `task_run_id` points outward and does not pin it; scan cleanup precedes task cleanup. The snapshot digest proves integrity while the graph exists—it is not an archive and cannot reconstruct a deleted graph.

Managed research publication and retention now share one re-entrant thread plus cross-process lease per canonical database. Cleanup holds it from the first artifact-catalog read through the SQLite commit; a writer holds it from the final complete action-source recheck through atomic file publication. The lock sidecar is derived from the lexical, non-link, single-link database path under the fixed owner-only `/tmp/ashare-radar-artifact-locks-<uid>` root. This is a coordination boundary for cooperating processes under a trusted same-UID filesystem account, not protection against another process with the same UID deliberately renaming or replacing the lock root. Database-parent and sidecar identities are rechecked while held, and a thread may not nest leases for different databases.

The six project-managed writer families—full-market probability, probability source, outcomes, fit, future range, and individual probability—must use their exact project data directory and the canonical `data/ashare_radar.sqlite3`; path aliases, symlinks, hard-linked databases, mixed managed/external batches, or a different database fail closed. A genuinely external output remains an offline self-contained artifact and is not presented as project-managed runtime evidence. Multi-file publication is all-or-nothing: only newly linked files are journaled by path, size, and SHA-256; on any body or final namespace failure, rollback streams each exact regular single-link file and attempts every cleanup without loading artifact bytes into memory. Existing identical immutable files are not removed.

Retention verifies probability artifacts larger than 64 MiB with bounded memory rather than materializing their JSON. The streaming verifier checks the canonical envelope, algorithm/notice/filename digest, both legacy `payload` and current `generated_at+payload` scopes, repeated nested generation-time binding, single-run fallback, scalar run-ID conservation, and long sorted unique manifests. Custom databases scan only sibling managed directories; they never inherit the project's tracked `docs/research/artifacts` individual-probability fallback. Runtime restore is database-only, so it refuses replacement whenever any sibling managed artifact, fixed future-range summary, or applicable project fallback exists; restore the database and artifacts as one audited bundle or archive the artifacts explicitly first.

That shared action-source gate is now the single lower-level authorization boundary for Discovery queue/apply actions, two-run screen alerts, new strategy execution and simulation/evidence/automation, probability capture outbox and managed artifact publication, future-range generation, TOP100 refresh, and any degraded retry that would copy preserved rows. A current run must also carry the immutable v5 rule contract, exact canonical replay receipt, and replayable official skip evidence. A v6 retry that discards the prior rows and performs a complete fresh-universe recomputation remains allowed by its separate lifecycle contract. History/detail/results, publication summaries, breadth, screening evaluation, offline evaluation and other explicitly read-only browse/audit surfaces may still show a digest-valid but action-ineligible publication with its disclosed origin and diagnostics; visibility is not authority.

The probability-capture outbox is the authoritative runtime state, not a hint inferred from artifact presence. `pending`/`processing` means **正在归档研究样本** and is polled only for the same run, with a default hard limit of 60 attempts including failed requests. `skipped`, a missing outbox row, or an action-ineligible source is terminal and keeps probabilities/filtering closed. `succeeded` is usable only when its required `archive_digest` exactly matches the source projection's `run_binding.source_integrity_digest`; a fitted probability artifact must bind that same digest as a third identity. A mismatched old artifact is ignored rather than shown. Only live `pending`/`processing` rows temporarily pin their source graph; terminal outbox status alone does not pin, while a separately verified supported file may.

Frozen run `#82` is intentionally not repaired in place. Its original publication has no authorizing score-distribution/replay evidence, so the verified read projects `source_scan_action_ineligible` instead of the generic “尚未生成”. Do not edit its diagnostics, add an outbox row, rewrite results, reseal it, or copy a later archive onto it. Start a new after-close official scan to create a current v6/v5 publication; it should move from **正在归档研究样本** to **点时样本积累中** after source-v3 capture succeeds. Actual percentages still require the unchanged fixed-session label, OOS calibration, joint-estimand and deployment gates.

A fresh terminal `failed`/`cancelled`/`interrupted` run with no publication, including `#86`, makes zero results reads and ends the probability card with `aria-busy=false`, null horizons, and disabled filtering instead of leaving **正在读取证据** visible; a previously selected published leaderboard remains owned by that published run and is not mixed with the terminal run. Queued/running work may remain busy. A results-read failure similarly clears values and filtering and displays **证据读取失败·等待重试** until a successful retry renders the verified projection. Direct API `availability=ineligible_run_contract` is intentionally worded as the generic **来源批次不符合研究归档合同·未进入归档**, because a published intraday/TOP100/legacy-seal run can also be contract-ineligible; only the synthetic terminal-unpublished state asserts that a batch was not published.

`snapshot_seal_origin` is an authorization boundary:

- `publication` proves the digest was created in the original publication transaction, but origin alone is insufficient: each action above also passes the exact full-scope and diagnostics-v1 `score_distribution.pass` consistency gate. It is required for Discovery run/apply/leaderboard/enqueue/rank-change operations, both snapshots in delta and screen-alert comparisons, new Strategy Lab executions/simulations, executable Shadow, strategy-evidence refresh, automation latest/action, probability capture and managed artifact use, future-range research, degraded retry-copy, and TOP100 refresh.
- `legacy_backfill` proves only that a retained row graph was internally consistent when the migration read it. It remains available for history/detail/results, publication-summary audit, breadth and screen evaluation, offline evaluation, backup verification, and compatible historical strategy-execution audit; it cannot authorize any of the actions above. In particular, runtime run `#80` is a verifiable `legacy_backfill` audit snapshot, not an authorization source.

The startup migration deliberately resets every pre-existing published row to `legacy_backfill`, recomputes the v2 digest, verifies every seal, installs all five triggers, and records its marker last in one rollback-capable compatibility transaction. Strategy-execution source migration runs only after that completes. It binds existing executions to the verified source digest and seal origin when the source run exists; a retained orphan remains `source_snapshot_verification_status=legacy_unverified` with null source digest/origin and is forensic evidence only. New execution inserts require a verified source binding, while any new execution, evidence refresh or action additionally requires the complete shared action-source gate. Do not edit or reseal a published graph, manually drop the triggers, or rewrite `legacy_unverified` history.

### Trustworthy Screening Workbench

Expand **可信筛选工作台** on a selected published full-market leaderboard to inspect the market and the current filter before acting on individual rows. The panel is lazy: only its first expansion or an explicit refresh reads the selected run. It requests three independent, `no-store` projections over the same frozen SQLite evidence and can render one projection even when another is unavailable:

- `GET /api/market-scans/{run_id}/breadth` returns population counts by result status and market, score coverage/distribution/percentiles, advancing/flat/declining counts, and industry cross-sections.
- `POST /api/market-scans/{run_id}/screen/evaluate` returns the canonical `ScreenSpecV2`, its SHA-256 digest, a sequential condition funnel, aggregate exclusion reason codes, the paged matches with passed-condition codes, and bounded near misses with explicit failed or missing conditions.
- `GET /api/market-scans/{run_id}/delta` compares only the immediately previous published run with the same `(mode, scope, rule_version)`. It reports Top20/50/100 entrants, exits and present-but-unrankable rows, rank/raw-score movement, market/industry exposure changes, and frozen quote/K-line/metadata evidence changes. No compatible predecessor is an explicit unavailable state, not an inferred empty comparison.

The workbench does not deserialize every large result envelope. Breadth reads only `status`, `market`, `score`, `change_pct`, and `industry`; evaluation reads the executable scalar projection, computes membership and ordering, and then hydrates only the requested page plus bounded near misses. Request limits cap that final full-detail hydration at 300 unique symbols (200 page rows plus 100 near misses), while digests and results remain identical to the full-hydration semantics.

`ScreenSpecV2` is the strict executable contract shared by ordinary result queries, saved discovery plans, the workbench evaluator, and the filter metadata embedded in Excel. It covers status, SH/SZ/BJ markets, industry fragments, ST/new-stock state, score/trend/change/turnover/amount/quality ranges, frozen confidence/risk/tradability research-score ranges, keyword search, and one to three unique sorts. Unsupported or extra fields fail validation instead of being ignored. The same parameterized SQLite compiler supplies result pages, saved-plan application, and Excel row selection/order; `导出信息` records the schema, canonical spec JSON, and digest. Missing numeric evidence is excluded as missing when a condition needs it and is displayed as `--`, never converted to zero.

Saved discovery plans are schema v2. In addition to the prior conditions they retain keyword and frozen confidence/risk/tradability thresholds plus one of five column views: overview, trend, liquidity, risk, or research. Existing v1 SQLite rows migrate with `overview` without changing their original revision, while v1 archive checksums retain their historical field surface; a later full update writes v2, and v1 archive import cannot smuggle v2-only fields outside its checksum. **记录变化提醒** recompiles the selected plan against the current and prior compatible frozen runs, uses the expected plan revision, suppresses a current `pending`/`missing`/`skipped` symbol instead of falsely calling it an exit, and persists one event for a plan revision, run pair, and semantic digest. Repeating the unchanged request returns the existing event.

These paths do not contact a quote/K-line provider and do not re-score or re-rank the selected run's frozen v4/v5 contract; the explicit change-reminder action only appends its local idempotent event. Every breadth/evaluation/delta response binds run identity and carries a canonical digest. Upside probability remains a separate Shadow evidence contract: `ScreenSpecV2` deliberately has no probability condition, and the trustworthy workbench asks the user to clear a probability threshold rather than silently accepting or ignoring it. The existing probability filter remains separately gated by the fail-closed `probability_filter_qualified(...)` authorization rather than a loose status boolean.

The scan workspace validates API response contracts, uses a single bounded exponential-backoff poller, tracks the active task separately from the selected-mode published result, and keeps the last publishable leaderboard visible while a new run is running or fails. It resets pagination only when the displayed result run changes, cancels obsolete discovery requests, and resumes immediately when the browser returns online. Static assets are revalidated with `no-cache`, and the scan ES modules share one version mapping.

On a Shanghai trading day, `preopen` (**盘前复盘**) is available only in `[00:00, 09:15)`. It fixes both `quote_date` and `data_date` to the previous completed trading session and requires the completed-session quote close to match that session's daily-K close. It is an independent mode with its own rule fingerprint, history, and retry cohort; it is not an `official` probability-source capture and is not eligible for the official-only future-range study. `intraday` is available in `[09:30, 15:15)`. It requires quotes from the current trading day, uses completed daily K-lines only through the previous trading day, and validates each quote's previous close against that final K-line close. Its output is a provisional intraday leaderboard and may change with the live market. `official` is available from 15:15 onward, when `quote_date` and `data_date` are the same completed trading day and the quote close is checked against the same-day K-line close. The server rejects `preopen` on weekends and holidays, and the 09:15-09:30 trading-day gap admits neither `preopen` nor `intraday`; on a non-trading day, only an `official` run may resolve both dates to the latest completed trading day. Optional after-close scheduling, concurrency, timeouts, retention, degraded behavior, and troubleshooting are documented in the [Operations Guide](docs/OPERATIONS.md).

### Strategy Decision Laboratory

The expandable strategy laboratory inside **全市场选股** turns a frozen published scan into an evidence-first research workflow without changing that run's persisted production ranking contract.

- A strict, immutable `StrategySpec v1` carries the Shanghai main board, STAR, Shenzhen main board, ChiNext, and Beijing board universe; exclusions and typed hard filters; independent Alpha 1/5/20-day, confidence, risk, and tradability objectives; portfolio, rebalance, execution, and evidence policies; a revision; and a stable semantic SHA-256 fingerprint. Saved strategy roots and all immutable revisions are included in local user-data portability.
- The Chinese entrypoint produces a structured draft, explicit defaults, ambiguities, unsupported clauses, and a deterministic dry-run plan. It never executes generated SQL or user code, does not require an LLM, and requires an explicit confirmation before save or execution.
- Executions bind the exact strategy revision/fingerprint and shared-action-gate-eligible market-scan digest/origin, rule version, `data_as_of`, data date, cost fingerprint, result digest and `strategy-execution-freshness-v2` decision. Optional selectors such as `revision`, `run_id`, or `data_date` are resolved before the execution fingerprint is built, so two requests that identify the same immutable semantics replay identically. `latest_scan` is rejected before any write when the trusted exchange calendar is unavailable, the snapshot is future-dated, or its exchange-session age exceeds the strategy limit; exact historical replay keeps its frozen decision-time semantics but does not bypass the shared action gate. A migrated orphan with `source_snapshot_verification_status=legacy_unverified` remains audit-only and cannot be used for research or action. Both paths keep the original production rank visible, paginate candidates, expose independent objectives and Pareto membership, and can legitimately return `no_trade` when evidence or constraints fail.
- Portfolio drafts execute—not merely serialize—equal, inverse-risk-constrained, and explicit custom weights. They apply buy/hold hysteresis, evidence-source completeness/allowlists, A-share lots, effective-dated board price limits, suspension/zero-volume checks, T+1, costs, slippage, capacity, and stock/industry/board limits. Non-custom allocation iteratively refills from the deterministic candidate pool after constraint failures and recomputes weights; `replacement_attempt_count`, `pool_exhausted`, and `underinvested_reason` expose a portfolio that still cannot fill. Custom weighting never adds an unrequested symbol. Counterfactuals and one-weight-at-a-time sensitivity use persisted deterministic fields only. Simulation output is an idempotent, sealed, readable paper plan and never reaches a broker.
- The evidence center validates and reads the retained compact v5.5 artifact rather than running a multi-minute cross-date evaluation in an HTTP request. It exposes the research boundary, execution and market coverage, production Top20/50/100 evidence, rank deltas, Shadow constraints, cost/capacity robustness, exposure and promotion gates while keeping unavailable numbers typed as unavailable. The latest evidence row, reconstructed execution result and simulation plan are checked against their canonical digests and exact identities; corruption fails closed without falling back to an older row. A legacy or incompatible artifact is an explicit unavailable state, not an empty successful report. Generate a newer baseline explicitly with `tools/evaluate_market_scan_shadow.py --compact`; there is no automatic promotion control.
- The separate `GET /api/strategy-lab/executable-candidate-shadow` projection is also read-only and `no-store`. Its current schema/spec are `market-scan-executable-candidate-shadow-v2` / `executable-candidate-shadow-spec-v2`. It accepts an explicit current `official` exact-full-market `run_id` and notional only after the source passes the complete shared action gate and the user submits the Shadow form; opening the market workspace or Strategy Lab never calls this heavy endpoint. The response keeps `status=research_shadow`, `efficacy_status=not_generated`, `production_effect=none`, preserves each selected stock's production rank beside its Shadow order, and shows frozen filter/risk/cost/capacity proxies, industry/board exposure, estimated turnover and refill/underinvestment evidence. It explicitly reports historical ADV as unavailable: capacity uses only the frozen signal-session amount, suspension and one-price-limit states are daily-bar proxies, cost omits live spread/impact/depth, mixed industry taxonomy is not a risk model, and the candidate order is not verified Alpha. A one-machine, on-demand run-77 observation took 9.2 seconds; this is evidence for keeping the interaction explicit, not an SLA. Errors stay local, stale responses are ignored and an abandoned request is aborted.
- Version-pinned schedules run at most once for a published scan, isolate failed attempts, and emit fingerprinted new-entry, removal, utility-threshold, stale-data, or invalid-evidence events. Archiving disables an active schedule at its next claim boundary, and an archived strategy cannot start a new latest-scan execution or be re-enabled. They create research executions and alerts only—never orders.

The browser editor exposes named and custom objective profiles, all three weighting methods, custom symbol weights, portfolio exposure/capacity limits, and buy/hold thresholds. It keeps invalid compile state and save availability consistent, clears a stale execution when the selected strategy or revision changes, renders simulation/schedule lineage, and compares immutable `StrategySpec` revisions separately from execution-result comparisons.

Future event timelines, crowding/fundamental divergence, evidence debate, personalized review, strategy sharing, AutoML, and broker integrations are extension boundaries only. They are not enabled and cannot bypass deterministic compilation, point-in-time evidence, or manual review.

## Review Workspace Trust Boundary

Advice review is a local **Research Shadow** workflow with
`production_effect=none`; it does not change formal advice, the individual-stock
diagnosis, full-market ranks, saved screening membership, or any live order.
Each plan freezes one persisted advice snapshot. Its editable current projection
is backed by an append-only `(plan_id, revision)` ledger whose canonical JSON and
SHA-256 digest are checked on every active read. Update, evaluation, paper-plan
creation, and archive operations use the caller's expected revision; a stale
browser or concurrent writer receives a conflict instead of silently changing a
newer plan. Archiving hides the plan from active views but retains the source
advice, every plan revision, and every evaluation attempt for audit.

Evaluation is append-only as well. An identical input/result replay reuses the
existing row, while a materially different canonical input or result for the
same plan revision, `as_of`, and rule appends the next `attempt`. The current card and global summary
select the latest `as_of` and attempt only for the current plan revision. The
current `advice-review-evidence.v2` contract binds the server audit time and
attempt into the canonical input; legacy v1 remains readable only through the
metric-free conservative projection. Plan, input, and result digests are
rechecked on read, while the digest of the complete
source window is sealed inside the canonical input binding; missing,
legacy-unverified, or digest-inconsistent evidence is projected conservatively as
`insufficient_data` and cannot contribute returns, MFE, MAE, hits, or favorable
statistics.

The public evaluator rejects a future `as_of`; `evaluated_at` is a server-owned
trusted audit timestamp. Daily evidence is cut at the 15:15 Shanghai publication boundary,
uses the trusted exchange-session calendar, and requires one continuous,
unique, non-fallback PIT `qfq` row per expected session with supported data,
execution, suspension, and corporate-action metadata. A suspended session can
satisfy calendar coverage but its carried OHLC cannot trigger target/stop or a
paper fill. A missing fixed session, conflicting duplicate, mixed contract, or
invalid source date fails closed. Same-day target and stop touches remain
explicitly ambiguous because daily bars cannot establish intraday ordering.

Every `/api/reviews...` and `/api/paper-trading...` response path is
`Cache-Control: no-store`; unexpected, dependency, internal-validation, and
response-validation failures expose one generic unavailable message. Browser
contracts independently validate symbol/plan/revision ownership, digest shape,
time ordering, source-session counts, status/conclusion/metric consistency, and
summary count conservation before rendering. Global statistics and the due queue
load independently, so one unavailable projection does not erase another valid
one.

## Paper-Trading Review

Open **复盘工具 → 模拟交易** to validate frozen review plans against completed historical daily bars. This is a deterministic paper-trading model, not a broker connection: it never submits a real order and cannot reproduce intraday queue position, liquidity depth, or every exchange exception.

- A strategy can be frozen only from the exact current review-plan revision and canonical plan digest. The repository rechecks both under `BEGIN IMMEDIATE` before insert, closing the read/write race, and permits at most one retained strategy row for that plan revision. A strategy that has never appeared in a run may be deleted and recreated; once referenced by an immutable run it cannot be deleted.
- A strategy becomes eligible only after its activation date and enters at the next tradeable daily open. Priority, allocation, and a 1–60 session entry expiry are explicit; a target or stop reached while waiting invalidates the late entry.
- Stock T+1 is enforced. An entry-day target/stop signal is latched and exits at the next sellable session open; suspension, zero volume, locked limit-up buys, and locked limit-down sells remain unfilled and appear in the event ledger. A suspended session neither marks an open position to a carried close nor clears a pending exit.
- Effective-dated main-board, STAR, ChiNext, and Beijing-board quantity/price-limit profiles replace a universal 100-share assumption. The listing-session counter is inclusive: a trusted listing date is session 1, and the catalog's exact left boundary is valid coverage rather than an underflow. A listing date before the trusted calendar's minimum still fails closed with `trading_calendar_out_of_coverage`. Historical ST state is applied only from its own effective date; missing or future-dated ST/listing metadata is surfaced as degraded rule quality rather than silently treated as certain.
- Versioned base, conservative, and stress cost profiles split commission, minimum commission, sell-side stamp duty, transfer fee, and buy/sell slippage. Net/gross return, cost drag, benchmark/excess return, drawdown duration, win/loss quality, turnover, exposure, and sample-gated risk metrics are reported separately. Benchmark return starts at the first simulated session open so it shares the portfolio's investment clock.
- Only completed canonical trading-day bars at or before `as_of` can affect price rebasing, execution, benchmark returns, fingerprints, or provenance. The complete visible source window must cover every trusted exchange session with PIT `qfq` and execution metadata, including explicit suspended sessions; conflicting, malformed, or non-trading-date rows inside that window are rejected, while later rows cannot invalidate an earlier replay. Benchmark valuation uses trading sessions only.
- Every simulation appends an immutable run with strategy, market-data, configuration, rule, cost, source, and SHA-256 fingerprint provenance. `20260813_paper_trading_output_digest_v3` additionally seals the complete persisted run projection used by dashboards and exports. Reads recompute that output digest before returning a run; its canonical form omits surrogate row IDs and audit timestamps but binds each strategy's revision/digest/symbol identity, deterministic ordering, account/configuration and all stored results, trades, equity and events. A child row whose strategy is not a member of that same run, a cross-symbol child, or a declared strategy/execution/outcome count mismatch fails closed instead of being filtered out of the hash. Historical performance always uses that run's frozen `configuration.initial_cash`, even if a damaged database exposes a different current-account value. Account principal cannot be changed after either a strategy or run exists. Historical runs can be reopened, compared, exported as JSON or formula-safe UTF-8 CSV, and included in local user-data backup/import.

Review and paper records retain source-window fingerprints and derived evidence, but not a content-addressed copy of every raw K-line row. Their ordinary exports therefore prove the stored commitment and outcomes; they are not standalone long-term market-input replay bundles after cache retention or provider revision.

## Discovery and Research Loop

- Saved discovery presets filter only fields already persisted by a completed scan: multiple markets/industries, ST/new-stock state, all six min/max ranges, and up to three sort levels. The UI can create, fully edit, apply, rename, delete, import, and export these definitions with optimistic revision checks. Applying a preset never fetches another data source.
- One row, the current page, or all pages of the current filtered result can enter the research queue. Requests are chunked to the 100-symbol API cap and retain immutable source run/preset/revision provenance. Adjacent published runs are comparable only when mode, scope, and rule version all match.
- Historical watchlist-condition scans render read-only evidence with the requested `as_of`, rule version, condition results, and stored metrics. Opening the current stock workbench is a separate explicit action, so present data is never presented as a historical snapshot.
- After the daily-bar publication boundary, bounded scheduler tasks refresh advice for active, non-excluded watchlist rows without an LLM and evaluate due review plans. Each stock is isolated; stale dates, weak quality, invalid contracts, and unchanged conclusions do not create misleading snapshots. Mark-viewed uses the displayed advice ID as a watermark, preserving changes produced later.
- Review plans merge snapshot-derived structured evidence with user notes. Evidence records carry an ID, value, direction, data date, nature, and rule version. Evaluation records executable trigger/invalidation bases, target/stop outcomes, return, MFE, and MAE, while `GET /api/reviews/summary` exposes a current-revision, digest-verified global local-only aggregate. It is descriptive review evidence, not a probability, causal performance estimate, or trading guarantee.

Offline ranking research is available without touching production scores:

```bash
python tools/evaluate_market_scan.py --database data/ashare_radar.sqlite3 --output evaluation.json
python tools/evaluate_market_scan_shadow.py \
  --database '<VERIFIED_STATIC_BACKUP>/runtime.sqlite3' \
  --run-id 71 --mode official \
  --variant v5_4_skip5_multilevel_residual \
  --variant v5_4_skip5_multilevel_residual_volume_lifecycle \
  --variant v5_5_bounded_nonlinear_stability \
  --bootstrap-samples 1000 --compact \
  --output shadow-comparison.json
python tools/evaluate_market_scan_probability.py --database data/ashare_radar.sqlite3 --output-dir data/market-scan-probability
python tools/runtime_data.py backup --destination '<VERIFIED_BACKUP_DIR>'
python tools/runtime_data.py verify '<VERIFIED_BACKUP_DIR>'
python tools/backfill_market_scan_probability_history.py --source-database '<VERIFIED_BACKUP_DIR>/runtime.sqlite3' --target-database data/research/market_scan_probability_history.sqlite3 --output-dir data/research/market_scan_probability_history --symbol-limit 120
python tools/backfill_market_scan_probability_replay.py --database data/research/market_scan_probability_history.sqlite3 --output-dir data/research/market_scan_probability_replay --start-date YYYY-MM-DD --end-date YYYY-MM-DD
python tools/evaluate_market_scan_future_range.py --database data/ashare_radar.sqlite3 --output-dir data/research/market_scan_future_range
python tools/benchmark_market_scan.py --database data/ashare_radar.sqlite3 --output performance.json
```

The evaluator reads published frozen ranks and only later persisted complete trading days. It reports Top 20/50/100 gross/net returns, equal-weight excess return, session-block bootstrap intervals, session maximum drawdown, adverse excursion, Rank IC/ICIR, deciles, monotonicity, stability, turnover, execution status, and board/market/segment/liquidity/scan-time/quality strata. Cohorts below either the stock floor or the independent-session floor are `insufficient_data`; reports never rewrite score weights or a historical `rule_version`.

The probability command is SQLite-read-only, but persists immutable, content-addressed research artifacts while retaining `probability=null` whenever evidence is insufficient. Current source capture is `market-scan-probability-source-artifact-v3` / `market-scan-probability-source-snapshot-v3` with `market-scan-probability-source-full-market-coverage-v2`. It writes only current `full-market-score-v5`, includes explicit success/missing/skipped conservation for ALL/SH/SZ/BJ, and maps v5 `continuous_trend.score` to the stable probability feature alias `rank_refinement` with no v4 rank discount. Frozen source v1 and v2 artifacts, including their v4 score contracts, remain exact read/audit compatibility evidence; they are never silently rewritten as v3 or accepted as a current write. The probability model/feature/label/split/result contracts and unchanged H1/H5/H20 fixed-session, OOS, joint-estimand and deployment gates remain independent of this intake upgrade. The UI leaves unavailable values blank and never substitutes 0% or 50%.

Filtering is a stronger authorization than display. It requires an opaque token returned only by strict verification of `market-scan-probability-filter-authorization-artifact-v1` with payload version `market-scan-probability-filter-authorization-v3-raw-drift-joint-execution`; an ordinary mapping or self-reported pass flag cannot authorize anything. Verification replays the complete raw OOS prediction set, proper-score intervals/ECE, the preregistered candidate family plus BH-FDR, temporal drift and raw session economics, and binds the exact evidence/metrics/input, horizon/target, official scan cohort and unique production score rule/hash. Artifact time is timezone-aware, content-bound, non-future and mature; ambiguous unsealed legacy latest rows fail closed. Filtering only narrows rows and preserves persisted production ranks.

Probability fitting is additionally isolated by the exact `(mode, scope, rule_version)` contract: dates, labels, equal-weight benchmark, train/calibration/test folds, models and review gates are never borrowed across cohorts. The strict `market-scan-probability-deployment-refit-v1` code path refits on a separate train/calibration corpus with its purge, replay and freshness checks rather than reusing the last OOS evaluation fold; only a verified `market-scan-probability-deployment-estimator-artifact-v1` token can serve a new prediction. No such verified deployment artifact currently exists. More fundamentally, current v4 labels cover the executable-only conditional population and cannot identify the required all-decisions joint event. Authorization, filtering, deployment refit and new prediction therefore all fail closed; formal run 71/run 77, individual rows and new prediction output remain typed `null`.

`decision-time-joint-execution-probability-v2` is only the contract skeleton for one matured label/OOS-prediction sample, keyed by `{run_id}:{symbol}:{horizon}:{target}` after its fixed entry and exit sessions have completed. It decomposes the intended all-decisions event into entry fill, executable exit and positive round-trip net return, but it is never a signal-day model input or real-time per-symbol action proof. The current skeleton always includes `observed_joint_outcome_components_unavailable` and `strict_joint_assessment_replay_not_verified`, forces all five probability components to `null`, and returns action qualification false regardless of supplied digests or flags. A future contract needs official unadjusted entry/exit OHLCV plus amount, effective-dated exchange/ST/listing/delisting and corporate-action-aware reference rules, same-session entry/exit capacity, a frozen signal-date full-market cohort with leave-one-out or predeclared external benchmark, observed three-component outcomes and strict OOS assessment replay.

Every newly published, action-eligible `official` full-market run atomically enqueues a typed point-in-time capture in the publication transaction. The runtime leader drains that durable outbox through bounded leases, retry and restart reconciliation. Query code reads action eligibility, outbox state, score contract and the requested results page through one request-local verified SQLite snapshot; it does not race a second cohort read against capture state. Only `succeeded` plus an exact outbox/source digest match exposes the source-v3 projection; pending, skipped, missing or mismatched states stay explicit and cannot reveal stale probability rows. Capture failure never changes the persisted production rank.

The after-close scheduler maintains compact, immutable outcome artifacts under `data/research/market_scan_probability_outcomes/`. For each archived D snapshot, H1/H5/H20 use fixed D+1-open entry and D+2/D+6/D+21 exits. A target that has not closed is `not_mature`; after maturity, a missing fixed-session bar remains `data_unavailable` and is never shifted. The source-research projection separates archived, mature, available, eligible, coverage, next-maturity, maintenance, and fit state. Split v3's two `H+1` purge gaps make the registered H1/H5/H20 fit floors 224/232/262 independent dates with at least 95% label coverage. The bounded `sampled_oos_assessment` trigger remains 260 available sessions per horizon; at exactly 260 the H20 formal split gate is still short and must stay insufficient until 262. The diagnostic keeps at most 300 sessions and 90 SH/SZ/BJ-balanced rows per session (27,000 total), uses `include_records=false`, and binds exact source/outcome pair and research digests. Its sampled benchmark is not the registered full-market benchmark, so `fit_selection_qualified=false`; the leaderboard remains `probability=null` and cannot filter or rerank.

Historical outcome-v1 files may contain one narrowly recognized pre-fix rule-profile shape: an otherwise intact fixed-session record stored `entry_rule_profile_degraded` or `exit_rule_profile_degraded` where current replay verifies the profile. The verifier completes envelope, filename/digest, source/cohort/calendar, every sibling record, quality and limitation checks before raising typed `ProbabilityOutcomeSemanticDriftError`; it does not accept that file as current outcome evidence. Maintenance keeps that typed file as a mechanically bound terminal catalog entry. When its source digest matches and its `(as_of_date, generated_at)` is at least as new as the newest valid outcome, every restart deterministically reports `due=0` and one `skipped`, makes no K-line read, publishes no outcome or fit, and does not retry forever. A strictly newer valid outcome supersedes the drift entry and restores normal maintenance. Source-research still leaves the immutable file untouched, excludes its run and every dependent fit, and exposes `outcome_evidence_status=legacy_semantic_drift_excluded`, `selection_qualified=false`, and an unfitted source-only projection. Digest/mechanical failure, an ordinary replay mismatch, an invalid sibling, a conflicting same-order drift, or a drift bound to another source remains a hard/non-authorizing case. Never edit or re-sign the legacy artifact to make it current.

The retained real official corpus has only two dates: run 71 on 2026-08-11 has 5,499 successful rows out of 5,542 and run 77 on 2026-08-12 has 5,494 out of 5,543. Both retained archives are source v1 and lack the inner production score hash, so both are `legacy_unbound`, zero horizons are filter-qualified, and every H1/H5/H20 estimate remains `probability=null`. The legacy reader preserves historical evidence for audit, while current source v3 accepts PIT evidence v4 plus the current v5 score contract; compatibility never upgrades a legacy row or authorizes a filter. The UI now distinguishes action-ineligible, capture-pending, capture-skipped/outbox-missing, source-archived accumulation, and underpowered research instead of collapsing all cases into **尚未生成研究证据**. None authorizes filtering.

The runtime leader now preloads and deep-verifies source research before publication of its in-memory index. Store and source-research refreshes use non-blocking single-flight plus atomic snapshot replacement: warm readers keep the last complete verified snapshot while one refresher performs deep work, never a partial index. A representative fresh-process preload took 8.158412 seconds versus the previous 13.217-second cold observation; 100 warm projections averaged 0.000756715 seconds. A 201,055,106-byte legacy run-62 artifact returned typed unavailable in 0.00087925 seconds without a deep interactive read. These single-machine observations are budgets/regression evidence, not SLAs or proof that all future corpus sizes are solved.

`historical_replay_v1` is a separate backfill cohort for studying whether longer date coverage is useful before real official archives mature. Create and verify a `tools/runtime_data.py backup` bundle first; an ordinary file copy is rejected because it may omit live WAL content. The history command accepts only the bundle's manifest-bound, sidecar-free `runtime.sqlite3`, uses its static `qfq` cache only as a current-universe/anchor source, and re-verifies it before and after acquisition. It downloads an SH/SZ/BJ-balanced sample of 360 fixed-session Tencent qfq bars into a new isolated SQLite database and binds it to a content-addressed manifest. Replay also requires that database to have no WAL/SHM/journal sidecar, opens it with SQLite `mode=ro&immutable=1`, and rejects a main-file fingerprint change. It then reconstructs 11 common OHLCV features using bars no later than D, maps entry and H1/H5/H20 exits to fixed trusted exchange sessions without shifting a missing bar, and models the absolute `net_return_positive` target with registered costs. Neither stage reuses the official cohort or its net-excess target or can populate an official leaderboard probability: current-universe survivorship, missing historical listing/delisting and ST membership, unavailable amount/turnover, unmodelled price-limit tradeability, public-provider provenance, and possible later qfq rebasing remain explicit limitations. Output is immutable research evidence, not production data or an automatic promotion decision.

Probability-history manifests now use an explicit compatibility matrix: v2 is the current attested format; v1 is readable only through the legacy reader and is labelled either `legacy_attested_v1` or `legacy_unattested`. A deeply verified transitional attested-v1 manifest may be published as a new v2 content address without overwriting the old file. Unattested v1 remains audit-readable but cannot be upgraded or used as current trusted research evidence.

The retained attested 2026-08-11 replay accepted 96 symbols with 279 independent signal dates and 100% fixed-session label coverage. Its reported 5/5/3 sparsest-bin counts, H5 AUC 0.4938, Brier Skill -0.00978 and ECE 0.0980 belong to the superseded feature/label/split-v2 evidence contract. It remains useful for deterministic replay and negative historical context only; it can never authorize the current v4/v3 filter. Under the current split-v3 224/232/262 date floors, 279 dates are numerically sufficient, but a new current-contract evaluation and every authorization gate would still be required. No probability is exported from this legacy study.

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
- User-owned records can be exported and transactionally imported as versioned JSON. Import commits require a matching server preview and verified rollback backup; immutable review-ledger relationships/digests survive collision remapping or reject the import. When an embedded plan identity changes as part of a complete semantic-preserving graph remap, import propagates the new plan digest and recomputes the affected paper-run output digest in the target database; a pure surrogate-ID remap leaves the v3 output commitment stable. A partial/source-wins merge cannot rewrite an account after Paper history exists or any strategy already referenced by an immutable run—re-signing changed history is forbidden. Backup creation, verification, rotation, and restore share a bounded cross-process operation lease and require both SQLite integrity and foreign-key checks, so an in-use bundle cannot be pruned or a broken relationship restored. Scheduled cleanup removes only regenerable/runtime rows through throttled set-based retention; explicit previewed cleanup applies the configured scan keep window and protects active work plus every verified database, file-artifact, and retained-retry-graph reference. Large successful cleanups compact SQLite only when reclaimed pages are material, and compaction failure never rolls back logical retention. Archiving an advice-review plan hides it from active views but retains its source snapshot, canonical revision ledger, and evaluation attempts. Full backup/restore and manual retention cleanup are documented in the [Operations Guide](docs/OPERATIONS.md).
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
