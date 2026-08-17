# 全市场横截面评分前沿研究与优化方案（2026-08-12）

> 2026-08-13 信任边界更新：本报告中的数值与 run 71/77 研究结论保持历史原貌；当前运行时合同已升级为 `market-scan-snapshot-digest-v2`、`snapshot_seal_origin=publication|legacy_backfill` 和 PIT evidence v4。`legacy_backfill` 只证明迁移时图一致，不能授权 Discovery、delta/筛选提醒、策略执行/证据/自动化、概率、未来区间、degraded retry-copy 或 TOP100 refresh。动作源还必须是精确全市场范围，并在 diagnostics v1 中无 blocker、仅有一个 info `score_distribution.pass` 且无评分分布 warning/其他 passed 冲突；浏览审计仍可见。run #80 是可审计但不可授权的 concrete legacy backfill。托管 research artifact 发布与 retention 通过同一跨进程租约串行化，不能用一个通过摘要但动作门禁失败的 run 生成新托管证据。

## 结论

AShareRadar 不应把“更深的模型”直接替换为生产评分。当前生产合同
`full-market-score-v4` 必须保持不可变；新增方法先作为版本化 Shadow challenger，
只有在独立交易日、点时完整性、成本后收益、Rank IC、分位单调性、换手、容量、
暴露和多重检验同时通过后，才允许进入人工晋级评审。

本轮最值得实现的改进不是继续堆叠传统技术指标，而是：

1. 保留稳定、可解释的线性/排序直通路径，只加入幅度受限且可消融的交互；
2. 将 Alpha、风险、交易成本与容量分开记录，再合成净可执行分；
3. 用经过市场、板块、质量合格行业和流动性残差化后的全市场相对强度及市场状态验证；行业宽度和同业网络留待取得可靠 PIT 数据后研究，不能由当前标签推断；
4. 用时间分组样本外结果、成本压力、换手和多重检验评估候选，不用单次最好回测选模型；
5. 证据不足时返回 `insufficient_data` / `null`，不把序数分映射成上涨概率。

## 一手研究证据与采用边界

| 研究 | 一手结论 | 本项目采用 | 明确不采用 |
|---|---|---|---|
| [Machine Learning and the Implementable Efficient Frontier, RFS 2026](https://academic.oup.com/rfs/advance-article/doi/10.1093/rfs/hhag022/8524346) | 预测目标若忽略换手、冲击成本和 Alpha 衰减，毛收益高的信号可能在成本后为负；经济特征重要性应按成本后效用衡量。 | 多资金规模成本/容量情景、净收益门禁、持仓滞回、Alpha 与成本分层。 | 不直接复制机构月频组合优化器，也不把论文参数当作 A 股生产参数。 |
| [Confident Risk Premiums and Investments Using ML Uncertainties, RFS 2026](https://academic.oup.com/rfs/article-abstract/39/5/1463/8287227) | 预测精度的不确定性可用于过滤低置信预测。 | 将证据覆盖、跨折稳定性和区间宽度作为独立门禁；未来数据成熟后再研究保守分。 | 不把任意模型方差冒充覆盖率经过验证的置信区间。 |
| [Getting the Target Right in Return Prediction, 2026](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615698) | 横截面标准化或 rank 目标可能比原始收益目标更适合排序；目标设计本身影响很大。 | 每个交易日独立的 rank/z-score 与净超额目标；按 D+1/D+2/D+3 分开验证。 | 不认为 rank 永远优于幅度目标，也不把 rank 解释为概率。 |
| [Design choices, machine learning, and the cross-section of stock returns, 2026](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5031755) | 模型设计选择产生的非标准误差可能大于普通估计误差。 | 预注册变体、逐项消融、跨设计稳健性与多重检验。 | 不用“最佳一次配置”作为晋级证据。 |
| [Empirical Asset Pricing with Probability Forecasts, AFA 2025](https://www.aeaweb.org/conference/2025/program/paper/53Yz87YF) | 概率预测与收益预测提供互补信息。 | 保持收益排序与上涨概率两条独立合同；概率只由独立校准窗生成。 | 不把趋势强度转换成百分比。 |
| [Machine learning in the Chinese stock market, JFE](https://doi.org/10.1016/j.jfineco.2021.08.017) | A 股横截面中流动性的信息含量突出，且小盘/大盘、国企等分层不同。 | 流动性同时以独立 Alpha 研究特征和成本/容量门禁出现，二者语义分离。 | 不将低流动性简单解释为高预期收益。 |
| [Empirical Asset Pricing via Machine Learning, RFS](https://academic.oup.com/rfs/article/33/5/2223/5758276) | 非线性交互有价值，但较浅、受约束的方法通常比盲目加深网络稳健。 | 稳定直通路径、浅层有界交互、线性基线与 challenger 消融。 | 当前样本量下不上深度网络。 |
| [Charting by Machines, JFE 2024](https://doi.org/10.1016/j.jfineco.2024.103791) | 多尺度价格路径可包含非线性横截面信息。 | 只使用截止 `data_date` 的多尺度路径、跳过近端动量、振幅/缺口/位置和量价关系。 | 不把技术图形解释为因果。 |
| [Forest through the Trees, Journal of Finance 2025](https://onlinelibrary.wiley.com/doi/10.1111/jofi.13477) | 交互可被压缩为少量、可解释的条件组合。 | 有界交互和人可读条件证据；未来可作为规则发现 challenger。 | 树叶历史收益不直接变成生产权重。 |
| [Dual peer effects and cross-stock predictability, JFE 2026](https://doi.org/10.1016/j.jfineco.2026.104274) | 同业强度和个股在同业中的相对位置可能提供增量信息。 | 仅列为 P1 数据路线：取得可审计的 PIT 行业宽度或经济网络后再建独立 challenger。 | 当前 v5.5 不计算行业宽度、同业网络或持仓网络，也不把粗行业标签冒充论文的经济网络。 |
| [RankGLU, 2026 preprint](https://arxiv.org/abs/2606.08930) | 线性直通路径叠加有界门控交互，比无约束扩展更稳定。 | 仅采用“稳定直通 + 有界交互 + 消融”的结构思想。 | 预印本结果不直接迁移，也不上未验证神经网络。 |
| [PRISM-VQ, IJCAI 2026](https://arxiv.org/abs/2605.13407) | 金融先验、离散潜在状态和专家路由可适应市场状态。 | 仅保留未来研究路线：可解释状态分层。 | 当前 PIT 面板不足，不实现 VQ/MoE。 |
| [Replicating Anomalies, RFS](https://academic.oup.com/rfs/article/33/5/2019/5236964) | 控制微盘和多重检验后，大量异常不能复现。 | 市场/流动性分层、容量权重、FDR、样本外门禁。 | 不只报告等权微盘收益。 |
| [A Taxonomy of Anomalies and Their Trading Costs, RFS](https://academic.oup.com/rfs/article/29/1/104/1843689) | 进入/保留双阈值能有效降低高换手策略成本。 | Top N 滞回、最小持有逻辑和换手预算作为研究门禁。 | 不以毛收益覆盖交易摩擦。 |

### 机构规则与 A 股本地化

- [MSCI A 股因子研究（2025）](https://www.msci.com/research-and-insights/paper/are-you-really-capturing-the-right-factors-unlocking-deeper-insights-in-china-a-share-factor-investing)说明，A 股因子层级不能直接套用全球权重，非目标规模、流动性和估值暴露必须单独归因。
- [MSCI China Equity Factor Model](https://www.msci.com/downloads/web/msci-com/data-and-analytics/factor-investing/equity-factor-models/China%20Equity%20Factor%20Model-cfs-en.pdf)把 PIT 基本面、动态行业、拥挤度、同类公司风险和多期限风险模型列为中国股票模型的重要组成。
- [MSCI Core Multiple-Factor 研究](https://www.msci.com/downloads/web/msci-com/research-and-insights/paper/efficient-multi-factor-indexing-and-concentrated-markets-introducing-the-msci-core-multiple-factor-indexes/Core%20Multi%20Factor%20Index_Research%20Paper.pdf)采用证券级多因子目标及跟踪误差、特异风险、Beta、行业、个股和换手约束。其数值只可作为 Shadow 初值。
- [FTSE Global Factor Index Series v8.8（2026-07）](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/ground-rules/ftse-global-factor-index-series-ground-rules.pdf)明确使用行业边界、capacity ratio、个股权重与换手控制。
- [MSCI 拥挤度研究](https://www.msci.com/research-and-insights/blog-post/crowd-control-momentum-and-concentrated-markets)与[容量研究](https://www.msci.com/research-and-insights/paper/a-practical-approach-for-analyzing-fund-capacity)支持把估值/动量极端、异常换手、残差波动、ADV 参与率和 days-to-liquidate 作为独立风险证据。
- [中证智选价值稳健规则](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/931586_Index_Methodology_cn.pdf)与 [S&P A 股质量指数](https://www.spglobal.com/spdji/en/indices/dividends-factors/sp-china-a-share-quality-index/)说明，质量、价值、波动应使用多描述子而非单一指标；当前数据库没有完整 PIT 基本面，不能伪造这些因子。
- [中信证券 2026 量化数据需求](https://www.citics.com/newsite/xxgs/cgxmjg/202602/t20260210_1208117.html)列出的财报、预告、分析师、持仓网络、专利和研报情绪，属于后续数据路线；取得可审计 PIT 来源前不得进入当前评分。

## 当前合同审计

### 生产 `full-market-score-v4`

生产分主要来自趋势组件和数据质量扣分，再用极小的精排折扣打破同分。它具备严格行情/日 K/日期门禁和确定性重放，但不做横截面中性化，也不把 ST、容量和可交易性直接纳入生产排名。

本轮正确性审计不改变公式或历史排名，但冻结了此前分散的边界：唯一最小历史为
61 根完整日 K；`official`/`preopen` 的 1/5/20/60 日及 skip-return 参考位移为
`horizon+1`，`intraday` 为 `horizon`。盘中没有同进度量能时，生产趋势确认和
Shadow 量能增量都为 0，并保留原因。基础分为 leadership 减质量扣分，整数分为
基础分取整，final/raw 为基础分减小于 0.05 的有界精排；它们都是趋势状态序数分，
不是上涨概率。

发布门禁 `score-layer-distribution-v4` 要求 base/integer/final 与 leader/trend/quality/
refinement 组件证据全覆盖，校验 leader/trend 的精确别名关系，同时记录各层 entropy
bits、normalized entropy、effective distinct count、effective precision、总体方差及组件
零方差。raw-only 证据失败；常量基础分直接阻断；低熵/高并列的基础层即使被六位小数
精排“打散”也会降级。PIT evidence v4 还把结果身份、分数字段、来源/日期/元数据、
61 根真实日 K 及每根 K 线的 aware `as_of` 绑定到 `market-scan-session-coverage-v1`，并
强制 `quote event <= quote_observed_at <= decision`、bar date midnight `<= bar.as_of <= decision`
和 bar observation 非递减；可信交易所日历中的缺口只记录、
扣置信度并关闭 action eligibility，绝不补造 K 线。真实只读诊断为：

| 批次 | base distinct / N | base Hn | base 有效档 / 精度 | final distinct / N | final Hn | final 有效档 / 精度 | 组件方差（leader/trend/quality/refinement） |
|---|---:|---:|---:|---:|---:|---:|---|
| 71 | 84 / 5,499 | 0.465598 | 55.1401 / 1d | 5,480 / 5,499 | 0.999444 | 5,472.7234 / 6d | 220.968993 / 220.968993 / 0.052354 / 0.018566 |
| 77 | 79 / 5,494 | 0.453215 | 49.5420 / 0d | 5,475 / 5,494 | 0.999443 | 5,467.7234 / 6d | 225.941487 / 225.941487 / 0 / 0.017092 |

两批 Top100 的基础分并列率都是 100%，因此当前门禁会把它们识别为“小数抖动伪
离散”并降级，而不是把 final 的近乎唯一值误读为真实信息增量。compact inner
score contract 也只有在全部 success row 的生产规则/摘要完整且唯一时才返回；该
合同用于概率证据双绑定，不改变 v4 排名。

### Shadow v5.4

现有 v5.4 已覆盖：跳过近 5 日的中期动量、市场→板块→质量门禁行业→流动性分层残差化、全局 midrank、过热/风险/置信/ST/新股/容量/涨跌停/换手扣分、同完成交易日量价生命周期、完整 spec/hash/输入/归一化/排序重放，以及成本后收益、Rank IC、分位单调性、暴露和滞回换手评估。

因此本轮不能重复实现这些能力。新增候选只补以下增量：

1. 稳定直通 Alpha 上的有界交互，而不是无约束非线性模型；
2. 将既有多层残差化后的全市场分位明确记录为 `cross_sectional_residual_strength`；它不是行业宽度或同业网络；
3. 拥挤/容量/信号持久性的显式证据与消融；
4. 多候选真实多重检验和设计稳健性结论；
5. 只读产品面，明确 Shadow 与生产排名隔离。

### 可执行候选 Shadow 产品边界

`GET /api/strategy-lab/executable-candidate-shadow` 把“可执行性”做成独立只读投影，
而不是另一套生产榜。它只接受用户明确提交、摘要通过且原发布封印的 `official` 全市场 run_id 和
名义资金；打开全市场或 Strategy Lab、填入当前批次都不调用重接口。返回固定为
`research_shadow/not_generated`、`production_effect=none`，展示原生产 rank 与
Shadow 顺序、筛选/风险/成本/容量代理、行业/板块暴露和预计换手。

当前明确限制包括：历史 ADV 不可用，容量只用冻结当日成交额参与率；停牌和一字
状态只用日线成交额/单一价格/原因文本代理；行业分类粒度混合；成本未覆盖实时价差、
冲击与订单簿深度；研究顺序不是已验证 Alpha。run 77 在一台机器上的显式按需实测
约 9.2 秒，因此交互必须保持 lazy、可取消和 stale-safe；该时间不是 SLA。合同及
前端都 strict exact-key/fail-closed，null 显示为不可用而不是 0，局部失败不能移除
生产榜或其他 Strategy Lab 内容。

## 冻结优化合同

### Shadow v5.5 已冻结合同

- variant：`v5_5_bounded_nonlinear_stability`
- candidate：`full-market-shadow-score-v5.5`
- schema：`6`
- algorithm：`bounded-gated-residual-stability-v1`
- 作用域：只读 Shadow challenger；生产 `full-market-score-v4` 完全不变，自动晋级始终为 `false`。

v5.5 以 `v5_4_skip5_multilevel_residual` 的无量能基线作为稳定直通路径，
只加入 `[-6,+6]` 的确定性有界交互：

```text
residual_strength = clamp((normalized_alpha - 50) / 45, -1, 1)
stability_gate = 0.60 * coherence(skip5_20, skip5_55, ma20_slope)
               + 0.40 * abs(mean(skip5_20, skip5_55, ma20_slope))
crowding_risk = 0.45 * turnover_heat
              + 0.35 * completed_volume_surge
              + 0.20 * range_crowding
capacity_risk = unit(100000 / signal_day_amount, 0.2%, 1.0%)
implementability_gate = clamp(1 - 0.55 * crowding_risk - 0.45 * capacity_risk, 0, 1)
bounded_delta = clamp(
  6 * tanh(1.5 * residual_strength)
    * (0.35 + 0.65 * stability_gate)
    * quality_gate
    * (implementability_gate if residual_strength > 0 else 1),
  -6, 6
)
score = clamp(v5.4_direct_alpha + bounded_delta - enabled_penalties, 0, 100)
```

负残差不使用可实施性门控，是预注册的保守下行惩罚；正残差才会因拥挤和容量
缩小增量。每条结果保存公式版本、输入摘要、稳定性、拥挤、容量、质量门控、
限制说明和有界增量，并与批次归一化、spec hash、组件及排名一起确定性重放。
缺字段、非有限值、篡改证据或不一致 digest 均 fail closed。该规则受论文结构启发，
但没有训练模型，不宣称 ML、行业宽度或 peer network。

### 分层语义

```text
stable multilevel-residual cross-sectional alpha
+ bounded residual / stability interaction contribution
- overextension and fragility
- confidence / PIT coverage penalty
- tradability, cost and capacity penalty
= net executable ordinal score (not probability)
```

- 原始因子、残差化因子、交互、扣分和净分分别持久化。
- 缺失行业、无效分组、日 K 不足、非有限输入均给出原因；不得填 0 假装中性。
- 候选只在完整、已发布、同一 mode/scope/rule cohort 中比较。
- 评分仍为序数；上涨概率继续由独立标签、校准和 OOS 门禁生成。

### 晋级门禁

任何候选都必须满足：

1. 完整 PIT digest 与确定性重放；
2. 足够独立交易日及覆盖率；
3. D+1/2/3/5/20 时间分组样本外 Rank IC/ICIR 与分位单调性；晋级主统计量固定为候选相对生产的 session 配对 5 日 Top100 净超额；
4. Top20/50/100 成本后净超额为正，并报告 base/conservative/stress 成本及 10/50/100 万元容量；
5. 最大回撤、行业/板块/流动性暴露、ST/新股/涨跌停与拥挤约束；
6. 滞回后的换手门槛；
7. 5 日重叠结果使用 5-session circular moving-block 单侧 bootstrap；候选族以及同 contract/horizon 的因子族分别执行 BH-FDR；不足时 p 值和拒绝结论保持 `null`；
8. 多数时间折和关键 A 股分层不退化；
9. 自动晋级始终关闭，全部通过也仅允许人工评审。

当前评价并未执行组合切分与选择路径枚举，也没有与主统计量相匹配的收益比率
分布，因此 PBO 与 DSR 必须分别显示 `not_computed`，不得把“达到可计算样本门槛”
或旧 readiness 文案写成已得到 PBO/DSR 结论。

## 第二轮可信化落地与残余缺口

本轮没有改写 `full-market-score-v4` 公式或 run 71/77 的生产排名，而是关闭消费者侧
的可信度绕过：`market-scan-snapshot-digest-v2` 覆盖全部公开持久化 run/result 字段（包括
完整 public metrics/score details），校验发布/子行审计时间，并由五个 SQLite trigger 禁止
sealed run update/delete 和 child result update/delete/insert。共享 frozen-snapshot 校验重建 full-market 批次的 header/result 计数、
唯一 symbol、连续 success rank、非 success 无 rank、完整 score 字段和唯一生产规则/
摘要；查询在概率/未来区间 enrich 前后双读 cohort 并校验最终 page，避免 TOCTOU 混批。
snapshot seal 异常与概率/future-range/snapshot-model/strategy evidence/automation 异常分别返回
两个不泄露内部差异的 generic `409` 类别；全 `/api/market-scans`、`/api/discovery`、
`/api/strategy-lab` 成功/失败路径均 `no-store`，但该 header 本身不是完整性证明。

Strategy Lab 的 `latest_scan` 现在按可信交易所 session 判定新鲜度，日历不可用、未来数据
或超过策略 session 上限都在写入前拒绝；历史 replay 保持原决策时点。非 custom 组合在
约束剔除后按确定性候选池迭代补位并重算权重，显式给出 replacement attempts、pool
exhausted 和 underinvested reason；custom 不得补入未请求 symbol。可执行 Shadow 升为 v2。
证据中心校验 pinned compact-v2、严格 JSON/身份/摘要与重建 execution result，最新坏行不
回退；simulation plan 同时绑定 source execution digest/identity，tamper/reseal 均失败。

这些工程门禁提升了“不能把坏证据当好证据”的保证，却没有补出新的有效 Alpha 或概率。
特别是现有 qfq 与 signal-D amount proxy 不能替代未来 entry/exit 的未复权官方成交证据，
两日 official PIT 也不能支撑全决策 joint label、regime、FDR、deployment 或策略绩效结论。
因此生产排序仍是序数分，v5.5 仍 remain-shadow，当前概率仍 null。

## 分阶段路线

- **P0（本轮）**：只用现有冻结 OHLCV/行业/市场/可交易证据实现 v5.5 Shadow、真实 BH-FDR 框架、只读研究展示和成本/容量验证；生产 v4 不变。当前不实现行业宽度或同业网络。
- **P1（数据成熟后）**：加入完整 PIT 市值、行业+log-cap+流动性联合中性化、可靠行业宽度/同业网络、短反转执行 overlay、分析师修正、拥挤和容量曲线；Ridge/ElasticNet 与浅 GBRT 做严格 walk-forward horse race。
- **P2（新增可靠数据后）**：PIT 质量/价值/股息/预期修正、SOE/政策、持仓网络和另类数据；RankGLU/PRISM-VQ/Transformer 只能作为独立 Shadow challenger。

## 当前证据边界

上涨概率是独立合同，不得从上述序数分推导。当前为 core v4 / model v2 / feature v3 /
label v3 / split v3 / `market-scan-probability-result-v4-explicit-intervals`。H1/H5/H20 指
持有 1/5/20 日，固定 D+1 开盘进入、D+2/D+6/D+21 收盘退出；标签、purge gap 和
bootstrap block 都使用 `H+1` target offset。正式日期门槛为 224/232/262。source
artifact-v2/snapshot-v2 要求原发布封印、PIT evidence v4、record/source digest、
ALL/SH/SZ/BJ 精确计数与 population/eligibility/success coverage、market/board/industry/
liquidity/regime strata 覆盖，并把外层正式 scan cohort/rule hash 与内层全 success 唯一
生产 score rule/hash 双绑定，逐行重放 score details 及 outer score inputs。result v2/v3 等旧合同只可 replay/
read；legacy source v1 可原样审计 PIT evidence v1/v2/v3，但不会升级，永远不能
filter-qualified。

current result/API 将可为负的 `calibration_bias_interval` 与 `[0,1]` 的
`calibration_adjusted_probability_interval` 分开；通用 `confidence_interval` 只属于
result v2/v3 只读适配。三者都不是个股结果区间。筛选仅接受严格验证器返回的 opaque
token：artifact v1 / payload `market-scan-probability-filter-authorization-v3-raw-drift-
joint-execution` 重算 raw OOS predictions、proper-score intervals/ECE、候选族 BH-FDR、
时序漂移和原始 session economics；普通 mapping、自报汇总或 reseal 不能授权。

当前 v4 标签是 executable-only conditional population，不能证明包括未成交和不可退出
决策的 all-decisions joint estimand。`market-scan-probability-deployment-refit-v1` 的独立
refit/purge/replay/freshness 骨架虽已实现，但当前没有 verified deployment artifact，且
joint gate 在拟合前即关闭。因此 authorization、filter、deployment 和新 prediction 全部
fail-closed，正式 run/逐股值保持 typed null，不能写成“已经部署”。

`decision-time-joint-execution-probability-v2` 只是未来成熟标签/OOS prediction 的逐样本
证据骨架，不是信号 D 输入或实时 per-symbol action evidence。它把目标拆为 entry fill、
exit executable given entry、net positive given both 三组件，并要求 production sample_id、
entry offset=1、exit offset=H+1、退出日上海 15:00 后封存。v2 永久包含
`observed_joint_outcome_components_unavailable` 与
`strict_joint_assessment_replay_not_verified`，五个概率强制 null，action helper 恒 false。
未来新版至少需要未复权官方 entry/exit OHLCV+amount、有效日交易/ST/上市退市规则、公司
行动感知参考价、双边同 session capacity、signal-date 固定全市场 universe、LOO 或预声明
外部 benchmark、observed 三组件标签和 strict assessment replay。

真实正式证据目前只有两个交易日：run 71 为 5,499/5,542，run 77 为 5,494/5,543。
两者均为缺少生产 score hash 的 source v1，故都是 `legacy_unbound`、0 个
filter-qualified horizon，H1/H5/H20 全部 `probability=null`。store/source research 使用
non-blocking singleflight 与 atomic last-good snapshot，刷新时 warm reader 不读取半成品。
fresh-process preload 实测 8.158412 秒（此前 13.217 秒），100 次 warm projection 均值
0.000756715 秒；201,055,106-byte 的 run-62 legacy 文件首次交互 0.00087925 秒即 typed
unavailable，未做交互式 deep read。这些是单机回归预算，不是 SLA。

个股 D+2/D+3/D+4 概率又是另一个独立 Shadow 合同。当前 builder schema 为
`individual-upside-probability-assessment-v4-source-intake-bound`；v3 已 superseded。
除 estimator/target/split 外，v4 只加载原发布封印的完整 source-v2/feature-v3/PIT-v4 records，绑定
`quote event <= quote_observed_at <= decision <= captured_at <= generated_at`、bar-as-of、
15:15 成熟交易日、完整
ALL/SH/SZ/BJ coverage envelope、注册生产 score spec/hash 与逐行 score-details replay。
Assessment v1/v3 拒绝；v2 仅按 exact legacy shape 审计可读，run/date/digest-only source
不计 current 证据。compact v4 保留完整 intake identity，却没有 runtime source locator
或 records；runtime 因此仍不能独立重放或认证原 source，公开 projection 强制 count 0、
signal null、selection false 和概率全 null，非零 current 声明增加
`official_pit_source_artifacts_not_runtime_replayed`。未来自报计数不能单独证明正式
PIT corpus。公开 calibrated 门禁另要求 overall 288、H1/H2/H3 284/286/288 日期、
至少两折且每折 60 OOS 日、最稀 calibration bin 20 日、全部折正 Brier skill 与
Brier identity、可信 signal/cutoff 交易日和完整 versions/digests。保留的 tracked v2
8,733-byte artifact 内容 digest 为
`517691b101dcb2142693a74f6e5ac9ef10f386c545572b6bacfe161f186ba677`。真实
current-contract official PIT 为 0/288 日、`signal_date=null`，279 日回放仍为
非官方；三个 horizon selection 全为 false，产品概率/区间全为 `null`，并披露
legacy-source/compact-metrics limitation，`production_effect=none`。

真实验证只读取 `tools/runtime_data.py` 生成并验证的静态备份。备份
`<VERIFIED_STATIC_BACKUP>/runtime.sqlite3` 的 SHA-256 为
`d4d005b9515e05abc54688642cd241b1066df054a9d55ac22af0e081aa3db546`，
大小 `2,129,387,520` 字节，前后完整性检查均为 `ok`。复现命令为：

```bash
PYTHONNOUSERSITE=1 .venv/bin/python tools/evaluate_market_scan_shadow.py \
  --database '<VERIFIED_STATIC_BACKUP>/runtime.sqlite3' \
  --run-id 71 \
  --mode official \
  --variant v5_4_skip5_multilevel_residual \
  --variant v5_4_skip5_multilevel_residual_volume_lifecycle \
  --variant v5_5_bounded_nonlinear_stability \
  --bootstrap-samples 1000 \
  --compact \
  --output docs/research/FULL_MARKET_SELECTION_SHADOW_V55_2026.json
```

该次执行约耗时 2 分 50 秒，生成于 `2026-08-12T09:07:04Z`。最终紧凑报告
大小 `231,983` 字节（约 227 KiB），SHA-256 为
`29884e99744a3001b156c338dff77d9e4f00d0bf455f7d1c1b2d7c8f2b0859ad`。
`--compact` 保留聚合、Top-N、排名差、稳健性、暴露和晋级证据，删除逐股巨型
records 与机器本地数据库路径；它不是重新计算或缩短统计过程。

冻结正式批次 #71 有 5,542 只股票，其中 5,499 只成功并通过完整 61-bar
点时证据重放；v5.5 覆盖率为 `1.0`，PIT 完整性为 verified。它相对生产排名
比较 5,499 只股票：平均 rank delta 为 `0`，中位数为 `176`，平均绝对差
`1793.364248`，最大绝对差 `5423`，Top20/50/100 overlap 均为 `0`。
这些只是一个横截面的描述性差异，不是收益提升。

当前只有 1 个独立信号日，5 日 forward/候选-生产配对 session 均为 `0`，因此
净超额、IC、单调性、bootstrap p 值和 BH-FDR 拒绝结论全部不可用。v5.5 的
100 万元 stress 容量覆盖仅 `0.33`，最大板块/行业/流动性暴露差为
`0.4155428259683579`，超过 `0.20` 门槛。PBO 与 DSR 均为 `not_computed`。
报告状态为 `insufficient_data`，没有 eligible candidate，所有候选结论均为
`remain-shadow`。不得用该批次宣称 v5.5 有效、可晋级或具有上涨概率含义。
