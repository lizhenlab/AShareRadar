# 个股 D+2 / D+3 / D+4 上涨概率前沿研究与优化方案（2026-08-12；可信度复核 2026-08-13）

## 结论

个股研究可以增加“第二、第三、第四个交易日上涨概率”，但它必须是一个独立的
Shadow 研究合同，不能由趋势分、诊断结论、情景权重或全市场 1/5/20 日概率换算。
本轮冻结并实现的输出是：在完成交易日 `D` 形成信号，按固定交易所日历于
`D+1` 取官方日 K `open` 作为不顺延的价格代理，分别在 `D+2`、`D+3`、
`D+4` 取日 K `close`，对应持有 1、2、3 个交易日的**声明成本后日 K 代理
净收益为正**概率。该 open 代理不证明、也不保证实际成交。三个期限各自拥有
标签、模型、校准、样本外证据和门禁；任一较短期限的结果都不能推出另一期限。

当前产品只能诚实显示研究状态，不能显示百分比：2026-08-13 第三轮复核把“有日期
和摘要的旧 source-v1”与“构建时完整通过当前 source-v2 intake 的 PIT”分开。当前
builder schema 是
`individual-upside-probability-assessment-v4-source-intake-bound`；v3 已 superseded
且拒绝读取。V4 只加载 source-artifact-v2 / snapshot-v2、estimator feature-v3、
PIT-evidence-v3 完整记录，并把时间、全市场覆盖、注册评分合同和逐行评分重放身份
贯穿 compact verifier。tracked v2 baseline 中 run 71/run 77 仍可审计，但不计 current
evidence；当前正式产品投影为 0 个独立信号日，而不是旧口径的 2 个。compact v4
仍未保留 runtime 可解析的 source locator/records，因此公开投影强制计数为 0、
`signal_date=null`、三个 horizon `insufficient_data` 且概率/区间全 null；非空 current
声明增加 `official_pit_source_artifacts_not_runtime_replayed`。这不否定 build-time
intake 的严格性，但不能把内容封印等同于运行时独立 source authenticity/replay。
H1/H2/H3 的两折注册切分门槛分别为 284/286/288 日；最长 H3 使用
`entry_offset=1`、`target_offset=gap=H+1=4`，即 120 训练 + 4 gap +
40 校准 + 4 gap + 2 × 60 测试 = 288 日。
单独构建的认证历史回放是 `official=false` 的非正式研究队列，不能填充当前正式
个股投影；其实际样本外选择门禁也未通过。因此报告和三个 horizon 都必须保持
`probability=null`，并显示 `insufficient_data` 或 `not_generated`、证据计数和
`gate_reasons`，并令 `signal_date=null`。这不是系统故障，而是对现有证据边界的
正确表达。

本轮实现不改变既有趋势评分、诊断、买卖点、建议、全市场排名或 1/5/20 日概率，
也不把本研究用于筛选、自动交易或建议强化。

## 2026-08-13 可信度复核与修复

第三轮审计继续构造可重放反例：未来/盘前 `generated_at`，quote 晚于 run as-of、
as-of 晚于 capture、含 offset 但上海本地日期错误，低覆盖或缺市场 envelope，伪造
已注册 score spec 但篡改外层 raw/trend/final 或内层 score details，跨日期复用 run ID，
以及用不一致的 counts/folds/calibration bins/Brier skill、非交易日 signal/cutoff 绕过
公开 calibrated 门禁。旧 v3 还只保存可自行重封的 source identity/version/digest，旧
v2 会把 `{data_date, run_id, integrity_digest}` 的 run 71/run 77 当作 2/288 current
PIT。另有 store 原地修改/同尺寸内容替换、非正式 history end 回填 `signal_date`、
Workbench 子对象/策略卡跨股票或跨 decision time、正式建议接受 15:00 后 quote、以及
typed unavailable 数字继续污染评分或 DOM 等独立反例。这些路径会夸大证据、混合
股票/时点或产生伪方向，属于必须关闭的 P1 可信度问题。

已修复边界与仍开放限制如下：

- current builder schema 是
  `individual-upside-probability-assessment-v4-source-intake-bound`；v1/v3 拒绝，显式
  构建时只有完整加载的 source-artifact-v2 / snapshot-v2、feature-v3、PIT-evidence-v3
  records 可进入 current source identity；
- 每条 source 必须满足 `quote_timestamp <= run.as_of <= captured_at <= generated_at`，
  这些时间均可绝对解析并映射到同一可信上海交易日；15:15 前 source 不成熟；
- source-v2 逐行重放 PIT context、注册 `full-market-score-v4` spec/hash、score details
  与外层 score/raw/trend/features；全市场 envelope 必须完整包含 SH/SZ/BJ 和可重建 ALL，
  人口下限为 ALL/SH/SZ/BJ 4,000/1,800/2,500/200，每层 eligible >= 90%、success
  coverage >= 95%，records 完整且个股 assessment intake 的 success/total >= 98%；
  run ID 不得跨日期复用，跨日 source/score contract 必须唯一；
- tracked v2 assessment 仍可按其精确 legacy shape 审计读取，但 legacy source
  只触发 `legacy_official_pit_sources_audit_only_not_current_evidence`，产品计数为 0；
- store 指纹绑定 device/inode/mode/size/mtime/ctime 和内容 SHA-256，读取前后要求稳定，
  同一最新 `generated_at` 的不同 artifact 拒绝，返回值使用深拷贝；
- 没有 runtime-replayed current-v4 official date 时 `signal_date=null`，不得回退到 non-official
  history end；
- compact horizon metrics 仍不能从 artifact 内部独立重放，必须展示
  `compact_horizon_metrics_not_independently_replayable`。在可重放 predictor、完整
  current PIT corpus 与独立 horizon 评估落地前，三项概率/区间保持 null。
- compact v4 持久化并严格复验完整 intake identity，但仍不持久化 source
  locator/records；runtime 不能仅凭内容 SHA 独立证明 source authenticity 或重放。
  当前公开投影明确将 runtime replay 置 false，强制 count 0、signal null、selection
  false 与概率全 null；非零 current 声明显示
  `official_pit_source_artifacts_not_runtime_replayed`。locator-bound manifest 与逐
  source replay 是下一轮 P0，不能把可重封的内部 `official_pit.session_count` 或
  `signal_date` 当成已认证样本量。
- 公开 `calibrated_shadow` 还需报告 288 official/replay dates；H1/H2/H3 分别
  284/286/288 independent dates，至少 2 folds 与每 fold 60 OOS dates，至少两个
  calibration bins 且最稀 bin 20 dates，全部 folds Brier skill > 0 并严格满足
  `1 - brier/reference_brier`，通过 bin 单调/最高 bin lift、完整 digests/versions、
  可信交易日 signal 和严格更早的可信 training cutoff；任一不符仍为 null。

同轮个股工作台旁路审计也已收紧：`stock-workbench-v2` 的全部 research children 与
每张 strategy card 都绑定请求股票和同一 decision/signal-date 的 `updated_at`，cache
与 direct child routes 不能绕过；interactive Shadow 永不落正式 advice。正式 advice
只有 15:15 后 active queue 可写，且 quote event 必须位于 14:55:00–15:00:00、至少
60 条有效完成日 K。量能、结构/chip、估值、盘口、fund flow、risk 与 calibration
不可用时不再保留方向评分或 DOM 数字；factor execution 必须通过连续唯一交易日、
PIT qfq、suspension/open-execution/corporate-action 元数据。`/api/analyze`、workbench
和 upside-probability 的全部成功/失败路径均 `no-store`，5xx/dependency/response-
validation 内容统一为非披露错误。

## 一手研究证据与采用边界

以下按“已发表顶级期刊 / 官方机构材料 / 最新工作论文”分层使用。工作论文只用于
提出待验证的 Shadow 假设，不把其结果或参数直接迁移到 A 股生产。

| 一手来源 | 主要证据 | 本项目采用 | 明确不采用 |
| --- | --- | --- | --- |
| [Empirical Asset Pricing with Probability Forecasts（AFA 2025；SSRN 2025 修订）](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4717935)；[AEA/AFA 官方会议页](https://www.aeaweb.org/conference/2025/program/paper/53Yz87YF) | 概率预测和收益预测包含互补信息，组合两者可能优于单独使用其中之一。 | 将“上涨概率”与现有趋势/收益序数完全分离；概率只来自独立二元标签、样本外预测和校准。 | 不把趋势强度或情景比例映射成概率；不把论文组合结果当作本项目已验证结果。 |
| [Confident Risk Premiums and Investments Using Machine Learning Uncertainties，RFS 2026](https://academic.oup.com/rfs/article-abstract/39/5/1463/8287227) | 预测精度存在显著横截面差异，使用预测区间过滤不精确预测可改善样本外策略。 | 每个 horizon 返回区间和证据门禁；区间过宽或样本不足时 withholding，而不是展示一个看似精确的点估计。 | 不把模型内标准差冒充经过样本外覆盖检验的置信区间。 |
| [Machine Learning and the Implementable Efficient Frontier，RFS 2026](https://academic.oup.com/rfs/advance-article/doi/10.1093/rfs/hhag022/8524346) | 忽略换手和交易成本的预测会偏向短暂、规模小的信号；策略应按成本后结果评估。 | 标签使用固定 D+1 日 K open 代理、固定退出和声明成本；D+2/3/4 分开建模，并明确容量与真实成交尚未被日 K 证明。 | 不把毛收盘方向或日 K 代理结果称为可执行胜率；不复制论文的机构组合优化器或参数。 |
| [Machine learning in the Chinese stock market，JFE 2022](https://www.sciencedirect.com/science/article/pii/S0304405X21003743) | A 股中流动性是重要预测维度，散户占比与短期可预测性有关；成本后样本外检验不可省略。 | 将流动性作为候选特征和未来成交能力研究维度，并明确区分其 Alpha、成本与容量语义；按市场/板块/流动性分层检查稳定性。 | 不把低流动性直接解释为高上涨概率，也不把当前缺少 amount/盘口的数据当作成交能力门禁已实现。 |
| [Empirical Asset Pricing via Machine Learning，RFS 2020](https://academic.oup.com/rfs/article/33/5/2223/5758276) | 非线性交互有增量，但低信噪比资产收益中，受约束的浅层模型与模型组合通常更稳健。 | 先冻结可解释 Logit/校准基线，再以浅层树或有界交互做独立 challenger 和消融。 | 当前 PIT 样本不足时不上深度网络，也不用训练内准确率选模型。 |
| [SMARTboost，Journal of Financial Econometrics 2025](https://academic.oup.com/jfec/article/23/3/nbae028/7901240) | 金融时间序列及同日 panel 存在时间与横截面相关，完全随机交叉验证会污染泛化评估；论文使用带清洗/间隔的时间块验证。 | 同一交易日全部股票保持同组，并在训练、校准、测试间设置 purge/embargo；SMARTboost 至多作为预注册浅层 challenger。 | 不按股票行随机切分，不把论文方法名或海外实验当成 A 股短期限概率已获验证。 |
| [Charting by Machines，JFE 2024](https://www.sciencedirect.com/science/article/pii/S0304405X2400014X) | 多尺度历史价格路径及其非线性交互可包含横截面预测信息。 | 候选特征保留多尺度收益、跳过近端动量、波动/下行波动、振幅、收盘位置、缺口和量价关系。 | 不把图形相关性解释为因果，也不宣称月频/海外结果已证明 A 股 D+2/3/4 有效。 |
| [MASTER，AAAI 2024](https://ojs.aaai.org/index.php/AAAI/article/view/27767)；[StockMixer，AAAI 2024](https://ojs.aaai.org/index.php/AAAI/article/view/28681) | 两项工作都利用股票间关系或跨股票混合改善收益/价格预测，是 cross-stock 表征的近期 challenger。 | 仅列入 P2：先取得有效日期正确的横截面与市场信息，再与冻结 Logit 做完全样本外、成本后对照。 | 排序分、收益点预测或价格预测不等于校准后的二元概率；不因 benchmark 排名直接进入 P0/P1 或产品显示。 |
| [Dual peer effects and cross-stock predictability，JFE 2026](https://www.sciencedirect.com/science/article/pii/S0304405X26000450) | 经济同业整体强度与个股在同业中的相对位置提供 firm-own 特征之外的信息。 | 作为 P1 Shadow 路线：取得可审计、有效日期正确的行业/供应链/经济网络后，分开检验 peer strength 和 within-peer position。 | 当前粗行业标签不能冒充经济网络；月度证据不能直接生成日级概率。 |
| [Mosaics of Predictability，NBER Working Paper 35158，2026](https://www.nber.org/papers/w35158) | 美国股票的可预测性具有资产与状态依赖，整体平均可能掩盖只在部分 regime 出现的信号。 | 用于预注册 predictability/regime gate 和分层退化诊断，要求各时期与主要 A 股层级证据可见。 | 不把美国股票月频结论或树节点直接外推到 A 股 D+2/3/4，也不在看到结果后挑选“有效状态”。 |
| [FinVerse，arXiv 2026](https://arxiv.org/abs/2608.03259) | 对 43 个公开通用时间序列基础模型的金融 benchmark 显示，通用预测指标/排名不必然转化为有用的金融预测。 | P2 若评估 TSFM，必须同时对照简单基线，并以本合同的概率校准、时间外稳定性和声明成本后决策指标验收。 | 不把通用 TSFM 排名、点预测精度或预训练规模解释成金融决策价值或上涨概率。 |
| [ConForME，PMLR 2024](https://proceedings.mlr.press/v230/galvao-lopes24a.html) | 为多期限预测区间提供联合覆盖方法，提醒逐期限区间与整条路径覆盖是不同问题。 | 仅作为 P2 多 horizon **联合覆盖诊断**；先审查时间相关下的交换性/校准集条件，并与逐 horizon 概率门禁分开报告。 | 联合收益路径覆盖不等于二元概率校准，也不能自动生成当前股票的概率或概率置信区间。 |
| [Getting the Target Right in Return Prediction，2026 工作论文](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615698) | 目标变量的定义和标准化方式可能比模型复杂度更重要；rank 目标会丢失收益幅度。 | 预注册绝对成本后正收益、市场相对正收益、rank 三类目标的 challenger；当前 UI 只展示冻结的绝对正收益合同。 | 不把 rank/z-score 解释为概率，不在看到结果后切换主目标。 |
| [Design choices, machine learning, and the cross-section of stock returns，2026 修订工作论文](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5031755) | 大量模型设计选择会产生显著“非标准误差”，单个最佳配置很可能夸大有效性。 | 预注册 target × feature family × model × calibration 设计矩阵，报告全体结果、消融和多重检验。 | 不用一次最佳回测、最佳随机种子或反复调参后的命中率作为晋级证据。 |
| [MSCI China Equity Factor Model](https://www.msci.com/downloads/web/msci-com/data-and-analytics/factor-investing/equity-factor-models/China%20Equity%20Factor%20Model-cfs-en.pdf) | 中国股票模型强调 PIT 基本面、动态行业、状态所有权、拥挤、peer similarity、非线性和多期限，并提供日级交易风险模型。 | 将 PIT 基本面、动态行业、拥挤、同业和多市场状态列入有数据血缘后的 P1/P2 路线；短期限分别建模。 | 没有 PIT 数据时不以当前财报、当前行业或当前相似度回填历史。 |
| [MSCI A 股因子研究（2025-12-09）](https://www.msci.com/research-and-insights/paper/are-you-really-capturing-the-right-factors-unlocking-deeper-insights-in-china-a-share-factor-investing) | A 股本地因子层级及规模、流动性、估值等非目标暴露需要单独识别。 | 样本外证据按 SH/SZ/BJ、板块、流动性和规模代理分层，报告退化而不是只看总体指标。 | 不把全球因子权重直接作为 A 股 D+2/3/4 权重。 |
| [中信证券 2026 量化数据采购需求](https://www.citics.com/newsite/xxgs/cgxmjg/202602/t20260210_1208117.html) | 机构级量化研究要求长期 PIT 行情、财务、预期、高频、情绪、研报和关系网络，并保留数据血缘。 | 作为数据路线图和采购验收清单：有效日期、发布时间、修订版本、来源、覆盖率和缺失原因必须可审计。 | 采购需求不是有效性证据；当前数据库没有的字段不生成、不推断。 |
| [上海证券交易所交易规则（2026 年修订）](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml) | 现行交易制度包含价格涨跌幅限制、停牌及异常交易等本地约束。 | V1 固定交易日且不顺延；缺失、零成交和版本冲突的固定日证据 fail closed。真实停牌状态、锁定涨跌停排队、订单优先级与退出可交易性列为 P1 数据/模型门禁。 | 日 K 的开/高/低/收不能证明盘中成交顺序、排队成交或实际 fill。 |
| [深圳证券交易所交易规则（2026 年修订）](https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html) | 深市现行规则同样要求按市场、证券类型和有效日期处理价格限制、停复牌与申报成交约束。 | 与上交所规则分别版本化；V1 只把交易日和日 K 缺失/零成交作为可证明门禁，逐笔状态与排队能力留待 P1。 | 不声称 D+1 日 K `open` 已剔除锁板、排队或不可成交样本，也不把深沪规则粗暴合并成一条常量。 |
| [《中华人民共和国印花税法》](https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193058/content.html)；[财政部、税务总局 2023 年第 39 号公告](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html) | 法律规定证券交易按成交金额、向出让方征税；2023-08-28 起证券交易印花税减半征收。 | 冻结成本 profile 绑定卖出方向、2023-08-28 生效日、政策来源和版本；本轮 2025–2026 标签均在该生效日之后。 | 不把当前 profile 永久套到跨政策日期或未来样本；跨界时必须新增有效期版本，也不把税费完整性等同于佣金、滑点、冲击或真实成交已建模。 |

## 冻结产品与统计合同

### 1. API 形状

独立只读接口：

```text
GET /api/stock/upside-probability?symbol=600519
```

它返回：

```text
IndividualUpsideProbabilityReport
├── symbol
├── signal_date
├── status
├── generated_at
├── target_contract
├── horizons[3]
│   ├── display_day             # 2 / 3 / 4
│   ├── holding_sessions        # 1 / 2 / 3
│   ├── status
│   ├── probability             # 门禁不通过时必须为 null
│   ├── confidence_interval     # 无有效区间时为 null
│   └── gate_reasons
├── evidence
├── limitations
└── production_effect          # 固定为 none
```

该接口与 `/api/stock/workbench` 分离，避免概率证据读取延长或污染核心个股分析；
浏览器可在当前 symbol/load sequence 下独立加载，并拒绝旧股票的迟到响应。

### 2. 标签语义

对每个经过认证的完成交易日 `D`：

```text
signal:      D 收盘后、只使用 effective_at <= D close 的冻结特征
entry proxy: 固定交易所日历 D+1 官方日 K open，不顺延
display D+2: D+2 close，holding_sessions=1
display D+3: D+3 close，holding_sessions=2
display D+4: D+4 close，holding_sessions=3
target:      声明 round-trip costs 后 daily-bar proxy net return > 0
```

`display_day` 是面向用户的“第几个交易日”，`holding_sessions` 是从 D+1 开盘到
固定退出收盘之间的日历持有长度。二者不可混写。若固定退出日未成熟，D+1 或
退出日缺 K 线/零成交，或 adjustment/data/contract 版本不一致，该样本不可用；
不能换到下一可用日期。V1 日 K 标签只计算声明成本后的开盘/收盘**价格代理**，
不能证明实际买入或卖出成交。真实停牌、锁定涨跌停排队、订单簿优先级、冲击和
退出可交易性尚未建模，必须保留在 limitation，并作为 P1 数据与门禁工作。

API 的冻结字面量是
`entry=D_plus_1_official_daily_open_proxy_no_shift` 与
`target=round_trip_net_return_after_declared_costs_gt_0_daily_bar_proxy`；客户端必须
精确校验，不能把它们改写成“首次可执行开盘”或“保证成交”。

### 3. 状态与显示门禁

只有同时满足以下条件时，一个 horizon 才能显示非空百分比：

1. report `status=calibrated_shadow`；
2. horizon `status=calibrated_shadow`；
3. report `evidence.selection_qualified=true`；
4. horizon 的 point estimate 和有序 95% interval 同时存在；
5. 标签、特征、训练、校准、测试和 artifact 版本/摘要完整一致；
6. 样本外校准、区分度、稳定性、分层和区间门禁均通过。

Artifact 中每个 child 的 selection gate 独立验证，投影到公开 API 时折叠进对应
horizon `status`；公开的 `IndividualUpsideHorizon` 没有单独的
`selection_qualified` 字段。报告级 `evidence.selection_qualified` 是“至少一个公开
child 已获授权”的聚合状态，仍不能替代所选 child 自己的 `status`。

否则 `probability=null`。UI 使用 `—` 和状态/原因，不得回退成 `0%`、`50%`、
趋势分百分数或旧股票结果。`not_generated` 表示尚无研究 artifact；
`insufficient_data` 表示已有证据但未满足显示合同；二者都不是看跌判断。

`confidence_interval` 只有在预注册的样本外覆盖方法真正执行且通过对应门禁时才
可显示。完整性 SHA-256 只证明内容地址和重放一致，不是签名、数据发行者身份或
预测有效性的证明。

## 模型与验证方案

### P0：本轮已落地的可信空状态

- 三个固定 horizon 的独立 typed report/API/UI；
- 真实 PIT 正式证据和非正式认证历史回放完全隔离；
- report/horizon 双层状态、概率/区间 `null`、证据计数、限制和 gate reason；
- compact assessment 不持久化当前个股可重放 predictor；即使其它诊断未来通过，也以
  `current_stock_replayable_predictor_not_persisted` 保持 null，不能用 base rate 代替；
- current builder schema
  `individual-upside-probability-assessment-v4-source-intake-bound` exact-bind
  estimator/target/split；离线 intake 验证完整 source-v2/feature-v3/PIT-v3 records、
  time envelope、15:15 成熟日、全市场覆盖、注册 score spec/hash 与逐行 score replay；
  assessment v1/v3 拒绝，v2 只按 exact legacy shape 审计可读；compact runtime source
  replay 尚未落地，公开 projection 因此固定 count 0 / signal null / selection false；
- aware/non-future assessment time、quote/as-of/capture 顺序、official 完成交易日成熟边界、
  内容 SHA 文件指纹、稳定目录快照、deep-copy 与 same-instant conflict 全部 fail closed；
- 不改现有个股评分、研究报告、建议、全市场 rank 或自动化；
- 缺失证据 fail closed，前端不生成占位概率。

P0 的价值不是强行产生三个数字，而是先消除最危险的伪概率路径，并让正式 PIT
样本从今以后按一致合同累积。

离线历史输入先深度验证 manifest，只接受无 `<db>-wal`、`<db>-shm`、
`<db>-journal` sidecar、非链接且不超过 512 MiB 的普通 SQLite 文件，并执行完整
字节读取、size/SHA-256/content identity 校验。研究器把这份已验证字节通过
`sqlite3.deserialize` 固化到内存 SQLite，在
单一 `query_only` transaction 中读取，不在校验后重新打开源路径。门禁证明完整
读取期间的 identity/hash 稳定；此后只消费隔离的已验证 bytes，源路径随后发生的
外部变化不会污染内存计算。发布前再次执行 sidecar、identity、size、SHA-256 与
完整 bytes 重验，所以评估期间源文件替换或修改会 fail closed。工具自身没有源
SQLite 写路径。这里不声称使用路径级 `mode=ro&immutable=1`，因为冻结字节快照才是本
个股研究 CLI 的 TOCTOU 边界；输出 compact artifact 的硬上限是 2 MiB。

### P1：真实 PIT 样本成熟后的第一组 challenger

1. **可解释基线**：按 horizon 独立的 L2 Logit，使用训练窗拟合、独立校准窗做
   Platt/保守校准、完整测试窗给出 OOS 预测；经验基准只用于对比。
2. **价格路径**：1/3/5/10/20/60 日收益、skip-near-term 动量、实现波动、下行
   波动、ATR/振幅、收盘区间位置、隔夜缺口、量比和量价背离。所有特征只使用 D
   及以前且优先采用对 qfq 缩放不敏感的比率。
3. **横截面与环境**：市场/板块相对强度、市场状态、流动性分层和风险状态；只有
   effective-dated PIT 行业/规模证据可进入历史特征。
4. **浅层 challenger**：Elastic Net、受深度/叶节点约束的 GBRT、稳定线性直通
   加有界交互；每个模型和 feature family 做消融，不用深度网络作为首发模型。
5. **目标 challenger**：声明成本后的日 K 代理正收益为冻结 UI 主目标；市场
   相对正收益和横截面 rank 标签只作为独立研究目标，不共用校准器或用户标签，
   也不把 rank 称为成交概率。

### P2：取得可审计新增数据后

- PIT 财务质量、估值、盈利预期修正、公告/研报情绪和状态所有权；
- 动态行业宽度、供应链/经济网络 peer strength 与 within-peer position；
- 拥挤、持仓网络、逐笔/盘口成交能力和更真实冲击成本；
- 多尺度序列或图网络只能作为独立 Shadow challenger，必须保留 Logit/浅层树
  基线、消融、成本和样本外门禁。

### 时间切分与门禁

- 以**交易日为 group**，同日所有股票只能同时属于训练、校准或测试；不能按行
  随机切分。
- 训练与校准、校准与测试之间保留不小于最大标签持有/重叠长度的 embargo；三个
  horizon 分开重放。
- 只使用完整、不重叠的测试折；不把 partial tail、同日重复快照或当前时点证据
  当作独立交易日。
- 主校准指标使用 Brier / Brier Skill 和 Log Loss，同时报告 AUC、校准截距/斜率、
  ECE、有效概率箱、最高箱相对基准、区间覆盖、早晚折稳定性和主要 A 股分层。
- 必须至少有两个完整 OOS 折且每折不少于 60 个 OOS 交易日；H1/H2/H3 的
  independent-session 下限分别为 284/286/288。aggregate 与每折 Brier Skill 均为正，
  并严格重建 `1 - Brier/reference Brier`；至少 2 个有效箱，最稀箱不少于 20 日，
  有效箱单调且最高箱高于基准。signal/training cutoff 均须为可信交易日且 cutoff
  严格早于 signal；任一 horizon 独立失败，只将该 horizon 保持为 null。
- 预注册模型/目标/特征族，使用时间块 bootstrap 和同族多重检验；不通过筛选大量
  配置后只报告最好结果。
- 声明成本以及当前尚未建模的容量、真实停牌、锁定涨跌停排队、上市期、ST、
  新股、流动性、市场/板块和状态期限制必须可见；缺失值不填 0，也不把 coverage
  当成准确率。只有取得有效日期正确的状态/盘口/成交证据后，后者才可升级为真实
  成交门禁。

## 当前实测边界

| 证据队列 | 身份 | 当前结果 | 产品权限 |
| --- | --- | --- | --- |
| current-v4 build-time intake PIT | 完整 source-v2 / feature-v3 / PIT-v3 records；15:15 time envelope、ALL/SH/SZ/BJ coverage、注册 score spec 与逐行 replay 全绑定；compact runtime 尚无 locator/records replay | runtime 公开投影固定 0 个独立信号日，低于 H1/H2/H3 两折门槛 284/286/288 日 | `signal_date=null`、`status=insufficient_data`，D+2/3/4 概率与区间全部 null；非零内部声明增加 source-not-replayed limitation |
| tracked v2 official-source identities | legacy run/date/digest-only audit evidence | 保留 run 71/run 77 两个 source-v1 身份，但不满足 current source contract | 审计可读，产品计数固定为 0，不得升级或填充信号日 |
| 认证历史回放 | `official=false`、独立非正式研究 | 实际 OOS 选择门禁未通过；且当前队列/元数据/可交易证据不能证明历史正式 PIT | 不能填充正式报告，不能显示百分比，不能影响评分/建议 |
| 当前趋势、诊断和情景权重 | 规则型个股分析 | 没有独立二元标签与校准合同 | 继续按原语义展示，禁止换算为 D+2/3/4 概率 |
| 全市场 1/5/20 日概率 | 独立 cohort、目标、特征和 artifact | 与个股 D+2/3/4 合同不同 | 不借用模型、校准器、基准或投影 |

认证历史回放可以证明 pipeline 能读取、切分、训练、校准和拒绝证据；它不能证明
当前个股概率有效。即使未来非正式回放通过，也必须先获得与当前正式合同一致的
PIT universe、上市/ST/行业/可交易状态、成本/容量和数据版本，才能讨论迁移。

本轮实际回放使用认证历史 manifest
`b30942ef71d64b45642adbd61df329f3ceb687917dd0cdf0451c9386517d3b3e`，
其只读 SQLite SHA-256 为
`946db7db1a5b62342d139f223784545cd71020ed87a5679eb7d3a7a3fa80927a`。
样本包含 96 只 SH/SZ/BJ 股票、2025-05-21 至 2026-07-13 的 279 个信号日、
每 horizon 26,784 个有效观察；唯一完整 OOS 折包含 60 日、5,760 个观察。
tracked 个股 compact assessment 生成于 `2026-08-12T12:00:00+00:00`，schema 为
legacy audit-readable `individual-upside-probability-assessment-v2-estimator-bound`，
内容完整性 digest 为
`517691b101dcb2142693a74f6e5ac9ef10f386c545572b6bacfe161f186ba677`，
canonical JSON 为 8,733 bytes。该 digest 是 artifact 内容封印，不是文件外层
SHA-256。它把 2026-08-11 绑定到 legacy source-v1 run 71 /
`150cc48d464f888a465f2be2f44807bff4d885d2e71a95f5c58144146c0ecd3d`，
把 2026-08-12 绑定到 run 77 /
`c085b6fad503e66e2598dbbd8b14d6fa277927ca9cc699845c9b2a66e8ea7f6d`；
但缺少 v4 所需 time/coverage/registered-score/full-record intake identity，所以
current 投影明确输出 0/288、`signal_date=null`，并携带
`legacy_official_pit_sources_audit_only_not_current_evidence`。artifact 中的 horizon
metrics 也携带 `compact_horizon_metrics_not_independently_replayable`，不能仅靠外层
re-seal 证明完整模型重放；同理，v4 的完整 intake identity 虽可阻止正常 builder/
verifier 漂移，仍不能在没有 locator/records 时仅靠外层 re-seal 独立证明原 source
文件真实存在或与声明一致。
经验证的 compact JSON 保留在 `docs/research/artifacts/`，供干净部署只读展示同一
`insufficient_data` 基线；显式离线 CLI 的新输出仍写入本地
`data/research/individual_probability/`，不会覆盖该基线。

| 展示 / 持有 | 历史 fit 状态 | OOS AUC | Brier | Brier Skill | ECE | 关键失败 | 当前产品结果 |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| D+2 / 1 日 | `insufficient_data` | 0.493881 | 0.248855 | -0.001813 | 0.024449 | 仅 1 折、概率箱日期不足、最高箱不高于基准、非正式历史 | `probability=null` |
| D+3 / 2 日 | `calibrated_shadow` 但 selection 未通过 | 0.498565 | 0.251979 | -0.004469 | 0.054359 | 仅 1 折、最高箱不高于基准、非正式历史 | `probability=null` |
| D+4 / 3 日 | `insufficient_data` | 0.502474 | 0.255867 | -0.005573 | 0.080095 | 仅 1 折、概率箱日期不足、箱非单调、非正式历史 | `probability=null` |

三个 horizon 的 Brier Skill 均为负，且 current-contract 正式 PIT 为 0/288 日；这两个事实
分别说明“历史 OHLCV 基线未优于基准”和“正式队列尚不能完成注册切分”。D+3 的
fit 状态不是展示授权：其 selection 仍为 false，且 compact artifact 未持久化可
对当前个股重放的 predictor，所以不能从 base rate 或历史诊断构造当前百分比。

历史 qfq OHLC 是按该 provider vintage 调整后的研究序列，不等于当日账户可成交的
现金价格；后续复权因子仍可能重写历史值。标签中的 10万元名义金额与向下取整的
100 股整手只用于固定成本/数量基础，也不证明当时账户购买力、报单、容量或成交。

## 执行顺序与完成定义

1. **持续积累**：每天完成交易后冻结 feature/label contract、数据版本、来源、
   effective time 和 digest；固定退出未成熟时保持 `not_mature`。
2. **离线重放**：每个 horizon 独立构建时间分组 walk-forward、校准和全输入重放；
   HTTP 请求不训练、不写 artifact、不修改 SQLite。
3. **研究评审**：比较冻结 Logit 与预注册 challenger，检查 calibration、稳定性、
   成本/容量、分层和多重检验；所有失败理由保留在 artifact/report。
4. **只读发布**：仅把已验证、内容地址化、完整重放的 report 投影到独立 API；
   report/horizon 均为 `calibrated_shadow`、报告聚合 selection 已授权且 point/CI
   完整时，才能显示对应 horizon 的百分比。
5. **人工晋级**：通过门禁也只允许人工评审。当前 `production_effect=none`，不提供
   自动晋级、筛选、下单或建议强化入口。

本方案完成的验收标准是：D+2/D+3/D+4 语义唯一、状态和 null 合同可被 API/UI
测试重放；真实证据不足时不出现任何伪百分比；现有评分/建议字节级语义不因该
研究改变；研究来源、采用边界、当前实测失败和后续数据路线均可追溯。
