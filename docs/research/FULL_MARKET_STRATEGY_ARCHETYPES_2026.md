# 全市场多策略选股原型研究与优化方案（2026-08-12）

> 2026-08-13 信任边界更新：目录中的两日统计是冻结研究背景，不是当前动作权限。运行时现在以 `market-scan-snapshot-digest-v2` 和 `snapshot_seal_origin` 区分原发布证据与迁移审计；`legacy_backfill`（包括 run #80）不可授权策略执行、simulation、executable Shadow、证据刷新或自动化。即使 origin 为 `publication`，动作也必须来自精确全市场范围，并通过 diagnostics-v1 的无 blocker、唯一 info `score_distribution.pass`、无评分分布冲突门禁。新策略执行必须绑定该动作源快照 digest/origin；迁移后缺少源 run 的旧 execution 保持 `source_snapshot_verification_status=legacy_unverified`，只可 forensic audit，不能用于研究或动作。只读 browse/audit 不因动作资格不足而被隐藏。

## 摘要

全市场选股可以、也应该按策略拆分，但“分策略”不能只是给同一个总分换几个名称。
每个策略必须有独立的经济假设、信号形成期、固定入场与退出、持有期、可交易股票池、
成本与容量合同、样本外证据和晋级门禁。不同策略的原始分不可直接相加；只有已经通过
同一证据标准的策略专家，才可进入多策略组合。

本报告冻结以下边界：

- 当前生产评分仍为 `full-market-score-v4`，排名、Top N、导出、自动任务和发布规则均不变；
- 当前 API 的 `available_for_draft` **只表示能够构造并 dry-run 编译一个现有
  `StrategySpec` v1 草案**，不表示该策略有效、可交易、已回测或可进入生产；
- 六个可载入模板为 `efficacy_status=not_generated`，三个 Shadow 路线为
  `insufficient_data`，五个缺字段路线为 `unavailable`；没有任何 `pass/passed` 状态，
  目录级 `production_effect=none`；
- 截至 2026-08-12，真实正式 PIT 全市场源只有 run #71 与 run #77 两个独立交易日；
- 两日横截面可以验证合同、完整性和模板投影，不能验证任何策略收益、因子轮动或状态路由；
- 新策略只能先作为 Shadow。没有合格证据时，收益、概率、区间、显著性和晋级结论保持
  `null` / `not_generated` / `insufficient_data`，不得用趋势分或历史基准填充。

## 1. 为什么必须拆成独立策略

同一个特征在不同期限、市场状态和交易约束下可能有相反含义。A 股一手研究尤其说明：

1. 日频收益可能先延续、随后反转，而周频和月频传统动量较弱；
2. 流动性既可能包含 Alpha 信息，也会决定交易成本和容量，两种语义不能混为一项加分；
3. 红利、低波、价值、质量和动量在 A 股的长期层级与全球市场不同，并且会随周期轮换；
4. 公告事件的价格发现可能主要发生在公告前，事后看到的财务结果不能回填为历史信号；
5. 小盘、低流动性和高换手信号常贡献较高毛收益，也最容易在真实成本后消失；
6. 拥挤可能与近期收益并存，同时提高尾部解拥风险，不能被简单处理为正向 Alpha。

因此，产品应从“一个万能分数”演进为：

```text
独立 StrategySpec 草案
        ↓
策略专属 PIT 数据、标签、成本与容量评估
        ↓
策略专属 Shadow 证据和 selection gate
        ↓
仅已过门禁的专家进入 balanced 静态组合
        ↓
动态路由仍需单独 Shadow 门禁和人工晋级
```

## 2. 冻结策略 taxonomy

### 2.1 策略目录

| strategy_id | 研究假设与主要信号 | 建议形成期 | 建议固定退出 / 调仓 | 主要适用状态假设 | 主要失效风险 |
|---|---|---:|---:|---|---|
| `daily_continuation` | 极短期价格延续；当日相对收益、量价确认、收盘位置、市场宽度、距涨跌停位置 | 1–3 日 | 最早 D+2 收盘；1–3 日滚动研究 | 风险偏好上升、宽度扩张、非极端拥挤 | T+1、涨跌停、开盘跳空和冲击吞噬收益；延续很快转为反转 |
| `short_reversal` | 短期过度反应后的残差反转；市场/行业残差收益、极端收益、成交量和流动性冲击 | 1–5 日 | D+3 / D+5 / D+10；2–10 日 | 震荡、高换手、非单边趋势 | 小微盘、低流动性与短端贡献可能不可交易；短端与延续混合会抵消 |
| `medium_momentum` | 中期个股与行业趋势延续；风险调整动量、行业动量、趋势稳定性 | 60–252 日，跳过最近约 20 日 | D+20 / D+60；月度 | 风险偏好稳定、趋势扩散 | A 股传统周/月动量证据弱且历史上可长期落后；崩盘和反转风险 |
| `value_garp` | 低估值或合理价格成长；EP/BP/SP/FCF/EV 复合估值，结合增长、预期与质量 | 最近有效 PIT 财报及预期，通常 1–4 季度 | D+60 / D+120 / D+250；月/季 | 估值修复、盈利企稳、通胀或利率上行假设需另证 | 价值陷阱、行业/SOE 偏置、无形资产低估、财务发布日期泄漏 |
| `quality_growth` | 盈利质量和可持续成长；盈利能力、应计、杠杆、现金转换、收入/利润增长与稳定性 | 最近有效 PIT 财报、预告/快报 | D+60 / D+120 / D+250；月/季 | 盈利确定性受重视、风险偏好下降或结构成长 | 高估值抵消质量收益；单独质量可能经历长时间落后；修订数据泄漏 |
| `dividend_low_vol` | 可持续股息、现金流与低波防御；股息增长、支付率、自由现金流覆盖、下行 Beta | 1–4 季度基本面 + 60–252 日风险 | D+60 / D+120 / D+250；月/季 | 高波、下行、配置资金占优 | 高股息陷阱、行业/SOE 集中、除权处理错误；低波不是无风险 |
| `event_revision` | 公告后的信息缓慢吸收；业绩预告/快报/正式报告、分析师修正、回购与分红事件 | 单一事件，按公布时间和修订版本 | 事件后首个合格 D 的 D+5 / D+20 / D+60 | 财报季、信息分散度高 | 公告前漂移被错误当作可交易事后信号；同日发布时间和停复牌处理错误 |
| `liquidity_attention` | 流动性、换手和关注的横截面信息；成交额、换手、Amihud、价量、大单、分析师/机构/舆情 | 1–60 日，按描述子分开 | D+2 / D+5 / D+20 | 散户参与高、信息传播慢 | Alpha、成本与容量语义混淆；高换手和冲击令净收益翻负 |
| `crowding_risk` | 共同持仓、资金流、主题热度、估值/动量极端和相关性抬升所表示的解拥风险 | 5–60 日 | 风险 overlay；与被保护策略同步 | 拥挤升温或风险承受能力下降 | 拥挤长端仍可能继续上涨；把风险指标反向交易会引入新的择时风险 |
| `balanced` | 对**已经独立过门禁**的策略专家做静态收缩或风险预算组合 | 专家各自形成期 | 初版月度或低频再平衡 | 不依赖单一状态 | 未过门禁专家被平均后仍是未验证信号；权重优化和路由过拟合 |

表中的状态只是预注册的分层假设，不是当前路由规则。P0 不根据 bull/bear、波动、
宽度、宏观变量或 HMM 标签自动切换策略。未来状态路由必须设置滞后、滞回、收缩、
权重上限和无状态证据时的静态 fallback，并独立证明优于静态组合。

### 2.2 采用与拒绝边界

#### `daily_continuation`

- **采用**：单独的超短期限合同；量价与市场状态只使用 D 收盘前已完成证据；排除固定
  入场/退出日缺失、零成交、停牌或证据版本冲突的样本。
- **拒绝**：与 `short_reversal` 共用一套目标或权重；把 D+1 日内毛收益当作 A 股
  可执行收益；因涨停后的机械延续而自动加分。

#### `short_reversal`

- **采用**：先计算市场、行业和流动性分层残差，再研究反转；成本与容量单独报告；
  只作为 Shadow challenger 起步。
- **拒绝**：直接做“昨日跌幅越大越买”；依赖无法成交的跌停/微盘；只报告等权毛收益。

#### `medium_momentum`

- **采用**：跳过近端反转窗口，个股和行业动量分开，报告风险状态和崩盘压力测试。
- **拒绝**：因海外动量证据强就设为 A 股默认主策略；将短期延续、月度动量和行业趋势
  合并成一个“趋势”含义。

#### `value_garp`、`quality_growth` 与 `dividend_low_vol`

- **采用**：使用多个描述子、有效日期正确的原始披露和修订历史；对行业、规模、Beta、
  SOE、流动性和非目标风格暴露做归因；长周期收益必须包含正确的现金分红/公司行动。
- **拒绝**：只按当前 PE/PB/ROE/股息率排序；用当前财报回填历史；把高股息称为低风险；
  用今天的前复权因子重写当时不可知的历史标签。

#### `event_revision`

- **采用**：事件以交易所/公司公布时间、首次可用时间、修订版本和来源摘要为准；事件在
  收盘后公布时，信号日必须移至下一个满足合同的交易日。
- **拒绝**：交易公告前漂移；按报告期而非披露时间排序；把未来分析师一致预期版本用于
  历史；事件日没有可交易证据时顺延到“下一次方便的价格”。

#### `liquidity_attention` 与 `crowding_risk`

- **采用**：流动性同时保留 `alpha_feature`、`cost_input`、`capacity_gate` 三个独立字段；
  拥挤首先用于仓位折扣、集中度上限与压力测试。
- **拒绝**：低流动性自动加分；原始热度自动加分；看到拥挤后无条件做反向交易；把单日
  成交额覆盖率当作真实市场冲击已建模。

#### `balanced`

- **采用**：先以静态等权、等风险或强收缩权重作为基线；专家信号先在各自可比股票池
  内标准化；组合层重新执行风险、成本、容量和暴露约束。
- **拒绝**：直接相加不同尺度的原始分；未通过的策略以“分散化”为由进入组合；看到
  回测结果后选择路由状态、窗口或权重。

## 3. A 股与前沿一手证据

### 3.1 A 股本地证据

| 一手来源与日期 | 主要证据 | 本项目采用 | 本项目不据此宣称 |
|---|---|---|---|
| [MSCI, *Are You Really Capturing the Right Factors?*, 2025-12-09](https://www.msci.com/research-and-insights/paper/are-you-really-capturing-the-right-factors-unlocking-deeper-insights-in-china-a-share-factor-investing) | 2010 至 2025-06 的 A 股指数研究中，高股息、最小波动和分散多因子长期领先，动量落后；非目标规模、估值、流动性和 SOE 暴露会显著影响结果。 | 本地化策略优先级、单因子归因、非目标暴露控制。 | 指数回测不等于本项目 Alpha，也不保证未来表现。 |
| [中证指数《策略指数及指数化投资发展年度报告（2025）》, 2026-03-27](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/researches/files/zh_CN/20260327180029-%E7%AD%96%E7%95%A5%E6%8C%87%E6%95%B0%E5%8F%8A%E6%8C%87%E6%95%B0%E5%8C%96%E6%8A%95%E8%B5%84%E5%8F%91%E5%B1%95%E5%B9%B4%E5%BA%A6%E6%8A%A5%E5%91%8A%EF%BC%882025%EF%BC%89.pdf) | 境内策略指数由单因子向多因子、动态配置演进；2025 年红利占产品规模近八成，现金流、低波和质量受到配置资金关注；报告强调规则、容量和可复制性。 | taxonomy、规则透明、容量友好和多因子路线。 | 产品规模和资金流不是策略有效性或因果证据。 |
| [S&P DJI, *How Smart Beta Strategies Work in the Chinese Market*, 2019-04-10](https://www.spglobal.com/spdji/en/research/article/how-smart-beta-strategies-work-in-the-chinese-market/) | 2006–2018 样本中，动量更偏上涨环境；低波、价值、质量和股息更偏下跌环境；低波与高股息历史风险调整表现较强。 | 将 up/down 与情绪状态预注册为分层诊断。 | 样本较早且含模拟指数，不能直接生成 2026 动态路由。 |
| [Leippold, Wang and Zhou, *Machine Learning in the Chinese Stock Market*, JFE 2022](https://doi.org/10.1016/j.jfineco.2021.08.017) | A 股中流动性是重要预测族；散户占比与小盘短期可预测性有关，大盘与 SOE 在较长周期也有可预测性；论文检查成本后表现。 | 流动性、规模、所有权与期限交互；净成本门禁。 | 不复制论文参数，不把低流动性本身称为正 Alpha。 |
| [Gao, Jiang, Xiong and Xiong, *Daily Momentum and New Investors in an Emerging Stock Market*, NBER w31839, 2023-11](https://www.nber.org/papers/w31839) | A 股存在约一日的日频动量，之后出现反转；周频/月频动量缺失。剔除触及价格限制的股票日后效应减弱但仍存在。 | 将超短延续和短反转拆成两个合同；价格限制单独门禁。 | 不把论文毛收益机械年化，不认为月度动量已被 A 股证明。 |
| [Yao and Yang, *Delayed Feedback Trading and Return Reversals: Evidence from China's T+1 Rule*, 2026-05-09](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6736959) | A/H 匹配和 T+1 制度研究支持延迟反馈交易与反转的制度渠道。 | 最早退出必须符合 T+1；沪深市场制度按有效期版本化。 | 工作论文不能单独授权生产反转策略。 |
| [Liu, Yu, Zhang and Zhang, *Earnings Announcement Drift in China*, PBCSF-NIFR, 2025-09-16](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5493686) | A 股公告前漂移约为公告后漂移的六倍；机构和大额个人交易方向与盈利意外一致。 | 事件策略只消费公告后可得证据；按投资者/所有权/信息环境分层。 | 公告前价格发现不是本项目可在公告后交易的信号。 |
| [S&P China A-Share factor definitions and methodologies](https://www.spglobal.com/spdji/en/landing/investment-themes/china/) | 官方 A 股指数分别定义价值、质量、低波、动量和股息，并配置流动性、成份数量、行业或权重约束。 | 多描述子、明确策略边界和可复制性约束。 | 指数方法不替代本项目 PIT 回放与净成本验证。 |

### 3.2 模型、组合与统计前沿

| 一手来源与日期 | 主要证据 | 本项目采用 | 拒绝边界 |
|---|---|---|---|
| [Li, Rossi, Yan and Zheng, *Machine learning from a “Universe” of signals*, JFE, 2025-07-22](https://doi.org/10.1016/j.jfineco.2025.104138) | 在实时可得的大型基本面信号空间中，简单递归排序优于若干黑箱 ML；低频基本面策略成本后仍为正，而高换手历史收益策略成本后为负。 | 简单可解释基线、feature engineering、按信号衰减匹配调仓、成本后比较。 | 不因模型更复杂而升级，也不把论文样本的收益率移植到 A 股。 |
| [Malamud and Pedersen et al., *Machine Learning and the Implementable Efficient Frontier*, RFS, 2026-03-15](https://academic.oup.com/rfs/advance-article/doi/10.1093/rfs/hhag022/8524346) | 成本无感预测会依赖短命小规模特征；组合应直接按净成本风险收益评价，并考虑 Alpha 衰减和资金规模。 | 多成本/资金规模情景、持仓滞回、净效用和经济特征重要性。 | 不直接复制海外机构最优权重或成本参数。 |
| [Lopez-Lira, *Confident Risk Premiums and Investments Using Machine Learning Uncertainties*, RFS, 2025-10-01 online / 2026-05 issue](https://academic.oup.com/rfs/article-abstract/39/5/1463/8287227) | 只选择预测区间较精确的股票，可改善多种模型的样本外组合。 | 低置信预测不入选或降权；区间覆盖需独立验证。 | 模型内方差、横截面分散度或数据完整性摘要不能冒充覆盖区间。 |
| [Avramov et al., *Multifactor Timing with Deep Learning*, JFEC, 2026-04-18](https://academic.oup.com/jfec/article/24/3/nbag006/8658726) | 有经济约束的多任务时序模型在海外因子择时中优于静态/普通 ML；149 因子扩展中最佳平均方向准确率仍仅 53.8%，基准为 52.8%。 | P2 的受约束多任务 router challenger；必须与静态等权和风险预算比较。 | 小幅分类优势不授权 A 股生产动态路由。 |
| [Haddad, Kozak and Santosh, *Factor Timing*, RFS 2020](https://academic.oup.com/rfs/article/33/5/1980/5753962) 与 [Vasilas, *Factor Timing with Portfolio Characteristics*, RAPS 2024](https://academic.oup.com/raps/article/14/1/84/7191017) | 因子收益具有时变性；降维和组合特征可帮助预测，但交易成本与实现性仍需单独判断。 | 用少量策略主题、收缩和低频路由减少维度；静态基线不可省略。 | 不为每个因子单独寻找最优状态，不在全样本后挑选路由变量。 |
| [Dong, Kang and Peress, *Fast and Slow Arbitrage*, RFS, 2025-05-27](https://academic.oup.com/rfs/article-abstract/38/10/2936/8151559) | 持久而非短暂的基金资金流可预测因子收益，样本外解释度最高约 6.6%。 | 将持久/暂时资金流分开，未来只作 P2 路由与拥挤证据。 | 没有可审计资金流历史时不以成交热度替代。 |
| [Chincarini, Lazo-Paz and Moneta, *Crowded spaces and anomalies*, JBF, 2026-01](https://www.sciencedirect.com/science/article/pii/S0378426625001992) | 异常组合长端收益集中于较拥挤股票，但拥挤也增加机构尾部解拥风险。 | 拥挤作为仓位、集中度和压力测试 overlay。 | 不把拥挤无条件加分或无条件反向交易。 |
| [Bellofatto et al., *Tradable Risk Factors for Institutional and Retail Investors*, Review of Finance, 2024-09-11](https://academic.oup.com/rof/article/29/1/103/7755053) | 纸面因子与可交易代理存在约 2%–4% 年化实施短缺；交易与卖空成本解释了显著差异。 | 将纸面 Alpha、实施成本和机会成本分开；只评价 A 股可执行长端。 | 学术多空因子不是 A 股普通账户可直接复制的组合。 |
| [Jensen, Kelly and Pedersen, *Is There a Replication Crisis in Finance?*, Journal of Finance 2023](https://onlinelibrary.wiley.com/doi/10.1111/jofi.13249) | 153 个因子可聚为 13 个主题；层级贝叶斯和全球样本支持不少主题具有可重复成分。 | 使用少量经济主题、层级收缩和跨市场证据作为先验。 | 主题层面可复制不表示每个 A 股实现、期限或排序规则有效。 |
| [Hou, Xue and Zhang, *Replicating Anomalies*, RFS 2020](https://academic.oup.com/rfs/article/33/5/2019/5236964) | 控制微盘、价值加权和多重检验后，大量已发表异常不能复现。 | 微盘/流动性分层、容量权重、FDR 和严格样本外门禁。 | 不只报告等权微盘收益，不将单因子 t 值当作最终证据。 |
| [Calónico and Galiani, *Beyond Bonferroni*, NBER w34050, 2025-07；2026-06 修订](https://www.nber.org/papers/w34050) | 多重检验方法应匹配决策结构；依赖结构下可用 Romano–Wolf 与层级方法提高可信度和检验力，关键是预注册。 | 试验族预注册、依赖感知重采样、家族级错误控制。 | 不在看过结果后重定义 family 或只保留赢家。 |
| [Koijen and Levy, *Assessing the Benefits of Optimized Agentic AI Systems for Asset Pricing*, NBER w35431, 2026-07](https://www.nber.org/papers/w35431) | 针对模型训练污染和市场反身性，论文建立公告时点的实时样本外 AI benchmark。 | P2 文本/agentic AI 只允许模型版本冻结的前向实验。 | 通用模型对历史文本的事后评分不能作为无泄漏回测。 |

### 3.3 中国机构实务与数据路线证据

- [南方上证 50 指数增强基金合同（2025）](https://www.citics.com/newsite/ywzx/tgyw/xxpl/cpgg/202503/P020250320598858717349.pdf)
  将股票模型分为多因子、GARP、相对价值和短期综合，并采用核心/卫星结构及行业、个股、
  风险与成本约束。它支持“独立模型 + 组合约束”的架构参考，不是收益有效性证明。
- [中信证券 2026 年量化投资数据采购需求](https://www.citics.com/newsite/xxgs/cgxmjg/202602/t20260210_1208117.html)
  要求严格 PIT 的行情、财务、预告/快报、一致预期、高频、机构情绪、研报文本、专利、
  网络、调研和舆情数据。它是数据字段与血缘验收参考，不是候选因子已有效的证据。
- [MSCI *Factor Indexing Through the Decades*, 2025-07](https://www.msci.com/downloads/web/msci-com/research-and-insights/paper/factor-indexing-through-the-decades/factor-indexing-through-the-decades.pdf)
  将价值、动量、质量、规模、低波和收益率列为核心策略族，并把无形资产调整、文本、
  分析师情绪和宏观冲击列为未来方向。新增数据仍须先证明 PIT 和增量净效用。

## 4. 当前真实数据与证据边界

本报告冻结时的正式源目录只有以下两份 canonical published artifact；它们的文件摘要和两日统计保留历史研究含义，不替代当前数据库 `publication` seal authorization：

| run | data_date | 模式 / 状态 | 成功 / 总数 | 特征与完整性 | 当前权限 |
|---:|---|---|---:|---|---|
| #71 | 2026-08-11 | `official` / `degraded` | 5,499 / 5,542 | 79 个特征；记录覆盖率与 source digest 覆盖率均为 1.0；payload digest `150cc48d464f888a465f2be2f44807bff4d885d2e71a95f5c58144146c0ecd3d` | 可用于合同、模板映射和完整性验证；不能评估收益 |
| #77 | 2026-08-12 | `official` / `degraded` | 5,494 / 5,543 | 79 个特征；记录覆盖率与 source digest 覆盖率均为 1.0；payload digest `c085b6fad503e66e2598dbbd8b14d6fa277927ca9cc699845c9b2a66e8ea7f6d` | 可用于合同、模板映射和完整性验证；不能评估收益 |

两个 digest 是内容完整性摘要，不是数据发行者身份签名。两个交易日也不等于两个成熟
标签日：截至本报告时点，最短满足 A 股 T+1 的固定退出结果尚不足，更不可能评估 20、
60、120 或 250 日策略。历史回放、当前数据库快照和个股概率研究 artifact 均不能冒充
上述策略目录的正式逐日 PIT 策略证据。

因此当前统一状态为：

```text
template_contract: partially_available
official_pit_sessions: 2
strategy_efficacy: not_generated
calibration: not_generated
multiple_testing_result: not_generated
promotion_eligible: false
production_effect: none
production_score: full-market-score-v4 (unchanged)
```

## 5. P0 StrategySpec 模板目录与 availability

### 5.1 已实现的固定 API 合同

仓库现已实现只读 `GET /api/strategy-lab/templates`。严格响应模型为
`full-market-strategy-template-catalog-v1`，并固定：

```text
as_of_date: 2026-08-12
selection_mode: exclusive
production_rule_version: full-market-score-v4
production_effect: none
official_session_count: 2
template_count: 14
catalog_digest: 0038e66d4ce6c13bafb51e3fddf990c11f5f8f38c001343f5f4523ff255a9d1f
```

该接口固定服务于精确范围 `沪市 + 深市 + 北交所当前上市A股` 的 SH/SZ/BJ 全市场研究，不接受个股、自定义 universe 或 `scope` 参数。
它不读取 provider 或 SQLite，不保存策略、不启动扫描、不生成执行或证据，并返回
`Cache-Control: no-store`。目录及每个模板均用排除自身摘要字段后的 canonical JSON
SHA-256 绑定语义；摘要用于完整性和漂移识别，不是发行者签名。

每个模板严格返回 identity/name/family/objective、formation/holding/rebalance horizon、
availability、可空 `strategy_spec`、contract/efficacy/regime 状态、required/missing fields、
gate/regime/cost/risk/limitation notes 和 digest。模板按 `template_id/version` 确定性排序，
但 `template_id` 本身必须全局唯一；Pydantic 模型会重算 template/catalog 两层摘要。额外
字段、重复 ID/列表值、摘要与内容不符、非法状态组合和 `missing_fields` 非 required 子集
均 fail closed。

### 5.2 实际状态定义

- `available_for_draft`：必须包含可规范化、指纹化和 dry-run 编译的同名 StrategySpec，
  `contract_status=verified`、`efficacy_status=not_generated` 且无缺失字段；只证明草案合同
  可表达，不证明 archetype 有效。`profile` 必须是 `custom`，避免命名画像覆盖固定目标
  权重；hard filter 字段/周期必须属于模板白名单；`required_fields` 直接等于编译器完整
  execution plan，而不是只列作者显式写出的过滤字段。
- `shadow_only`：研究字段合同已冻结，但只有两个 official 交易日；必须
  `strategy_spec=null`、`contract_status=verified`、`efficacy_status=insufficient_data`，不能
  从目录载入为可执行草案。
- `unavailable`：关键 PIT 字段缺失；必须 `strategy_spec=null`、contract/efficacy 均
  `unavailable`，并显式列出 `missing_fields`；禁止用代理、补零或通用 balanced 静默降级。

### 5.3 当前 14 模板能力矩阵

六个可载入模板的完整 `required_fields` 为 16–18 项，统一包含默认的上市板块、上市天数、
冻结日停牌代理、ST、状态、行业、价格、成交额、质量、61 根 PIT 日 K 合同与六个评分维度；
使用收益硬过滤的草案再加入相应 1/20/60 日 raw return 字段。这个集合来自 dry-run 编译
结果，不能手工缩减为“策略自定义过滤条件”。

| template_id | availability | 形成/持有/调仓 | 当前可表达或已冻结 | 仍未获授权的含义 | efficacy_status |
|---|---|---:|---|---|---|
| `balanced_multi_horizon` | `available_for_draft` | 61/5/5 | 1/5/20 日序数目标、风险、置信、可交易、成交额与组合约束 | 不是真正由过门禁专家组成的 balanced 组合 | `not_generated` |
| `bounded_medium_trend` | `available_for_draft` | 61/10/10 | 有界 20/60 日趋势代理、风险、流动性和成交额 | 不是已验证中期动量；10/10 用于避免无独立调仓控件时的载入漂移 | `not_generated` |
| `capacity_first` | `available_for_draft` | 61/5/5 | 成交额、可交易性、风险、序数目标和较低单股上限 | 日成交额约束不是盘口冲击或真实容量证明 | `not_generated` |
| `daily_continuation` | `available_for_draft` | 61/1/1 | 1/5 日序数、当日收益范围、风险、可交易与成交额 | 61 日是当前输入证据合同；不证明真正的 1–3 日延续 Alpha 或可成交退出 | `not_generated` |
| `defensive_liquidity` | `available_for_draft` | 61/10/10 | 风险、可交易、成交额与中期序数约束 | 不是有 PIT 股息/低波描述子的防御因子；10/10 防载入漂移 | `not_generated` |
| `pullback_continuation` | `available_for_draft` | 61/5/5 | 中期序数较强且单日有界回撤的草案 | 不是已验证反转或延续策略 | `not_generated` |
| `industry_relative_strength` | `shadow_only` | 61/5/5 | 行业、行业相对强度、20 日序数与可交易字段合同 | 无足够跨日 OOS 证据，不返回 StrategySpec | `insufficient_data` |
| `medium_momentum` | `shadow_only` | 61/5/5 | 20/60 日收益、风险、可交易字段合同 | 未建立 skip-month、长期形成期和状态证据 | `insufficient_data` |
| `short_reversal` | `shadow_only` | 61/5/5 | 收益、风险、可交易与成交额字段合同 | 无残差反转 target、固定净收益标签和足够交易日 | `insufficient_data` |
| `crowding_risk` | `unavailable` | 252/20/20 | 当前只有 `amount`、`tradability` 等不完整代理 | 缺 PIT common holdings、fund flow、crowding score、capacity score；不可声称拥挤合同完整 | `unavailable` |
| `dividend_low_vol` | `unavailable` | 252/20/20 | 无可载入策略 | 缺 PIT 股息、分红事件、自由流通市值等字段 | `unavailable` |
| `event_revision` | `unavailable` | 252/20/20 | 无可载入策略 | 缺公告事件、分析师修正和一致预期 PIT | `unavailable` |
| `quality_growth` | `unavailable` | 252/20/20 | 无可载入策略 | 缺 PIT ROE、现金流质量与盈利增长 | `unavailable` |
| `value_garp` | `unavailable` | 252/20/20 | 无可载入策略 | 缺 PIT PE、PEG 与盈利增长 | `unavailable` |

所有 14 项共同披露：当前停牌与一字状态只使用冻结日 K 成交额和持久化原因文本代理，
不等同交易所逐时停复牌、封单、订单优先级或实际排队成交证据。该限制必须随模板显示，
不能因为字段进入编译器 required set 就删去。

研究 taxonomy 与产品模板不是一一等同：`liquidity_attention` 当前被拆成防御流动性与容量
草案，但没有独立有效性；`dynamic_router` 仍停留在 P2 研究路线，未进入这 14 个 P0
模板。产品不得把 `available_for_draft` 翻译为无条件的“策略已就绪”。固定文案应为：

> 模板合同可载入；策略有效性尚未生成；仅生成研究草案；不改变生产排名。

## 6. 标签、持有期与再平衡合同

### 6.1 统一时间轴

所有可交易收益研究先使用同一冻结时间轴，再由各策略选择自己的 horizon：

```text
signal D:
  只使用 effective_at <= D 官方收盘的 canonical published PIT 证据

entry proxy:
  固定交易所日历 D+1 官方日 K open
  不顺延到“下一次可用开盘”

earliest exit:
  D+2 close（买入 D+1 后满足 A 股股票 T+1）

target:
  固定退出价格代理的 round-trip net return
  或减去预注册 benchmark 后的 net excess return
```

绝对净收益、市场/行业相对净收益与横截面 rank 是三个不同 target family。它们必须有
不同的 `target_id`、试验族和报告列；rank/z-score 不能解释为收益幅度或上涨概率。

交易制度必须按交易所和生效日期版本化。截至本报告日期，现行一手规则为
[《上海证券交易所交易规则（2026 年修订）》](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)
和
[《深圳证券交易所交易规则（2026 年修订）》](https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html)，
两者均于 2026-04-24 发布、2026-07-06 施行。上交所规则第 3.1.4 条明确，买入证券在
交收前不得卖出，实行回转交易的品种除外。因此本报告对普通 A 股采用“D+1 买入、最早
D+2 卖出”的保守合同；回转交易品种、历史规则日期和交易所差异不得沿用这一默认值。

### 6.2 策略期限矩阵

| strategy_id | 首发研究退出 | 扩展退出 | 再平衡上限 | 备注 |
|---|---|---|---|---|
| `daily_continuation` | D+2 | D+3 | 每日 | 不研究违反 T+1 的 D+1 卖出 |
| `short_reversal` | D+3 / D+5 | D+10 | 2–5 日并带滞回 | 与 continuation 分开训练与选择 |
| `medium_momentum` | D+20 | D+60 | 月度 | 形成期与退出期分开，跳过近端窗口 |
| `value_garp` | D+60 | D+120 / D+250 | 月/季 | 财务变化慢，不允许日频无意义换手 |
| `quality_growth` | D+60 | D+120 / D+250 | 月/季 | 仅在新 PIT 披露或定期计划触发 |
| `dividend_low_vol` | D+60 | D+120 / D+250 | 月/季 | 标签需正确计入现金分红和公司行动 |
| `event_revision` | 事件后 D+5 | D+20 / D+60 | 事件驱动 + 冷却期 | 同公司多事件需预注册合并规则 |
| `liquidity_attention` | D+2 / D+5 | D+20 | 日/周 | 成本压力必须随换手同步扩大 |
| `crowding_risk` | 不单独生成买入 target | 与所保护专家一致 | 与专家同步 | 首发只做风险 overlay |
| `balanced` | 由专家净目标组成 | 月度组合评价 | 月度或更低频 | 不混用专家原始预测尺度 |

### 6.3 固定日 fail-closed

以下情况样本不可用，不能顺延、填零或改用当前价格：

- 入场或固定退出日尚未成熟；
- 官方日 K 缺失、零成交、adjustment/data/contract 版本冲突；
- 当时的上市、ST、停复牌、板块或涨跌停规则无法确认；
- 事件在信号截点后公布，或财务/分析师版本的首次可用时间不明；
- 长持有期跨公司行动但没有可审计的现金流/股本调整；
- benchmark、行业或股票池使用了未来成份信息。

日 K open/close 是价格代理，不能证明真实排队、成交优先级、盘中触及顺序或实际 fill。

## 7. 成本、容量与风险合同

### 7.1 成本必须有效日期化

每个 `cost_profile` 至少记录：

- 买卖佣金、最低佣金、过户费、卖出印花税及生效日期；
- 买卖滑点、spread 代理、冲击模型版本；
- 策略换手、入选/保留双阈值、最小持有和冷却规则；
- base / conservative / stress 三档及其参数来源；
- 股票板块、价格区间和流动性分层的适用范围。

[《中华人民共和国印花税法》](https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193058/content.html)
规定证券交易印花税向出让方征收；
[财政部、税务总局 2023 年第 39 号公告](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html)
自 2023-08-28 起减半征收。跨政策日期必须切换版本，不能把当前费率永久回填历史。

### 7.2 容量必须与资金规模一起报告

首发报告至少覆盖 10 万、50 万、100 万元研究名义本金，并显示：

- 单股下单额 / 当日及 20 日 ADV 的参与率；
- 100 股整数手、最低佣金和未分配现金；
- 个股、行业、上市板块权重和持仓数上限；
- days-to-liquidate、成交额覆盖率、涨跌停/停牌不可执行比例；
- 组合换手、冲击后净收益和容量临界点；
- 大盘/中盘/小微盘、SH/SZ/BJ 和主板/科创/创业/北交所分层。

成交额大于某阈值只是预筛，不是容量证明。没有盘口、排队和冲击证据时，结果必须称为
`daily_bar_capacity_proxy`，不能称为真实可成交容量。

### 7.3 风险与非目标暴露

每个策略独立报告市场 Beta、行业、上市板块、规模、流动性、波动、SOE、价值/成长、
拥挤和单股贡献。中性化前后结果必须同时保留；不能只展示“净化”后的好结果而隐藏
原始策略实际承担的暴露。

## 8. 试验注册、时间切分与多重检验

### 8.1 不可变 trial registry

每次策略实验在读取测试结果前登记：

```text
trial_id
strategy_id / archetype_version / spec_fingerprint
research_question / economic_rationale
universe_contract / eligibility_contract
signal_cutoff / entry / target_id / exit_horizon
feature_family / neutralization / missing_policy
model / hyperparameter_space / seed
train / validation / calibration / test date groups
purge / embargo / overlap rule
cost_profile / notional / capacity scenario
primary_metric / secondary_metrics / rejection_direction
test_family_id / multiple_testing_method
data_artifact_digests / code_revision / registered_at
status / failure_reason
```

失败、无效、被中止和参数较差的 trial 仍保留在 registry；不得删除后缩小检验族。

### 8.2 时间切分

- 以交易日为 group，同日全部股票只能属于同一个 train/calibration/test 区段；
- purge/embargo 不小于最大标签重叠和信息发布时间缓冲；
- 只使用完整、成熟、不重叠的主测试折；重叠 horizon 使用 session block bootstrap 或
  合适的 HAC 推断；
- 行业、指数成份、财报、ST、停复牌、公司行动和规则均按当时有效版本重放；
- 训练、模型选择、概率校准和最终测试严格分开；测试折不能被反复用于调参。

### 8.3 指标与 selection gate

策略主门禁由预注册的 primary metric 决定，辅助报告至少包括：

- 成本后 Top20/50/100 超额、Rank IC/ICIR、分位单调性；
- 年化收益只在期限和独立样本允许时报告，同时报告置信区间、最大回撤和尾部损失；
- 换手、成本贡献、容量、暴露、早晚折与状态/市场分层稳定性；
- 对概率模型另报 Brier/Brier Skill、Log Loss、AUC、校准斜率/截距和区间覆盖；
- 候选相对 `full-market-score-v4` 与简单同策略基线的配对增量。

选择条件必须同时满足 PIT 完整性、足够独立 OOS 日期、成本后增量、风险与容量、主要
分层不退化和多重检验。即使全部通过，也只进入人工晋级评审；不自动改生产。

### 8.4 FDR 与路径过拟合

- 同一 `strategy_id × target_family × horizon` 的全部特征、模型和参数变体组成固定 family；
- 先用按交易日聚类、保留横截面相关性的重采样生成 p 值；独立或满足 PRDS 的 family 才用
  预注册 BH-FDR，任意或无法确认的依赖结构改用更保守的 BY-FDR，并固定目标 `q`；
- 多个相关主指标如要求控制 FWER，可预注册 Romano–Wolf stepdown；它不能被误写成 FDR，
  也不能在结果出来后与 BH/BY 相互切换；
- 策略主题可做层级 shrinkage，但不能用主题先验掩盖单个实现的失败；
- 在没有完整候选路径枚举与收益比率分布前，PBO 和 DSR 保持 `not_computed`；
- FDR 通过不等于经济显著，净收益/风险/成本/容量门禁仍必须独立通过。

## 9. P1 与 P2 数据及模型路线

### P1：先形成可解释、可验证的静态专家

#### 数据

1. 持续积累每日 canonical、通过 original-publication + 精确全市场范围 + diagnostics-v1 评分门禁的 PIT source，并绑定股票池和规则版本；
2. effective-dated 上市、退市、ST、停复牌、板块、涨跌停、流通股本、市值和行业；
3. 逐版本财报、业绩预告/快报、公告时间、修订记录、现金分红与公司行动；
4. 分钟/逐笔或可靠 spread、ADV 和冲击代理，用于快策略真实成本与容量；
5. 指数/行业 benchmark 的当时成份、收益和分类版本。

#### 模型与顺序

1. 首先冻结每个策略的复合排序或线性基线；
2. `value_garp`、`quality_growth`、`dividend_low_vol` 先做低频、行业内标准化的静态专家；
3. `daily_continuation` 与 `short_reversal` 严格分离，并优先验证成本/容量而非毛收益；
4. `event_revision` 和 `liquidity_attention` 在时间戳与交易证据完整后进入 Shadow；
5. Elastic Net、浅 GBRT 和有界交互只作为 challenger，逐特征族消融；
6. `balanced` 先比较等权、等风险和强收缩权重，不做状态择时。

### P2：新增可靠数据后的 Shadow 前沿

- 分析师预测与修正版本、机构/基金持仓、持久/暂时资金流、北向或其他可审计主体流；
- 公告/研报/新闻文本、调研、专利、供应链和经济同业网络；
- 共同持仓、主题热度、残差相关、估值/动量极端和更真实的拥挤/解拥压力；
- 受约束多任务因子 router、MoE/HMM 或离散状态模型；
- 置信区间感知的入选/降权和多策略联合风险预算；
- 文本 LLM/agentic 系统只进行模型版本、提示、工具和知识截止冻结的实时前向实验。

P2 的深度模型必须同时击败简单同策略基线、静态等权和静态风险预算，并通过增量净效用、
校准、漂移、解释、容量和稳定性门禁。AUC、方向准确率、毛 Sharpe 或论文 benchmark
排名中的任一单项提升都不足以晋级。

## 10. P0 / P1 / P2 可执行验收

### P0：合同与可信空状态

- typed catalog 固定 14 个 `template_id/version`、6/3/5 availability 分区、缺口、限制和摘要；
- `template_id` 全局唯一，模型重算 template/catalog 摘要，语义变更不得沿用旧 digest；
- `available_for_draft` 仅允许载入并 dry-run 编译 StrategySpec 草案，六项均
  `efficacy_status=not_generated`；其 16–18 项 required fields 必须逐项等于 compiler
  execution plan，`profile=custom` 与固定权重不得被命名画像覆盖；
- `balanced_multi_horizon` 对当前生产读取与排名无任何副作用；
- unsupported 因子不能静默降级为通用 `balanced_multi_horizon`；
- API/UI 分别显示 `not_generated`、`insufficient_data` 或 `unavailable`、原因和目录级
  `production_effect=none`；
- trial registry、标签、成本、容量和 FDR schema 先冻结；
- run #71/#77 的冻结 artifact 只能验证当时的确定性、指纹和 availability 投影，不能生成策略绩效；任何新 execution/evidence/automation 还必须独立通过当前原发布 snapshot seal；
- Ruff、mypy、pytest、JavaScript/E2E 和 inventory 在代码阶段统一验收。

### P1：静态策略证据

- 每个策略都有独立 target、固定期限、PIT feature manifest 和基线；
- 时间分组 walk-forward、purge/embargo、成本/容量及主要 A 股分层完整；
- 全部试验进入固定 family，FDR/依赖重采样结果可重放；
- 未通过的策略继续 Shadow，不向 `balanced` 提供权重；
- 通过者也只进入人工评审，不自动替换生产。

### P2：动态组合与另类数据

- 每个新增数据源有公布时间、首次可用时间、修订历史、来源与摘要；
- 动态 router 有静态 fallback、滞后、滞回、收缩和权重上限；
- 拥挤只先作为风险 overlay；文本/LLM 先做实时前向验证；
- router 与深度 challenger 以净成本效用而非毛预测指标晋级；
- 任一证据缺失、漂移或合同冲突均 fail closed。

## 11. 当前不能宣称的事项

截至 2026-08-12，项目不能宣称：

1. 任一新增 archetype 已经有效、盈利、稳定、校准或适合当前市场状态；
2. run #71/#77 两日已经形成回测、walk-forward、FDR、PBO 或 DSR 结论；
3. `available_for_draft` 表示策略已可投入生产；它只表示 StrategySpec 草案合同可构造并
   可通过 dry-run 编译；
4. 1/5/20 日 Alpha 序数分是收益预测、上涨概率或独立策略绩效；
5. 当前 `balanced_multi_horizon` 已经组合了通过门禁的独立策略专家；
6. 当前成交额与日 K 已证明真实成交、排队、冲击或资金容量；
7. 当前行业标签等同于有效日期正确的经济同业、供应链或持仓网络；
8. 当前数据包含完整 PIT 估值、质量、股息、预期修正、事件、持仓或拥挤因子；
9. 海外论文、指数回测、基金合同或机构采购需求已经证明 A 股策略 Alpha；
10. 动态路由、深度模型或 LLM 因为“更前沿”就优于静态可解释基线；
11. 新策略会改变 `full-market-score-v4`、生产排名、自动选股或真实交易；
12. 冻结日 K 成交额与原因文本已经证明逐时停牌状态、一字封单、排队优先级或真实 fill；
13. 本报告构成投资建议或收益承诺。

## 12. 冻结决策

本轮正确的交付不是立即生成十套不同排行榜，而是先建立十个互不混淆的策略研究合同。
P0 只发布模板目录、availability 和可信空状态；P1 在真实逐日 PIT 数据成熟后验证静态
专家；P2 才研究资金流、拥挤、文本和动态路由。

在任一阶段，生产不变量保持：

```text
production score/rank: full-market-score-v4 unchanged
new archetype templates: available draft / Shadow / unavailable only
available-draft efficacy: not_generated
shadow efficacy: insufficient_data
automatic promotion: false
real brokerage execution: none
```
