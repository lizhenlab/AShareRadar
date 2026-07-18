# 基本面与研究工作流竞品调研

> 调研日期：**2026-07-15**
> 调研对象：Simply Wall St、Stock Rover、Seeking Alpha、TipRanks、MarketScreener
> 资料口径：只采用产品官网、官方帮助中心、官方方法说明、官方定价页和官方产品公告。价格、套餐和覆盖范围均为日期截面，后续可能变化。

## 1. 结论先行

1. **主流核心不是“一个总分”，而是可下钻的研究闭环。** 五家都把财务、估值、同业、预期或专家观点、清单/组合和事件监控串成从初筛到复核的路径；总分只负责导航，原始指标、方法和缺失数据必须可见。
2. **对本地 A 股单股研究最值得借鉴的是 Simply Wall St 与 MarketScreener。** Simply Wall St 已有中国市场及沪深个股报告，擅长把财务健康、估值、风险和个人叙事压缩成易读页面；MarketScreener 直接覆盖沪深股票，擅长财务预测、估值、行业横比、预期修正和事件日历。两者的数据均带有商业数据供应商依赖，能证明需求和信息架构，不能证明数据可以免费复用。
3. **“结论为何变化”是最有价值的差异化。** Simply Wall St 的 Narrative 更新与公允价值历史、Seeking Alpha 的文章评级历史、TipRanks 的分析师动作历史、MarketScreener 的多窗口预期修正都在回答同一问题：观点何时、因何、基于什么证据发生改变。
4. **分析师预期必须做成“覆盖感知”能力。** 展示覆盖人数、统计口径、分歧、修正方向、数据日期和来源授权；覆盖稀疏时自动降级，不能把少量或陈旧预期包装成可靠共识。
5. **AI 摘要只能位于证据层之上。** Seeking Alpha 明示其 AI 报告可能出错且未经编辑审核；可借鉴的是“限定语料、自动更新、明确日期、保留来源与风险提示”，不是生成一个不可追溯的买卖答案。
6. **AShareRadar 已有较好的本地底座。** 现有 [REQUIREMENTS](../REQUIREMENTS.md) 与 [DESIGN](../DESIGN.md) 已包含本地自选、笔记、提醒、估值/同业、证据链、研究问答和可选 LLM。下一步应把这些能力收束为“证据快照 -> 结构化结论 -> 变化台账 -> 事件复核”，不应另建一套平行研究中心。

## 2. 标记与判断口径

- `核心`：产品官方资料把它放在常规个股研究或持仓监控主路径中；**不等于免费**。
- `免费受限`：可试用或可查看少量内容，但有报告数、标的数、历史长度、导出或高级字段限制。
- `付费`：官方明确要求某个订阅层级或附加订阅。
- `授权`：依赖 S&P Global、FactSet、Morningstar、Reuters、Quodd 等商业数据/内容，不能从竞品页面抓取后用于本地再分发。
- `部分`：能完成相邻任务，但不是完整能力，例如有评级变化提醒而没有用户投资论点版本管理。
- `未核验`：截至调研日，官方公开功能页没有足够证据；不以第三方评测补全。

## 3. 功能矩阵

| 能力 | 跨产品判断 | Simply Wall St | Stock Rover | Seeking Alpha | TipRanks | MarketScreener | 对本地 A 股的判断 |
|---|---|---|---|---|---|---|---|
| 财务健康 | **主流核心 5/5** | `核心/免费受限` Snowflake 的五维之一；区分金融与非金融企业，展示负债、流动性、利息保障、现金跑道等检查 | `核心/付费` 财务强度行业分位、十年趋势、同业排名与 Investor Warnings | `核心/付费` 三表、债务与盈利指标、Profitability/Quant grades；Premium 提供十年财务 | `核心/分层` 三表、债务/资产、现金流、利润趋势；Ultimate 才可导出部分明细 | `核心/免费受限` Financial Health 由 leverage 与 gearing 等组成，可下钻至财务报表和比率 | 做 6-8 个可解释检查即可；原值、同比/趋势、报告期和缺失原因必须同屏 |
| 估值 | **主流核心 5/5** | `核心/免费受限；高级付费` DCF/DDM/Excess Return、同业/行业倍数、Fair Ratio、分析师目标价 | `核心/付费` Fair Value、Margin of Safety、历史估值区间、Football Field；主要在 Premium Plus+ | `核心/付费` Value Grade 同时比较行业和自身历史，纳入 forward 指标 | `核心/分层` 估值比率、分析师目标价和 AI 估值摘要；Premium 解锁关键预测字段 | `核心/免费受限` P/E、P/B、EV 系列、股权/企业价值综合评分及未来年度估值 | 优先“历史分位 + A 股同行 + 可选预期锚”；DCF 只在输入完整时显示情景区间 |
| 同业比较 | **主流核心 5/5** | `核心` 自动选 peers，可编辑；比较关键倍数、行业和公平倍数 | `核心/付费` vs Peers 覆盖估值、增长、公允价值、财务强度、回报和回撤 | `核心/付费` Peers/Key Stats；免费少量，Premium 最多比较 20 个并带三类评级 | `核心/分层` 最多比较 10 只股票；Premium 增加目标价和 Smart Score，AI 报告含 peer section | `核心` 个股页直接提供行业财务比较、行业估值、评级、共识与修正 | 同行集合本身也是证据：展示行业口径、样本数、剔除原因和用户可替换项 |
| 分析师预期与证据溯源 | **核心，但高度授权依赖** | `核心/授权` S&P Global 共识；有覆盖数、目标价范围、历史目标价和分歧信心，但通常不是逐份研报溯源 | `核心/付费/授权` 共识、EPS/收入趋势和修正；Ultimate 提供 individual analyst detail，北美股票还可跳 SEC filings | `核心/付费/授权` Quant、SA 作者、Wall Street 三轨；作者评级可回到文章和历史表现，卖方数据来自第三方 | `核心/付费/专有` 逐分析师、机构、动作、日期、目标价、历史成功率与单股表现，是五家中最强的“观点责任链” | `核心/授权` 覆盖人数、预测、分歧、惊喜、7 日/1 月/4 月/1 年修正；底层由商业供应商支持 | 只接合法 API/许可数据；无许可时用公司指引、业绩快报和公告事实替代“卖方共识” |
| 投资清单 | **主流核心 5/5** | `核心/分层` Watchlist、Portfolio、Screeners；免费层有数量限制 | `核心/付费` Watchlists、Portfolios、Screeners、tags/colors；免费层仅基础个股研究 | `核心/分层` Portfolios、Holdings、Watchlist、Screeners 和提醒；高级评级/同步属 Premium | `核心/分层` Watchlist、Smart Portfolio、Screeners；高级字段与专家跟随按套餐限制 | `核心/分层` My Lists、watchlists、虚拟组合和主题清单；容量按 Free/Access/Premium/Expert 递增 | 保留“关注理由、研究状态、下次复核日、触发条件”，不扩成选股社区或荐股组合 |
| 研究笔记 | **常见 4/5** | `付费` Stock Notes；Narrative 可记录催化、预期、风险和自定公允价值 | `付费` ticker notes、comments、tags 和可配置外部 research links | `付费` Premium/PRO 可在组合或个股页记笔记，并在 My Notes 集中管理 | `部分/分层` Smart Portfolio 的 holding detail 可加 notes | `未核验` 官方套餐页支持列表、组合和 PDF 报告，但未找到等价的站内研究笔记证据 | 现有本地笔记应升级为结构化 Thesis：结论、证据、反证、失效条件、目标复核日 |
| 组合风险 | **主流，但非单股首要 5/5** | `核心/分层` 加权 Snowflake、风险/回报、地域/行业/持仓分散、真实收益 | `核心/付费` 贡献、Sharpe、beta、波动、最大回撤、相关性、模拟与再平衡 | `付费` Portfolio Health Score、聚合 Quant、警告、收益和券商同步 | `核心/分层` 行业/地域/市值/股息/β/P/E 暴露、波动与风险警告；另有 AI Portfolio Analyzer | `分层` watchlist 可按组合看热力、行业暴露、ratings radar、scatter、ESG 和前瞻日历 | 单股产品只需显示“该股对自选/持仓的集中度与事件暴露”；暂不做优化器和蒙特卡洛 |
| 事件日历 | **主流核心 5/5** | `部分` 以 Portfolio/Watchlist 更新流和邮件承载业绩、分红、预期修正、目标价、风险、并购等，不是强日历界面 | `核心/分层` Dashboard 有当月 earnings calendar，完整日历可按 portfolio/watchlist/市场筛选 | `核心/分层` 全市场 Earnings Calendar；组合 Earnings view 显示即将公布日期并可提醒 | `核心` Earnings、Dividend、Economic、IPO、Split、Buyback 等日历；组合显示业绩和除息事件 | `核心/分层` Company Calendar、宏观日程，watchlist 有前瞻业绩与事件日历 | 做“单股事件队列”而不是全球日历：公告、财报、股东会、解禁、分红、回购、停复牌、监管问询 |
| 研究结论变化跟踪 | **高价值差异化** | `强` Narrative Update 时间线 + Fair Value History；关注后收到观点变化 | `部分` 分析师动作、目标价/预期变化、提醒历史和定期组合报告；没有完整用户 thesis versioning | `强/付费` 作者评级与文章表现历史、Quant/评级升降提醒；PRO 有集中升降级，Alpha Picks 另有 Sell alerts | `强/付费` 分析师 initiated/upgraded/downgraded/reiterated 及专家跟随提醒；用户 thesis 版本较弱 | `强/授权` 收入/EPS/目标价/推荐在多时间窗的变化，watchlist 提醒估计修正和券商推荐 | 应作为 P0：保存“旧结论、新结论、变更字段、触发事件、证据 ID、时间、规则/模型版本” |
| AI 摘要 | **新兴、通常付费 2/5 明确** | `未核验为摘要` 条款已出现 Conversational AI/AI Chat，但公开功能页未明确套餐和可核验的研究摘要契约 | `未核验` 官方总览、帮助与套餐页未列原生 AI 研究摘要 | `付费/明确` Premium 的 Summary/Virtual Analyst Report 与 Earnings Call Insights；限定站内内容，但官方明确提示未经编辑审核、可能有错；Ask SA 属 PRO | `付费/明确` AI Stock Analysis 汇总财务、正反因素、风险披露、同业、业绩会和公司事件；另有 AI Portfolio Analyzer | `部分` 有自动 PDF、结构化 transcript、正面内容分析与关键词排序；未找到原生生成式 AI 个股摘要的官方证据 | 仅生成“基于证据 ID 的摘要”；每句话可展开来源、日期和数据状态，规则结论保持权威 |

## 4. 产品定位、付费与授权边界

| 产品 | 主流核心与差异化 | 免费/付费边界（2026-07-15） | 覆盖与授权依赖 | A 股参考价值 |
|---|---|---|---|---|
| Simply Wall St | 视觉化 Snowflake、完整公司报告、可编辑同行、Narrative/公允价值历史、组合重要更新 | Free 每月 5 份公司报告、1 个 10 持仓组合；Premium 每月 30 份、3×30；Unlimited 报告无限、5 个无限持仓组合及导出。Fair Value & Narrative、Stock Notes & Alerts 被列为付费能力 | 官方称全部公司基本面、历史、预测和治理数据来自 **S&P Global Market Intelligence**；中国市场页显示约 5,069 家公司 | **高**：单股研究信息架构与沪深覆盖都直接相关；数据许可不可照搬 |
| Stock Rover | 指标密集表格、深度同业、研究报告、财务强度、相关性/回撤/模拟、外部研究链接 | Free 只做基础个股研究；Premium 起提供组合、笔记、提醒；Premium Plus 起有完整估值图、无限评分、Research Reports；Ultimate 才有 analyst detail 和更多历史/实时能力 | 官方明确主要覆盖美国、加拿大交易所；试用期 CSV 导出也因 licensing restriction 被禁 | **低到中**：交互与风险方法可借鉴，现成标的/数据不能服务沪深 |
| Seeking Alpha | Quant/作者/Wall Street 三轨观点、文章与作者表现溯源、评级变更、组合健康、受限语料 AI 摘要 | Premium 年费官方列示为 USD 299，含 Quant、财务、比较、笔记、组合健康和 AI 报告；PRO 另含 Ask SA、Top Analysts、升降级等；Alpha Picks、Investing Groups 是独立产品 | Fundamentals、estimates、Wall Street ratings 来自 **S&P Global Market Intelligence**，行情来自 Quodd/Cboe/Nasdaq；官方禁止复制或再分发数据 | **中**：证据责任链和 AI 风险提示值得借鉴；A 股内容与授权可得性低 |
| TipRanks | 逐分析师动作与绩效、Smart Score、Smart Portfolio、丰富日历、AI Stock/Portfolio Analysis | Basic 可试用部分页面和组合；Premium 解锁最佳分析师共识、目标价、Smart Score 等；Ultimate 增加导出和更高限制。公开 Plans 页为动态页面，具体价格应以结算页为准 | 专家排名和 Smart Score 为 TipRanks 专有数据；机构/API 单独销售。当前 Stock Screener 市场列表含香港但不含沪深 | **中偏低**：分析师证据模型极强，但大陆 A 股数据入口不足、专有数据不可复制 |
| MarketScreener | 全球公司页、财务/预测/估值/行业一体化、预期修正、强筛选器、列表组合化分析和公司日历 | Free 有限；Access 提供完整新闻、共识、五年财务、transcripts/日历；Premium 增加组合、300 filters、研究和无限 PDF；Expert 增加 600+ filters、十年财务和自定义报告 | 官方称覆盖 60+ 交易所、70,000+ 证券，使用 S&P Global、FactSet、Morningstar、MSCI 及新闻线；可直接核验上海、深圳个股页 | **高**：是 A 股财务/估值/修正/事件布局的直接对标；商业数据只能作为需求证明 |

### 授权红线

1. 不抓取或缓存竞品的 S&P/FactSet/Morningstar/Reuters/Quodd 数据、评级、研报、transcript 或 AI 成品用于产品展示。
2. 不把“竞品页面可看到 A 股”误写成“该数据可以免费获得”。UI 模式可借鉴，数据必须来自 AShareRadar 已有合法 provider、交易所/公司官方披露或新增正式授权。
3. 分析师姓名、机构、目标价、逐次修正和全文观点若无授权，不应上线。可先提供公司业绩指引、公告事实、实际值 vs 可合法获得的一致预期。
4. 外部文章与公告优先保存 URL、标题、来源、发布时间、抓取时间和内容哈希；全文存储、翻译、摘要均需满足来源条款。

## 5. 关键用户流程

| 产品 | 官方主路径的抽象 | 最值得借鉴的环节 | 本地化时应删减 |
|---|---|---|---|
| Simply Wall St | 搜索股票 -> Snowflake 快扫 -> 财务/估值/未来增长下钻 -> 编辑同行/设置公允价值 -> 写 Narrative -> 加 Watchlist/Portfolio -> 接收事件和观点更新 | “一屏总览不替代下钻”；个人公允价值与 thesis 更新拥有历史轨迹 | 社区投票、公开 Narrative、全球发现页 |
| Stock Rover | 选择 watchlist/portfolio -> 表格横比 -> Insight/Peers -> on-demand Research Report -> notes/research links -> alerts/calendar -> portfolio analytics | 同一标的从表格、详情、报告、笔记和外部原文之间低摩擦跳转 | 北美 SEC 专属链路、重型模拟/再平衡、多券商同步 |
| Seeking Alpha | Symbol Summary/Quant -> Financials/Valuation/Peers -> 对照 Quant/SA Authors/Wall Street -> 阅读文章/transcript/AI report -> 记笔记/入组合 -> 接收 rating/earnings alerts | “结论 -> 作者/文章 -> 发布时股价 -> 后续表现”的责任链；AI 明示语料边界和风险 | 付费内容市场、社交评论、Alpha Picks/PRO 模型组合 |
| TipRanks | Analyst Forecasts -> 过滤 top analysts -> Smart Score/Financials/AI -> Compare -> Smart Portfolio notes -> expert/event alerts | 逐条展示分析师动作、机构、时间、目标价、个人总体与单股历史表现 | 专家排行榜、对冲基金/内部人跟随、缺乏沪深覆盖的专有评分 |
| MarketScreener | A 股 company page -> Financials/Valuation -> Consensus/Revisions -> Sector comparison -> PDF/report -> Watchlist -> forward calendar/alerts | 预测值、估值、同行、修正窗口和日历在同一股票上下文内；A 股页面可直接验证 | 600+ 条件全球筛选、媒体资讯聚合、编辑部组合与高价 Expert 工具 |

### 建议的本地单股闭环

1. **进入股票：** 先显示数据日期、来源状态、财务健康、估值区间、同行位置、近期事件和当前研究结论。
2. **下钻证据：** 每项判断展开到原始指标、报告期、计算方法、同行样本和公告/数据源链接；缺数据时显示缺失原因而非中性分。
3. **形成论点：** 用户填写结论、催化剂、关键假设、反证、失效条件、公允价值区间和下次复核日；系统绑定当时证据快照。
4. **加入清单：** 保存关注理由、研究状态（待研究/观察/持有/排除）和触发条件，不只保存股票代码。
5. **事件触发复核：** 财报、公告、估值越界、预期变化或风险规则命中时创建复核任务，展示“自上次结论后发生了什么”。
6. **提交新结论：** 用户或确定性规则明确确认是否改变结论；保存 diff、理由、证据和版本。AI 只能草拟摘要，不能静默改写权威结论。

## 6. 适合本地 A 股单股研究的精选能力

排序公式：`优先分 = 2 × 收益 + (6 - 实施成本) + 数据可得性`。三项均为 1-5 分；收益/数据越高越好，成本越低越好，满分 20。分数用于当前本地单股场景的相对排序，不代表投资效果。

| 排名 | 精选能力 | 收益 | 成本 | 数据 | 优先分 | 最小可交付定义 | 主要借鉴 |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | 统一证据卡与新鲜度契约 | 5 | 1 | 5 | **20** | 所有结论带 `source/as_of/period/value/unit/method/evidence_id/missing_reason`，一键回到公告或数据来源；沿用现有 evidence chain 和 data quality | 五家共同下钻模式；Seeking Alpha/TipRanks 的责任链 |
| 2 | 研究结论变化台账 | 5 | 2 | 5 | **19** | 保存旧/新结论、变化字段、触发事件、理由、证据 ID、规则/模型版本和时间；提供“自上次研究以来”视图 | Simply Wall St Narrative；SA ratings；TipRanks actions；MarketScreener revisions |
| 3 | 可解释财务健康检查卡 | 5 | 2 | 5 | **19** | 偿债、流动性、现金流、盈利质量、应收/存货、稀释等 6-8 项；金融行业使用独立规则；显示趋势与原值 | Simply Wall St、Stock Rover、MarketScreener |
| 4 | 事件驱动复核队列 | 5 | 2 | 4 | **18** | 单股财报/快报/公告/分红/回购/减持/解禁/问询等事件进入时间线，并自动关联受影响 thesis 字段 | 五家事件与 watchlist 工作流 |
| 5 | 结构化 Thesis 笔记与清单理由 | 4 | 1 | 5 | **18** | 在现有 notes/watchlist 上增加催化、假设、反证、失效条件、公允价值区间、状态和复核日；保留普通自由文本 | Simply Wall St Narrative；Seeking Alpha/Stock Rover notes |
| 6 | 三锚估值视图 | 5 | 3 | 4 | **17** | 历史 PE/PB 分位 + 可解释同行分位 + 可选预测/情景估值；输入不足不输出 DCF 单点 | Simply Wall St、Stock Rover、MarketScreener |
| 7 | 有证据边界的 AI 研究摘要 | 4 | 3 | 4 | **15** | 只读当前股票的有限证据集合，输出事实/推断/缺口/风险四段；每句绑定 evidence ID；失败回退规则文本 | Seeking Alpha、TipRanks；沿用现有可选 LLM 安全边界 |
| 8 | 同行选择与差异解释 | 4 | 3 | 4 | **15** | 展示默认同行、行业/规模/盈利状态匹配理由、样本数与剔除原因，允许临时替换但不污染默认规则 | Simply Wall St edit peers；Stock Rover/MarketScreener peer tables |
| 9 | 覆盖感知的预期修正层 | 4 | 4 | 2 | **12** | 有授权时展示覆盖数、均值/中位数、分歧、7/30/120 日修正和更新时间；覆盖不足明确降级 | MarketScreener、TipRanks、Stock Rover |
| 10 | 重型组合风险与优化 | 2 | 4 | 3 | **9** | 当前只保留持仓集中度、行业暴露和单股事件风险；相关矩阵、模拟、再平衡后置 | Stock Rover、TipRanks |

### 推荐交付顺序

- **P0：** 统一证据卡、结论变化台账、财务健康检查、事件复核队列。
- **P1：** 结构化 Thesis、三锚估值、同行选择解释。
- **P2：** 有授权的预期修正、证据约束 AI 摘要。
- **后置：** 组合优化、券商同步、社区和模型荐股。

## 7. 不应添加的能力

| 不应添加/当前不做 | 原因 |
|---|---|
| 黑箱“万能总分”或直接 Buy/Sell 按钮 | A 股行业差异、财务制度、数据缺失和小样本会制造虚假精度；总分只能导航，不能替代证据 |
| 抓取竞品分析师共识、研报、transcript、AI 报告 | 明确涉及第三方数据和内容授权，且 Seeking Alpha 官方明确禁止复制/再分发 |
| 券商账户连接、自动交易和下单 | 超出本地单股研究定位，增加凭证、隐私、合规、对账和交易事故风险；现有需求也明确排除自动交易 |
| 分析师/用户排行榜、公开 Narrative 社区、跟单 | 需要身份、审核、绩效口径、利益冲突披露和内容治理，成本远高于本地研究收益 |
| Alpha Picks、PRO Quant Portfolio 式模型荐股 | 从“辅助研究”滑向产品背书和组合建议，还依赖完整回测、费用、流动性和幸存者偏差治理 |
| Stock Rover 式蒙特卡洛、相关矩阵和自动再平衡全家桶 | 当前是单股研究工具，投入大、数据要求高，会稀释主路径；只保留轻量持仓暴露即可 |
| MarketScreener 式 600+ 条件全球筛选器 | 形成配置和认知负担；A 股本地产品先围绕少量可解释条件与单股复核闭环 |
| 无引用的“AI 一键研报”或 AI 静默改变结论 | 不能区分事实与推断，也无法回答结论为何变化；AI 必须引用证据并受确定性结果约束 |
| 在覆盖不足时补造分析师预期、目标价或 DCF | 缺失本身是研究信息；应显示覆盖不足、输入假设和敏感性，不应用默认值填满页面 |
| 复制第三方文章全文并长期保存 | 存储、翻译、摘要与展示都可能受版权/合同限制；优先元数据、链接和必要的事实摘录 |

## 8. 官方来源

以下链接均为产品官方域名，最后核验日期为 **2026-07-15**。

### Simply Wall St

- [Snowflake 五维与计分方式](https://support.simplywall.st/hc/en-us/articles/360001740916-How-does-the-Snowflake-work)
- [Financial Health 方法](https://support.simplywall.st/hc/en-us/articles/9812782597135-Understanding-The-Financial-Health-Section)
- [Valuation、同行选择、DCF 与目标价历史](https://support.simplywall.st/hc/en-us/articles/4751563581071-Understanding-the-Valuation-section-in-the-company-report)
- [财务、预测与分析师数据来源](https://support.simplywall.st/hc/en-us/articles/8908651462543-Where-do-you-source-financial-data)
- [Narrative、公允价值历史与更新](https://support.simplywall.st/hc/en-us/articles/13416018054415-Getting-Started-with-Narratives)
- [Portfolio returns、Snowflake 与 diversification](https://support.simplywall.st/hc/en-us/articles/9423775242383-Understanding-the-Portfolio-Returns-Analysis-Calculations)
- [Portfolio/Watchlist 重要事件通知](https://support.simplywall.st/hc/en-us/articles/7563299190159-What-important-updates-and-events-are-being-sent-to-me)
- [套餐与功能限制](https://simplywall.st/plans)
- [中国市场覆盖页](https://simplywall.st/markets/cn)
- [Conversational AI 条款](https://simplywall.st/terms-and-conditions)

### Stock Rover

- [官方产品总览与北美覆盖](https://www.stockrover.com/help/stock-rover-overview/)
- [Financial Strength 行业分位与同行](https://www.stockrover.com/help/stock-ratings/stock-ratings-financial/)
- [Insight：分析师、财报、SEC filings 与 vs Peers](https://www.stockrover.com/help/the-insight-panel/additional-default-tabs/)
- [Analyst Ratings 动作与后续回报](https://www.stockrover.com/help/analyst-ratings/analyst-ratings-overview/)
- [Research Reports 范围与订阅边界](https://www.stockrover.com/help/research-reports/overview/)
- [Notes、Research Links 与 row actions](https://www.stockrover.com/help/the-table/row-action-menu/)
- [Portfolio correlation](https://www.stockrover.com/help/correlation/correlation-overview/)
- [Dashboard、future income 与 earnings calendar](https://www.stockrover.com/help/dashboard/whats-in-the-dashboard/)
- [套餐、价格与 licensing restriction](https://www.stockrover.com/plans/)

### Seeking Alpha

- [Premium 功能与 AI 工具](https://help.seekingalpha.com/what-is-seeking-alpha-premium)
- [Valuation Tab 与行业/历史比较](https://help.seekingalpha.com/premium/what-is-the-valuation-tab-and-how-do-i-use-it)
- [Key Stats/Peers comparison](https://help.seekingalpha.com/premium/how-to-compare-stock-performance-with-premiums-key-stats-comparison)
- [SA 作者评级、文章和历史表现](https://help.seekingalpha.com/premium/how-do-i-know-that-the-analysts-on-the-seeking-alpha-sites-articles-are-credible)
- [Wall Street ratings 的来源与聚合](https://help.seekingalpha.com/premium/what-are-wall-street-analyst-ratings-on-seeking-alpha)
- [Portfolio Tracker 与 Health Score](https://help.seekingalpha.com/what-are-the-key-features-of-seeking-alphas-portfolio-tracker)
- [Portfolio/stock notes](https://help.seekingalpha.com/premium/how-can-i-add-notes-to-my-portfolio)
- [Summary Report 的输入语料](https://help.seekingalpha.com/what-content-is-the-virtual-analyst-report-based-on)
- [AI 报告未经编辑审核与错误提示](https://help.seekingalpha.com/does-seeking-alpha-use-ai-to-generate-these-reports)
- [Earnings Call Insights 的 AI 摘要](https://help.seekingalpha.com/what-are-the-seeking-alpha-earnings-calls-insights-articles)
- [组合 Earnings Calendar](https://help.seekingalpha.com/basic/where-can-i-find-the-earnings-calendar-for-the-stocks-in-my-portfolio)
- [市场数据供应商与禁止再分发说明](https://help.seekingalpha.com/basic/where-do-you-source-your-market-data-from)

### TipRanks

- [逐股比较工具与支持市场](https://www.tipranks.com/compare-stocks)
- [公司财务、财务健康图表与 Ultimate 导出](https://www.tipranks.com/news/labs/discover-company-financials-on-tipranks)
- [分析师排名方法](https://www.tipranks.com/glossary/h/how-analysts-are-ranked)
- [逐分析师预测、动作、目标价和单股历史](https://www.tipranks.com/news/labs/put-the-best-wall-street-analysts-to-work-for-you-with-tipranks-price-targets-and-analyst-ratings)
- [Smart Portfolio 分析、notes、事件与套餐边界](https://www.tipranks.com/news/labs/the-smartest-way-to-analyze-your-portfolio-just-got-smarter)
- [Smart Portfolio 风险与暴露](https://www.tipranks.com/smart-portfolio/analysis/overview)
- [AI Stock Analysis 内容范围](https://www.tipranks.com/news/labs/introducing-stock-ai-analysis-smarter-insights-faster-decisions)
- [AI Portfolio Analyzer](https://www.tipranks.com/news/labs/tipranks-introduces-ai-portfolio-analyzer)
- [当前 Stock Screener 市场列表](https://www.tipranks.com/screener/stocks)
- [官方套餐页](https://www.tipranks.com/plans)

### MarketScreener

- [全球 Stock Screener、财务健康、估值、共识和修正定义](https://www.marketscreener.com/stock-screener)
- [套餐、数据供应商、watchlists、组合分析与报告](https://www.marketscreener.com/services/solutions/)
- [全球股票覆盖列表](https://www.marketscreener.com/stock-exchange/shares/)
- [上海电气 A 股页面：财务、估值、共识、修正、日历与行业入口](https://www.marketscreener.com/quote/stock/SHANGHAI-ELECTRIC-GROUP-C-6500348/)
- [深圳美盛 A 股页面示例](https://www.marketscreener.com/quote/stock/SHENZHEN-MASON-TECHNOLOGI-20706744/)

## 9. 最终建议

AShareRadar 不需要复制任何一家竞品的“大而全”。最佳组合是：**Simply Wall St 的可解释单股摘要与 thesis 版本、MarketScreener 的 A 股财务/预期/日历组织、TipRanks/Seeking Alpha 的观点责任链，再用现有本地证据与规则系统约束 AI。** 产品验收标准应从“多了几个卡片”改为：用户能在一分钟内回答当前结论、关键证据、主要反证、下一事件，以及结论自上次为何变化。
