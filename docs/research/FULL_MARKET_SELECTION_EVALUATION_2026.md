# 全市场选股评分与 Shadow v5/v5.4 离线评估（更新至 2026-08-11）

> 2026-08-11 implementation update: production `full-market-score-v4` remains unchanged. The user-facing name is now “趋势强度”, explicitly an ordinal state score rather than an upside probability. The duplicate trend column was replaced by compact confidence/risk/tradability dimensions. Point-in-time evidence v2 now digests the quote, market/industry metadata, status flags, provenance, volume policy and 61 completed bars. Intraday research dimensions and Shadow v5.4 do not combine a current intraday price direction with completed-session volume: without time-aligned intraday volume, the volume lifecycle delta is deterministically zero and the reason is persisted.

> Shadow `full-market-shadow-score-v5.4` adds two candidates on top of replay-compatible v5.3: skip-five-day momentum with sequential shrunk market → board → quality-gated industry → liquidity residualization, and the same candidate with time-aligned volume lifecycle. Broad, unknown or mixed-granularity industry buckets are not mechanically neutralized. ST/new-stock, ATR, drawdown, price-limit progress, turnover extremes and 10万元 capacity are persisted as explicit constraints; tradability is a penalty-only dimension and is not counted again as alpha.

> 2026-08-01 implementation update: future scans now persist digest-verified 61-session point-in-time feature evidence inside each frozen score snapshot. The evaluator prefers this evidence, isolates per-symbol/per-run failures, reports coverage and rejection reasons, audits board/industry/liquidity exposure, and estimates buy/hold hysteresis turnover. Shadow `full-market-shadow-score-v5.3` preregisters four primary candidates: the v5.2 baseline, market/board residual momentum, residual momentum excluding the latest five sessions, and the same signal with volume-lifecycle confirmation/exhaustion. Production `full-market-score-v4` remains unchanged. Historical v5/v5.2 figures below stay historical and are not relabeled as v5.3 results.

New frozen snapshots also expose independent ordinal `AlphaScore_1d/5d/20d`, `ConfidenceScore`, `RiskScore`, `TradabilityScore`, and conservative/balanced/aggressive research utility. They are explicitly not return probabilities or investment advice. Candidate promotion remains manual, requires rule-isolated independent sessions, transaction-cost and exposure checks, verified point-in-time evidence, and—when comparing multiple candidates—at least 40 independent sessions before PBO/multiple-testing review.

The preregistered v1 promotion gate blocks review unless one candidate simultaneously has at least 20 independent sessions, at least 95% attested item coverage, 5-day mean Rank IC ≥ 0.02, positive Top100 5-day net excess after the execution-cost model, monotonic ranking bands, 5-day compounded drawdown no worse than -25%, Top100 hysteresis turnover no greater than 80%, and maximum absolute board/industry/liquidity share deviation no greater than 20%. Passing these thresholds only makes a candidate eligible for human review; automatic promotion remains disabled.

## 结论

候选评分已实现并可持续积累影子证据，但暂不晋级生产。

2026-08-11 的最新只读复跑覆盖 8 个已发布批次、33,150 个评分输入和 27,643 个具有可用前向窗口的观察；严格按 `mode + scope + rule_version` 隔离后，任何可比合约最多只有 4 个独立日期，5 日主晋级合约仍为 0 个独立日期，低于 evaluation-v2 默认的 20 日门槛。并且旧批次的证据尚未对评分所用的全部报价和元数据输入完成摘要校验，`kline_daily` 覆盖式缓存也无法证明当前历史K线就是扫描时点可见的原始修订。股票行数、正收益或相对收益既不能替代独立时间样本，也不能绕过时间截面完整性，更不能被解释为收益承诺。

生产 `full-market-score-v4`、历史 `rule_version` 和已发布榜单均未修改。Shadow 候选从冻结的扫描报价/元数据和 `date <= data_date` 的只读前复权日K重建排名，不会写回数据库或自动晋级；覆盖式K线缓存重建只能用于探索，评估器会把它标为不可用于晋级的输入证据。

## 上涨概率 Shadow（research v3 / label v2）

上涨概率是独立研究层，不是把“趋势强度”除以 100。注册周期为 1/5/20 个持有交易日，5 日“成本后净收益跑赢同批次可执行股票等权市场”为主目标，“成本后绝对净收益为正”单独保留。标签 v2 在扫描后的首个可交易日开盘入场，把入场日记为持有日 0，并在之后第 H 个有效交易日收盘退出；因此 H=1 不是买入当日收盘卖出。A 股 T+1、佣金、印花税、过户费、滑点、容量、停牌、零成交和涨跌停锁单均进入标签，且未成交或不可卖不会顺延到更有利日期。入场或退出日的有效日期规则 profile 缺失或 `quality!=ok` 时直接输出不可建模标签，不能进入等权基准或训练；规则 profile 已验证但日K无法复原盘口先后时，仍可按预注册开/收盘执行模型计算，同时单独保留 `daily_bar_model_limited`，不得把它描述为精确成交证明。

训练、校准、测试、标签、同批等权基准和人工评审门禁严格按 `(mode, scope, rule_version)` 分开。同一 cohort 和报价日期只保留按 `as_of/id` 排序后的最终已发布 run；如果研究编排层仍看到两个 run 身份，则 fail closed，而不是平均或拼接。同一报告含多个 cohort 时，顶层 horizon 只是 cohort 摘要索引，`probability` 必须为空；不能从顶层形成 pooled 概率、base rate、基准、模型或晋级结论，所有可校准结论及人工评审资格都必须指向明确的 cohort contract/digest。

模型只使用时点特征和来源摘要；注册特征覆盖生产分解、Alpha 1/5/20、置信/风险/可交易性、ATR/下行波动/回撤、量价与市场/板块/行业/流动性状态，并把 ST、新股及涨/跌停接近度分开记录，未来/事后字段名直接拒绝。同一日期的全部股票固定在同一 train/gap/calibration/gap/test 分区。默认 5 日研究至少需要 120 个训练日、5 个隔离日、40 个独立校准日、5 个隔离日和 60 个独立测试日，同时要求标签覆盖率不低于 95%、每个概率箱至少 20 个独立日期。所有完整测试窗组成互不重叠的多折 Walk-forward，末尾不足 60 日的余数不冒充完整折；每折独立拟合后聚合全部 OOS 预测，预测保存 `fold_id`，Brier Skill 逐行使用所属折校准期 base rate，只有最后完整折模型用于其后的 Shadow 预测。逐折 split/model/Platt/可选 Isotonic/经验贝叶斯/base rate/digest 与聚合指标都必须通过确定性重放。Platt 是独立校准主方法，经验贝叶斯为基线；Isotonic 只在达到注册样本门槛时作为不自动替换主方法的比较候选，并完整记录未评估/参数/指标。报告同时保留 Brier/Brier Skill、Log Loss、ECE、AUC、概率箱单调性及每箱成本后净收益/净超额/换手/回撤、Top100 成本后净超额、早晚期稳定性、主要市场/板块校准、日期区块置信区间和确定性全输入重放；任一门槛失败都必须输出 `insufficient_data` 和 `probability=null`。

当前真实只读 artifact 覆盖 8 个去重批次、4 个隔离 cohort 和 33,150 个冻结输入。仅旧规则的全市场 cohort 在 1 日周期拥有 3 个独立有效日期，14,654/16,470 条成熟标签可用，覆盖率约 88.97%，仍低于预注册的 95%；其余 cohort 的 1 日有效日期以及全部 cohort 的 5 日、20 日成熟日期均为 0，远未形成任何完整 Walk-forward 折。因此 198,900 条“批次 × 股票 × 目标 × 周期”结果全部只能持久化为空值及原因，不能展示占位百分比、启用概率筛选或宣称有效。8 个新 v2 artifact 已按 cohort 全输入重放通过，机器报告记录 `full_input_replay_verified=true`，只读 SQLite 的评估前后 SHA-256 同为 `63f460431fcc4b11f99e23ea592b605e4c784aa1b8584f69f3c13eb808a1e7ec`。生产 v4 排名完全不变，自动晋级关闭，5 日仍是未来积累数据后的主评估目标。

只读 CLI 不写 SQLite，而把每个 run/symbol/target/horizon（包括空值）保存为 `data/market-scan-probability/` 下的独立 JSON artifact，API 只读加载最新且通过严格 schema 与摘要校验的版本。单文件校验之后，artifact-set replay 还必须验证完整 run manifest，拒绝缺失/重复 artifact 或 record，按 cohort 重新汇集完整特征与标签输入，并逐 target/horizon 重新拟合；只有重建 study 字段、input digest 和 evidence digest 全部一致，机器报告才能写出 `full_input_replay_verified=true` 与 `artifact_set_replay` 明细。artifact 的 SHA-256 是用于检测内容变化的完整性封印，不是数字签名、发布者身份证明或真实性背书。若未来确需导入数据库，必须另建版本化表并由单独、显式审核的导入工具执行，不能把写库能力放进评估器或 API 读取路径。

## 未来区间验证（D+1 / D+2 / D+3）

未来区间验证是上涨概率之外的独立诊断层，目标是回答“冻结趋势评分与随后固定交易日的价格区间、路径和可执行净收益是否存在稳定关系”，而不是另造一个概率。它只接受最终已发布的 `official` 全市场批次，按 `(mode, scope, rule_version)` 隔离 cohort，并从每行摘要校验通过的 61 根时点证据取得 D 日 OHLC。D+1、D+2、D+3 必须由可信交易所日历确定，再精确匹配同一 `qfq`/`daily-kline.v1` 口径；停牌、零成交、未成熟、固定日缺失、版本冲突或 D 日复权重叠不一致时直接保留不可用原因，绝不顺延到下一根“方便”的 K 线。

区间层分别计算目标日最低/HLC3/最高相对 D 日同名值的变化、相对 D 收盘的变化，以及相对固定 D+1 开盘的指定日 low/HLC3/high/close 和累计 MFE/MAE/终点收盘收益。HLC3 只是 `(high + low + close) / 3` 的典型价代理，不能称为 VWAP；日 K 也无法还原盘中先后，因此高低点、MFE/MAE 和区间缺口属于探索性路径证据，不代表这些价格可同时成交。每个可用 offset 都显式声明日内路径未知。

可执行层复用上涨概率 label v2 的有效日期、板块/ST 规则、A 股 T+1、容量和成本模型。由于未来日 K 没有成交额字段，容量只使用冻结信号日 `amount` 作为明确记录的代理，不能解释为目标日真实可成交容量。D+1 因同日不能卖出而固定为不可执行；D+2 对应既有 H=1（D+1 开盘入场、D+2 收盘退出），D+3 对应 H=2，因而没有修改“上涨概率 H=1”的含义。有效行分别保存毛收益、成本拖累、净收益、同一 run 可执行股票等权净收益基准和净超额；未成交、不可卖或规则质量不足时保持空值，不把 0 当作结果。

统计按日期先聚合，覆盖 Top20/50/100/全市场和十分位，报告均值、中位数、正值比例、趋势分 Rank IC、分层单调性及日期区块区间。D+1/D+2/D+3 结果重叠，因此置信区间实际使用按信号日排序、长度为 3 的 moving-block bootstrap。`validation_gap_sessions=3` 是未来若训练或比较模型时必须遵守的最小 train/test 隔离契约；本报告只是描述性 cohort 汇总，不会据此丢弃相邻信号日。不足默认 30 个观察或 20 个独立日期、时点证据覆盖不足 95%、或任一固定 offset 覆盖不足 95% 时整体保持 `insufficient_data`。已有样本的均值、中位数和正值比例仍可作为明确标记的描述性观察，但不能据此宣称策略有效；推断性置信区间、单调性通过判定和有效性结论保持空值，缺失数据绝不填 0。已持久化且训练截止早于信号日、证据摘要匹配的 OOS `calibrated_shadow` 概率只能作为关联对照，不能在这里重新校准，也不能据区间结果改写生产评分、概率模型或自动晋级结论。

只读 CLI 在一个显式 SQLite 事务快照中完成查询，将每个 run 写为 `data/research/market_scan_future_range/` 下的不可变内容寻址 artifact，并记录评估前后文件 SHA-256。静态副本应保持字节一致；若运行中的服务并发写入，摘要会标记 `database_concurrent_external_change_detected=true`，不会把外部变化误归因于 query-only 评估器，也不会破坏已经隔离的事务快照。API 只加载通过严格 schema、身份和 OHLC/HLC3/路径/成本语义重放的版本。**未来区间验证** 页面按需加载固定日、分组和个股明细，Excel 同步输出区间、路径、执行、证据和版本列。旧批次没有 artifact 时显示 `not_generated`，样本不足显示 `insufficient_data`，所有空结果保持空值。

2026-08-11 已对真实 canonical official run 70 完成一次只读事务快照评估：5,499/5,499 行时点证据通过，排除 0 行，证据覆盖率 100%，评估前后数据库 SHA-256 一致。固定目标日为 2026-08-12、08-13、08-14；运行库当时尚未摄入这些后续交易日，因此 D+1/D+2/D+3 覆盖率均为 0，全部明确记为 `not_mature/target_exchange_session_not_ingested_yet`，总状态为 `insufficient_data`。这表示结果尚未成熟，不表示策略有效或无效。未传入概率 artifact，因此概率对照为 `not_available`。生成的 5,499 行 artifact 通过严格加载和语义重放，完整性摘要为 `c6445b37b18abff4c42b49debbaa47e134603c407b9cfa6820f95b6b797e92f5`；生产排名和概率模型均未改变。

## 已确认的生产基线

- 生产 `leader_score` 使用 `full-market-trend-only-v1`：`base=50`、`trend_weight=1.0`、无附加规则，因此主分实际等于趋势分。
- 趋势分已经包含均线相对位置、5/20日线斜率、当日涨跌、20日高低位置、换手率和量价确认。
- 数据质量先按 `(100 - quality) × 0.15` 扣分；不会产生正向强度。
- 中期精排再次使用均线结构、5/20日收益和20日区间位置，但最大只扣 `0.0499`，仅在同一整数基础分内排序。
- 当时最新正式扫描的失败原因是全市场报价快照跨度超过当时的15分钟门槛，而不是评分分布坍缩；发布门槛已于 2026-08-10 调整为20分钟，历史结论仍用于区分数据可信度失败与评分算法失败。

## evaluation-v2 口径

新版评估器保留生产冻结排名，并增加：

- `minimum_session_count=20`；状态同时要求股票观察数和独立扫描日期数达标。
- 先按扫描日期聚合，再以日期为区块做确定性 bootstrap 95% 置信区间。
- 每日 Spearman Rank IC、ICIR、十分位收益、Top 20/50/100 排名带和单调性。
- 按扫描日顺序计算的累计收益最大回撤，以及逐股窗口内最大不利变动（MAE）。
- 排名稳定性直接使用冻结榜单；没有后续收益的最新批次不会再被误算成100%换手。
- SH/SZ/BJ、主板/科创板/创业板/北交所、ST/新股、流动性、质量、市场环境和扫描时段切片。
- 可交易情景使用固定10万元名义本金：下一完整交易日开盘买入，T+1后在目标日或最多延后5日的下一可卖开盘退出；复用按日期和板块版本化的规则以及佣金、最低佣金、印花税、过户费和滑点模型。
- 涨停买不到、跌停卖不出、停牌、零成交、容量不足分别记录为 `unfilled`/`data_unavailable`；日K无法证明盘口成交时保留 `model_limited`。

生产评估的只读数据摘要：

| 项目 | 数量 |
| --- | ---: |
| 去重后已发布批次 | 8 |
| 至少具有一个前向窗口的批次 | 7 |
| 评分输入行 | 33,150 |
| 具有可用前向窗口的股票观察 | 27,643 |
| 跨合约独立扫描日期 | 4 |
| 单一可比合约最多独立日期 | 4 |
| 5 日主晋级合约独立日期 | 0 |
| 默认晋级最低独立日期 | 20 |

## Shadow Score v5

候选分不进入实时逐股扫描，完整版本按以下顺序计算：

1. `trend_continuation`：20/60日收益和10日尺度的MA20斜率，先在一个因子内部去重聚合。历史 v5 使用板块中位秩百分位；v5.2 改为全市场与板块中位秩的连续收缩，板块权重随样本平滑增长且最高为50%，消除29→30只时的规则突变并保留跨板块可比性。
2. `volume_confirmation_delta`：涨跌方向与K线重算的5/20日量比构成连续确认项，限制在 `[-6, +6]`；持久化量比只留作差异审计，不再直接驱动候选分。
3. `overextension_penalty`：涨幅接近板块涨跌停、价格偏离MA5/MA20的ATR倍数和极端20日位置，只扣分，最多20分。
4. `liquidity_penalty`：成交额、10万元名义本金占当日成交额比例、过低/过高换手，只扣分，最多15分。
5. `risk_penalty`：ATR/价格、下行波动率、60日最大回撤和跳空频率，只扣分，最多15分。
6. `confidence_penalty`：数据质量、兜底来源、元数据和历史长度，只扣分；ST和新股另有显式风险扣分。

每个版本都保存完整评分规范、SHA-256、板块归一化摘要、输入摘要和确定性排名摘要。预注册的消融只有：完整版本、去掉过热、去掉风险、去掉流动性；没有在当前短样本上搜索“最佳权重”。

## 2026-08-11 Shadow v5.4 只读复跑

两个 v5.4 候选分别从 6 个可重建批次中完成 27,559/27,559 行评分回放；可用于前向比较的全市场合约最多只有 4 个盘中独立日期。所有点估计都只能用于发现问题，不能用于确认有效性：

| 候选 | 次日独立日 | 次日 Rank IC | Top100 次日毛收益 | 市场超额 | 成本后净收益 | 成本后净超额 | 5 日主合约独立日 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 多层残差 | 4 | -0.0313 | +0.6484% | -0.4450% | +0.2750% | -1.1751% | 0 |
| 多层残差 + 量能生命周期 | 4 | -0.0313 | +0.6484% | -0.4450% | +0.2750% | -1.1751% | 0 |

多层残差候选的次日 Rank IC 95% 区间为 `[-0.202, +0.117]`；量能生命周期候选为 `[-0.179, +0.117]`，都跨过 0。两者点估计完全一致不是“量能有效”，而是本批可比样本全部为盘中模式，v5.4 按预注册规则把缺少同一时刻量能证据的生命周期增量置为 0。

机器晋级门禁明确拒绝两个候选：5 日 IC、Top100 成本后净超额、单调性和回撤均没有独立日期；旧证据未达到 v2 全输入摘要校验标准；最大板块/行业/流动性份额偏差为 `40.71%`，超过 `20%` 门槛。Top100 迟滞换手 `24%` 和评分行覆盖率 `100%` 虽通过各自门槛，但不能抵消其他失败项。结论仍是：生产 v4 不变，v5.4 继续 Shadow；同时比较多个候选时，还必须积累至少 40 个独立日期后再做 PBO/多重检验评审。

同一次生产复跑中，全市场可比合约的次日 Rank IC 为 `-0.1380`（4 日，95% 区间 `[-0.321, +0.045]`），3 日 Rank IC 为 `-0.1812`（2 日），5 日仍无样本。v5.4 的次日点估计看似较少为负，但样本量和时点证据都不允许据此宣称优于生产。

## 历史 v5 结果（只能用于提出假设）

以下 official 候选结果来自 3 个独立扫描日，全部为 `insufficient_data`：

| 候选 | Top N | 次日毛收益 | 市场超额 | T+1情景净收益 |
| --- | ---: | ---: | ---: | ---: |
| Shadow v5 完整 | 20 | +0.07% | -0.69% | +0.02% |
| Shadow v5 完整 | 50 | +0.29% | -0.48% | +0.18% |
| Shadow v5 完整 | 100 | +0.24% | -0.52% | +0.19% |
| 去掉过热扣分 | 50 | -0.10% | -0.87% | -0.10% |
| 去掉风险扣分 | 50 | -1.81% | -2.57% | -1.74% |
| 去掉流动性扣分 | 50 | +0.19% | -0.58% | +0.14% |

完整候选的 official 次日平均 Rank IC 为 `-0.101`（3日），intraday 为 `+0.377`（1日）；符号不稳定且日期太少。消融结果可以提出“风险扣分和过热扣分值得继续观察”的假设，但绝不能据此确认因子有效。

当前生产 v4 只有 1 个可评估的 intraday 日期：Top 20/50/100 次日收益分别约为 `-0.67% / +0.09% / +0.47%`。新版评估器因此全部返回 `insufficient_data`，不再因为Top 50含50只股票就判定样本充足。

后续代码审查先将候选升级为 v5.1，修正60日参照、冻结价格涨跌幅、冲突K线和实际上市日规则；本次再升级为 `full-market-shadow-score-v5.2`，加入K线重算量比、连续层级归一化和严格批次回放。回放现在会从输入重算趋势、量能和全部风险扣分，并验证注册规范、候选标识、板块上下文、归一化分值/摘要、扣分边界、总扣分、整数分和确定性排名，防止局部或自洽式静默损坏被当成有效证据。本文表格及配套 JSON 仍是升级前 v5 的历史结果，不得与 v5.1/v5.2 混合；v5.2 必须从零积累独立扫描日和可验证的时点输入，生产 v4 权重仍未自动变更。

## 晋级门槛和仍需数据

- 当前 v5.4 次日前向合约最多 4 个盘中独立日期，但预注册主指标是 5 日且当前为 0；必须从新证据 v2 批次重新积累至少 20 个具有完整 5 日前向窗口的独立日期，不能用次日股票行数折算。
- 10/20日窗口还必须等待每个扫描日之后出现足够的完整交易日，不能用更短窗口替代。
- 需要覆盖强势、弱势和震荡环境，并分别检查沪深北、板块、ST/新股和流动性偏置。
- 晋级评审由 `full-market-shadow-promotion-gate-v1` 逐项机器检查：覆盖率、5日Rank IC、Top100成本后净超额、分位单调性、回撤、迟滞换手和板块/行业/流动性暴露必须同时通过；任一切片严重依赖单一日期或单一板块都不得晋级。
- 即使达到门槛，也只允许进入人工评审，不允许工具自动替换生产评分。
- 旧批次的覆盖式K线缓存或 v1 证据没有完整摘要校验报价/元数据输入，因此仍固定阻断晋级；只有新证据 v2 对评分所用报价、状态、元数据和61根日K全部摘要校验通过，才计入可晋级覆盖率。

## 可复现命令

```bash
.venv/bin/python3 tools/evaluate_market_scan.py \
  --database data/ashare_radar.sqlite3 \
  --output docs/research/FULL_MARKET_SELECTION_EVALUATION_2026.json

.venv/bin/python3 tools/evaluate_market_scan_shadow.py \
  --database data/ashare_radar.sqlite3 \
  --variant v5_4_skip5_multilevel_residual \
  --variant v5_4_skip5_multilevel_residual_volume_lifecycle \
  --output docs/research/FULL_MARKET_SELECTION_SHADOW_V5_2026.json

.venv/bin/python3 tools/evaluate_market_scan_probability.py \
  --database data/ashare_radar.sqlite3 \
  --output-dir data/market-scan-probability \
  --report data/market-scan-probability-summary.json

.venv/bin/python3 tools/evaluate_market_scan_future_range.py \
  --database data/ashare_radar.sqlite3 \
  --output-dir data/research/market_scan_future_range \
  --report data/research/market-scan-future-range-summary.json
```

四个命令都以 SQLite `mode=ro` 和 `PRAGMA query_only=ON` 打开数据库。前两个机器可读报告分别为 `market-scan-forward-evaluation-v2` 和 `market-scan-shadow-comparison-v2`；第三个把完整概率研究记录写入数据库外的内容寻址 artifact，并在 stdout 与可选原子写入的 `--report` 中返回相同的 `market-scan-probability-evaluation-summary-v1`。该摘要的 `full_input_replay_verified` 只代表本次完整 artifact-set 确定性复算通过，不是生产晋级授权。第四个为每个目标 run 写入 `market-scan-future-range-artifact-v1`，stdout/`--report` 使用 `market-scan-future-range-evaluation-summary-v1`；其 `offline_replay_verified` 只证明 artifact 的语义重放一致，`database_bytes_unchanged` 只在无并发写的静态来源上证明字节未变，两者都不代表策略有效。
