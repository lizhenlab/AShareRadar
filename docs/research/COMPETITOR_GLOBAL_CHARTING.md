# 全球图表与市场扫描产品竞品调研

- 调研日期：2026-07-15
- 调研对象：TradingView、Finviz、Koyfin、StockCharts（补充样本）
- 产品语境：AShareRadar 是本地、单用户、A 股研究工作台，不是交易终端，也不承诺交易所级实时行情。
- 资料口径：只采用厂商官方产品页、帮助中心和官方更新日志。文中的“未证实”表示截至调研日未在官方公开资料中找到对应能力，不等同于断言产品绝对没有该能力。

## 1. 结论摘要

1. **最值得借鉴的不是更多指标，而是闭环。** 四类领先工作流都在缩短“市场概览/筛选 -> 观察列表 -> 图表核验 -> 预警 -> 复盘”的路径。AShareRadar 当前已有单股图表、分钟/日线分析、图表标记、自选、预警、数据质量和历史信号复盘，下一步应把这些孤立能力连接起来。
2. **TradingView 是图表交互上限，不应成为功能范围模板。** 其 1-16 图布局、周期/标的同步、指标与绘图对象、事件标记、跨设备预警和逐 bar 回放很强，但完整复制会把产品推向通用交易终端。
3. **Finviz 的高价值在扫描吞吐量。** 2026 年新增筛选结果与 Advanced Charts 并排、多周期 Magnifying Glass、跨页面图表模板及一键图表价格预警，核心是让用户连续浏览候选，而不是反复切换页面。
4. **Koyfin 的高价值在研究数据的可复用组织。** 历史财务指标、公式、观察列表视图、筛选器、图表模板和联动仪表盘共享同一套研究对象；它也证明研究产品可以有意放弃分钟级交易终端深度。
5. **StockCharts 展示了“规则一次定义，多处复用”。** 同一技术扫描语言可用于筛选、观察列表扫描和定时预警，扫描结果又可写回 ChartList。这比为筛选器、预警和复盘分别发明规则更适合 AShareRadar。
6. **数据新鲜度必须是一等产品信息。** TradingView 显示延迟状态，Koyfin 暴露最后成交时间，StockCharts 同时显示最新数据时间与来源颜色；AShareRadar 应统一展示市场时间、抓取时间、来源、实时/延迟/收盘/缓存状态、复权口径和样本完整度。
7. **建议优先级：** P0 做统一新鲜度契约、双周期联动图、筛选到自选到逐只看图、可配置自选视图；P1 做事件层与统一预警、A 股行业/概念热力图；P2 再做严格防未来函数的逐日回放和移动端复盘增强。

## 2. AShareRadar 现状与研究边界

依据仓库当前 [需求说明](../REQUIREMENTS.md) 与 [设计说明](../DESIGN.md)，AShareRadar 已具备：

- 日 K 与 `1m/5m/15m/30m/60m` 分钟分析、均线和支撑/压力；
- 笔记、事件、规则命中形成的图表标记；
- 本地自选、价格/涨跌幅/趋势预警及触发/恢复事件；
- 单股历史信号统计与案例复盘，但尚非逐 bar 可视回放；
- 行情来源、缓存、数据质量、降级原因和系统诊断；
- 市场概览与有限样本强弱排序，但尚非完整市场筛选器和热力图。

因此本报告把“已有”能力视为增强基础，不建议重建。重点差距是跨周期联动、全市场发现入口、对象间跳转、状态持久化、统一时间语义和移动端任务裁剪。

## 3. 产品定位对照

| 产品 | 主导入口 | 典型闭环 | 对 AShareRadar 最有价值的启示 | 主要结构性限制 |
| --- | --- | --- | --- | --- |
| TradingView | 图表优先 | 图表/热力图/筛选 -> 自选 -> 条件预警 -> Bar Replay | 同标的多周期上下文、图表对象同步、事件与预警直接附着图表 | 功能面极宽；交易所行情、Pine 生态和多端同步成本高 |
| Finviz | 扫描优先 | Screener/Map -> 批量看图 -> Portfolio -> 筛选/价格预警 | 候选列表与图表同屏、模板跨入口复用、极低切换成本 | 主要覆盖美国市场；高级数据和交互集中在 Elite |
| Koyfin | 研究工作台优先 | Watchlist/Screen/Dashboard -> 历史图与财务 -> 文档/价格预警 | 表格视图、历史指标、公式、模板和仪表盘共享研究上下文 | 历史图官方说明最短为日频，不以分钟交易和 Bar Replay 为核心 |
| StockCharts | 技术列表/扫描优先 | Scan -> ChartList -> 多图检查 -> 同规则 Alert | 筛选、列表和预警共用规则语言；扫描结果可持续回写列表 | 市场覆盖有限；移动端偏伴侣；事件基本面深度较弱 |

## 4. 功能矩阵

评级含义：**强** = 官方资料显示能力成熟且处于主流程；**中** = 已支持但范围较窄或以邻近能力实现；**弱/未证实** = 仅有替代路径，或没有找到官方公开说明。

### 4.1 图表、指标、事件、回放与新鲜度

| 能力 | TradingView | Finviz | Koyfin | StockCharts |
| --- | --- | --- | --- | --- |
| 多周期图表 | **强**：每布局 1-16 图；可按组同步标的和周期；回放也可跨图同步。[TV-01][TV-02][TV-09] | **强**：Intraday 与 Multi-layout；2026-06 的 Magnifying Glass 可在原图内检查另一尺度，并展示同标的日/1 分钟/月等多图。[FV-01][FV-03] | **中**：Historical Graph 可拆分/合并多面板并联动 Dashboard，但官方说明最短周期为日，不支持分钟或小时 candle。[KY-01][KY-08] | **强**：ACP 最多 12 图；示例 GalleryView 同时展示 intraday/daily/weekly/monthly，可批量应用设置。[SC-01] |
| 指标与叠加 | **强**：指标可位于价格层或独立 pane，可套用模板/全布局；Compare 可叠加多标的并切换百分比尺度。[TV-01][TV-03] | **强**：高级图支持技术研究、叠加、绘图和比较；2026 图表模板可保存图型、指标、颜色、事件、延长时段等并跨 Screener/Stock Page 使用。[FV-01][FV-04] | **强（研究型）**：可叠加价格、成交量、均线、估值及数百种基本面序列，支持多标的、转换、注释和模板。[KY-01] | **强（技术型）**：会员每图可加最多 25 个 indicator 和 25 个 overlay，并支持指标面板上的叠加。[SC-02] |
| 事件标记 | **强**：财报以 E 图标锚定报告日并表达 surprise；事件面板可控制显示。[TV-04] | **强**：评级变更锚定精确 candle；图表还可显示财报 beat/miss 与股息，并纳入模板。[FV-04][FV-05] | **中**：财报/经济日历、财报 surprise 与 1 日价格反应、股息日程完整，但未证实其作为通用 candle 标记层。[KY-05][KY-06] | **中偏弱**：财报日历可直接跳图或建预警；未证实通用财报/新闻 candle 标记层。[SC-09] |
| 复盘/回放 | **强**：可选历史起点、自动或单步前进、调速、全布局同步；指标和绘图保留，但新建预警、非标准图型等受限。[TV-09] | **中**：有技术指标 Backtests 和较长历史数据，但未证实逐 bar 可视回放；多周期放大镜是检查工具，不是回放。[FV-01][FV-10] | **弱/未证实**：可做历史图、财报反应和组合历史分析，未见价格逐 bar 回放的官方说明。[KY-01][KY-06] | **中偏弱**：可指定历史结束日并用日期滑块逐日移动，另有静态历史图廊；未证实通用自动 Bar Replay。[SC-07][SC-10] |
| 数据新鲜度展示 | **强**：图表状态行明确显示 `Data is delayed`，市场状态可追溯交易所和所需数据包。[TV-10] | **中**：官方明确免费股票延迟 1 分钟、Elite 股票实时、期货延迟 20 分钟；这是清晰的产品/品类级披露，但未证实每个组件均展示 last-trade 时间。[FV-01][FV-09] | **强**：官方按地区区分 live/15 分钟延迟/EOD，并在 MYD ticker hover 或 quote box 展示最后成交时间戳。[KY-07] | **强**：图头显示最新数据日期/时间；绿色代表官方实时，黄色代表 BATS 实时或延迟，且文档解释最近 15 分钟的替换逻辑。[SC-06][SC-08] |

### 4.2 筛选、观察列表、预警、热力图与移动端

| 能力 | TradingView | Finviz | Koyfin | StockCharts |
| --- | --- | --- | --- | --- |
| 筛选器 | **强**：通用 Stock Screener 支持预设和即时过滤；Pine Screener 可对自选运行内置/私有/社区指标，但每次 screen 仅一个指标。[TV-05][TV-06] | **强**：基本面、技术面、Signal、Pattern 与自定义范围/预设；2026 起可在 Advanced Charts 侧栏直接浏览 Screen。[FV-02][FV-06] | **强**：覆盖 10 万+全球证券、5,900+ 条件；可按历史期、历史均值和自定义公式筛选，并导出到自选。[KY-02][KY-03] | **强（技术型）**：Scan Engine 支持 AND/OR 技术条件、形态和 universe 条件；预定义报告盘中每几分钟更新。[SC-03][SC-04] |
| 观察列表 | **强**：分组、排序、自定义列、技术/基本面/新闻/笔记；高级视图有绩效、风险、财报、股息与汇总统计。[TV-07] | **中强**：以 Portfolio/Watchlist 组织标的，可预览图表、写持仓/交易笔记，并支持较大列表；研究入口更偏 Portfolio。[FV-01][FV-11] | **强**：列、分组、多重排序、汇总行、公式、可复用 View、实时 Watchlist News、分享，并与 Dashboard/Screen 互通。[KY-03][KY-04] | **强**：ChartList 每组最多 1,000 图；既是观察列表，也是 Scan/Alert universe 和扫描结果容器。[SC-04][SC-05] |
| 预警 | **强**：价格、指标、策略、绘图、形态及整张自选；服务端运行，支持 App、桌面、邮件、Webhook 等。[TV-08] | **强**：价格、新闻、评级、内幕、SEC 文件、Portfolio 和“新标的符合 Screener”；2026 支持图上价格轴一键建预警。[FV-01][FV-07] | **强**：价格、估值、技术指标、新闻/公告/财报电话会/文件；桌面、邮件和移动 Push，多类全球标的覆盖不同。[KY-09] | **强**：简单价格或高级扫描条件；可持续/开盘/小时/收盘运行，经邮件、短信或站内通知，约每 3-5 分钟处理。[SC-13] |
| 市场热力图 | **强**：股票 Heatmap 支持分组、大小、颜色、标签和全屏。[TV-11] | **强**：经典 Maps 支持多个指数、月内表现和内幕交易等维度；Elite 有实时与盘中周期，2025 后优化响应式布局。[FV-08] | **中（邻近能力）**：未证实通用 treemap；Market Scatter 与回归线承担横截面比较。[KY-10] | **强**：MarketCarpet 以颜色方格扫描市场或 ChartList，带排行和 hover mini-chart。[SC-11] |
| 移动端交互 | **强**：原生移动 App 支持图表、跨设备预警和 Bar Replay 周期选择，核心能力延续到手机。[TV-08][TV-12] | **中偏弱**：官方 Heatmap 有响应式设计、Elite 提供 push；截至调研日未找到原生 App、完整移动工作流或触控图表手势的官方说明。[FV-01][FV-08] | **强**：iOS/Android 为桌面精简版，支持图表、市场概览、自选双向同步、组合、新闻和价格/公告 Push。[KY-11][KY-12] | **中**：官方 iPhone/iPad 伴侣 App 可看市场、ChartList 和临时图表；未见 Android 或完整 ACP 编辑说明。[SC-12] |

## 5. 共性产品模式

### 5.1 从“页面集合”转为“研究对象流”

领先产品让同一个标的集合在筛选器、观察列表、图表、预警之间流动：Finviz 把 Screen 放进图表侧栏；Koyfin 可把 Screen 导出到 Watchlist 并复用 View；StockCharts 的 Scan 结果直接写入 ChartList；TradingView 可扫描 Watchlist 并对整表创建预警。用户保存的是集合、条件、视图和注释，而不是一次性页面状态。

### 5.2 多周期的价值是保留上下文

共同目标不是堆更多图，而是查看细节时不丢失大结构。TradingView 用同步多图，Finviz 用跟随光标的 Magnifying Glass，StockCharts 用固定多周期 Gallery，Koyfin 用联动 Dashboard。AShareRadar 无需复制 12/16 图，应先解决“日线判断与分钟执行参考互相可见”。

### 5.3 筛选条件应可复用为预警和复盘条件

TradingView Pine Screener 能使用指标 plot/alert condition；StockCharts 扫描与高级预警共享语言；Finviz 可对 Screen 新进入标的发通知。共同模式是：用户验证过的条件不必在另一个模块重新配置。

### 5.4 事件是价格解释层，不是独立新闻页

TradingView 和 Finviz 将财报、股息或评级动作直接锚定价格时间轴。Koyfin 虽偏日历/快照，也把 surprise 与价格反应并置。事件标记必须有日期、来源、类别、状态和详情，且可以筛选/隐藏，否则密集信息会淹没价格。

### 5.5 市场总览必须能下钻

Heatmap/MarketCarpet 的价值不只是颜色，而是从板块/指数方格进入成分排行、mini-chart、完整图表或观察列表。颜色、面积、时间窗口和 universe 都需要明确，否则热力图会制造虚假的全市场代表性。

### 5.6 新鲜度是逐组件属性

“页面刚刷新”不等于“行情实时”。成熟做法至少区分市场时间、产品抓取时间、交易所/替代源、延迟类别和交易时段；StockCharts 甚至用颜色区别官方实时与可能被替换的 BATS bar。AShareRadar 已有来源和质量模型，适合把后端诊断提升为前台统一契约。

### 5.7 移动端是任务裁剪，不是桌面缩小

Koyfin 明确将移动版定义为 compact version，StockCharts 定位为 companion，Finviz 仍以 Web 为主；移动端高频任务集中在看状态、处理预警、维护自选、快速看图。完整筛选器构建、多图布局和复杂指标编辑应留在桌面。

### 5.8 回放与回测必须分开命名

TradingView 是逐 bar 视觉回放；Finviz 是技术条件回测；Koyfin 偏历史基本面与组合分析；StockCharts 是历史结束日/滑块检查。AShareRadar 当前“历史信号复盘”属于样本统计，不应在 UI 或需求中与“隐藏未来数据、逐步推进”的 Bar Replay 混称。

## 6. AShareRadar 高价值核心能力

评级：价值衡量对 A 股研究效率、判断透明度和闭环完整性的提升；成本含前后端、存储与运维；风险重点考虑数据授权、未来函数、错误预警和供应商稳定性。

| 优先级 | 核心能力与最小可行范围 | 价值 | 成本 | 风险 | 取舍依据 |
| --- | --- | --- | --- | --- | --- |
| P0 | **统一数据状态条**：每个图表/筛选结果展示 `实时/延迟/收盘/缓存/降级`、行情源、市场时间、抓取时间、数据年龄、交易时段、复权口径和样本完整度；详情可展开，默认保持紧凑。 | 高 | 低-中 | 中 | 直接复用现有 provider、cache、quality、warning 模型；最大风险是不同来源时间语义不一致。 |
| P0 | **日线 + 单一分钟周期联动图**：桌面双 pane、同标的、同步十字线/可视区和事件；分钟周期用 `5/15/30/60m` 分段控件切换。移动端只显示一个 pane，并用周期切换保留状态。 | 高 | 中 | 低-中 | 捕获 TradingView/Finviz 的上下文价值，同时避免 12/16 图布局和任意 pane 编辑器。 |
| P0 | **预设式 A 股筛选 MVP**：从可审计字段开始（数据质量、趋势、均线位置、量比/成交额、涨跌幅、相对强弱、风险、事件），显示 universe、命中数、缺失数、截至时间；支持排序、保存到自选、上一只/下一只看图。 | 高 | 中-高 | 中-高 | 形成发现入口；必须先有完整度和失败语义，不能把有限采样包装成全市场扫描。 |
| P0 | **自选 View 与“入选原因”**：可保存列组合、排序、分组/标签；默认提供“今日异动、趋势、风险、数据状态”视图；每只股票保留由哪个筛选/规则加入及加入时间。 | 高 | 低-中 | 低 | 借鉴 Koyfin/StockCharts，让当前 CRUD 自选升级为研究队列，不需要新数据授权。 |
| P1 | **统一条件模型：筛选 = 预警 = 复盘输入**：先支持受控字段、比较符、AND 条件组、周期和数据质量门槛；同一条件可“立即扫描”“持续预警”“历史验证”。 | 高 | 中-高 | 中 | 避免三套规则漂移；需版本化计算口径，并限制复杂表达式，暂不开放任意代码。 |
| P1 | **可筛选事件层**：先整合已有笔记、异常事件、规则命中、预警触发/恢复；有稳定授权后再加业绩预告/定期报告、分红除权和公告。每个标记显示来源、事件时间、写入时间及可见性。 | 高 | 中 | 中-高 | 已有 chart marks 基础；外部公告、财报、新闻的再分发与时间准确性是主要风险。 |
| P1 | **从图/筛选/自选一键建预警**：价格轴建价位预警；筛选条件保存为自选级预警；支持冷却、恢复事件、数据质量门槛和“数据已陈旧”预警。 | 高 | 中 | 中 | 复用当前预警服务；全市场持续计算会增加供应商、调度和误报压力，应先限自选。 |
| P1 | **A 股行业/概念热力图**：面积可选自由流通市值或等权，颜色可选 `1D/5D/20D`、相对强弱或成交活跃度；明确 universe、覆盖率、截至时间；点击下钻成分排行并可加入自选。 | 高 | 中-高 | 高 | A 股用户价值高，但板块分类、完整行情、停牌/ST、成分变化和市值授权必须可追溯。 |
| P2 | **严格 as-of 逐日回放**：选择历史日后截断所有未来行情、事件、指标与规则输出；单步前进、跳回实时、保存回放笔记；首版仅日线单股，不做秒/tick 或模拟成交。 | 高 | 高 | 高 | 与现有信号样本统计互补；未来函数、复权重算、历史成分和事件回填最容易制造虚假结果。 |
| P2 | **移动端研究伴侣**：优先自选排序、状态/新鲜度、预警收件箱、单图周期切换、事件详情和快速笔记；隐藏高级筛选构建、多 pane 编辑及系统诊断细节。 | 中-高 | 中 | 低-中 | 对齐 Koyfin/StockCharts 的任务裁剪；先做响应式 Web，不以原生双端 App 为前置。 |

### 6.1 必须先统一的数据契约

1. **Snapshot 身份**：`as_of_market_time`、`fetched_at`、`source`、`freshness_class`、`session`、`adjustment_mode`、`universe_version`、`coverage_count/expected_count`。
2. **Condition Schema**：字段 ID、计算版本、周期、运算符、阈值、质量门槛、缺失值策略；筛选、预警、复盘只引用同一 schema。
3. **Chart Mark Schema**：事件时间与入库时间分离，包含来源、授权类别、可见范围、严重度、关联规则/笔记 ID。
4. **Replay Clock**：所有数据访问必须接收同一个 `as_of`，缓存键包含计算版本和复权口径；任何晚于回放时钟的数据都不得进入输出。

### 6.2 推荐实施顺序

1. 先完成统一新鲜度契约和自选 View，使现有功能可信、可扫读。
2. 再完成双周期联动图和“筛选结果 -> 自选 -> 逐只看图”，形成最短研究闭环。
3. 将受控 Condition Schema 接入自选预警和现有历史验证。
4. 在覆盖率可证明后上线行业热力图；在 `as_of` 隔离测试成熟后上线逐日回放。

## 7. 当前阶段不适合的能力

“不适合”指不应进入近期核心范围；不代表永不建设。

| 能力 | 价值 | 成本 | 风险 | 决策与原因 |
| --- | --- | --- | --- | --- |
| TradingView 式任意脚本语言、社区指标市场与公开脚本 | 中 | 高 | 高 | **不做。** 需要沙箱、资源配额、审核、版本兼容、安全响应和知识产权治理；当前内置可解释规则更符合产品定位。 |
| 交易所级全市场逐笔/Level 2、秒级全量回放 | 中-高 | 极高 | 极高 | **不做。** 数据购买与展示授权、存储吞吐、复权/撮合语义均超出本地研究工作台边界。 |
| 12/16 图自由布局、25+ 指标/叠加和复杂绘图 CAD | 中 | 高 | 中 | **不做。** 对目标用户的边际价值低于双周期联动与筛选闭环，并显著增加移动适配和状态持久化复杂度。 |
| 全球多资产与数千基本面字段筛选 | 低 | 极高 | 高 | **不做。** AShareRadar 的差异化是 A 股本地研究；Koyfin 的覆盖依赖昂贵专业数据授权和长期字段治理。 |
| 全量新闻、研报、分析师评级/一致预期、逐字稿聚合 | 中-高 | 高 | 极高 | **仅在获得明确许可后做摘要级接入。** 抓取不等于可再分发，且历史修订、来源署名和删除要求复杂。 |
| 券商连接、图表下单、模拟撮合和一键交易 | 中 | 极高 | 极高 | **明确排除。** 与现有“不接券商、不下单、不构成投资建议”的产品边界冲突，并引入合规与资金安全责任。 |
| 对全 A 股运行任意复杂实时公式并提供 Webhook | 中 | 高 | 高 | **近期不做。** 先把受控规则限制在自选；全市场调度、供应商限流、Webhook SSRF/密钥管理与告警风暴风险高。 |
| 原生 iOS + Android 与桌面功能完全同构 | 中 | 高 | 中 | **不做全量同构。** 先交付移动 Web 伴侣任务；复杂图表编辑与筛选构建保留桌面。 |
| 声称无偏的组合回测或历史市场排名 | 高 | 高 | 极高 | **在历史成分、退市样本、复权和事件版本齐备前不发布。** 否则幸存者偏差与未来函数会产生不可接受的虚假确定性。 |

## 8. 产品验收护栏

- 任一筛选、热力图或排行都必须显示 universe、成功/缺失数量、截至时间与降级状态。
- 切换日线/分钟线时，不得把不同来源或不同时点的数据伪装为同步快照。
- 图表事件必须可隐藏、可追溯来源；未知日期不得吸附到最近 candle。
- 从筛选创建预警时，字段、周期、阈值、质量门槛和计算版本必须完整复制。
- 回放模式必须有持续可见的历史状态，实时自选报价/预警不得混入历史图表判断。
- 移动端不得仅靠 hover 暴露来源或事件详情，所有关键操作需有 tap 等价路径。
- 任何实时、延迟和 EOD 标签均应由数据契约生成，不由前端根据页面刷新时间猜测。

## 9. 官方来源索引

以下链接均为厂商官方资料，访问核验日期为 2026-07-15。

### TradingView

[TV-01]: https://www.tradingview.com/support/solutions/43000692404-layouts-charts-drawings-indicators-and-their-interaction/ "TradingView: Layouts, charts, drawings, indicators, and their interaction"
[TV-02]: https://www.tradingview.com/support/solutions/43000761094-how-to-sync-selected-charts/ "TradingView: How to sync selected charts"
[TV-03]: https://www.tradingview.com/support/solutions/43000543053-how-to-use-the-compare-tool/ "TradingView: How to use the Compare tool"
[TV-04]: https://www.tradingview.com/support/solutions/43000629790-earnings/ "TradingView: Earnings markers"
[TV-05]: https://www.tradingview.com/support/solutions/43000718745-how-to-use-filters-in-screener/ "TradingView: Screener filters"
[TV-06]: https://www.tradingview.com/support/solutions/43000742436-tradingview-pine-screener-key-features-and-requirements/ "TradingView: Pine Screener"
[TV-07]: https://www.tradingview.com/support/solutions/43000745825-mastering-the-tradingview-watchlists/ "TradingView: Watchlists"
[TV-08]: https://www.tradingview.com/support/solutions/43000520149-introduction-to-tradingview-alerts/ "TradingView: Alerts"
[TV-09]: https://www.tradingview.com/support/solutions/43000474024-how-do-i-turn-bar-replay-on/ "TradingView: Bar Replay"
[TV-10]: https://www.tradingview.com/support/solutions/43000471705-how-to-purchase-additional-market-data/ "TradingView: Delayed and real-time market data status"
[TV-11]: https://www.tradingview.com/support/solutions/43000707156-how-to-set-up-the-display-of-the-heatmap/ "TradingView: Heatmap display"
[TV-12]: https://www.tradingview.com/support/solutions/43000747376-how-to-select-replay-interval-for-the-bar-replay-in-mobile-apps/ "TradingView: Mobile Bar Replay interval"

### Finviz

[FV-01]: https://finviz.com/elite "Finviz Elite feature matrix"
[FV-02]: https://finviz.com/help/screener "Finviz Screener help"
[FV-03]: https://finviz.com/blog/introducing-magnifying-glass-chart-multiple-timeframes-at-once/ "Finviz: Magnifying Glass and multiple timeframes, 2026-06-18"
[FV-04]: https://finviz.com/blog/create-custom-chart-templates-for-screener-stock-pages-and-more/ "Finviz: Cross-product chart templates, 2026-03-27"
[FV-05]: https://finviz.com/blog/analyst-ratings-now-on-finviz-charts/ "Finviz: Analyst ratings and event markers, 2026-02-18"
[FV-06]: https://finviz.com/blog/access-stock-screens-from-advanced-charts-on-finviz/ "Finviz: Screener inside Advanced Charts, 2026-05-12"
[FV-07]: https://finviz.com/blog/one-click-chart-alerts-are-now-live-for-finviz-members/ "Finviz: One-click chart alerts, 2026-02-09"
[FV-08]: https://finviz.com/blog/evolving-the-heatmap-dow-jones-nasdaq-100-russell-2000-and-more/ "Finviz: Heatmap updates, 2025-09-16"
[FV-09]: https://finviz.com/help/faq "Finviz: Data update frequency"
[FV-10]: https://elite.finviz.com/elite "Finviz Elite: Backtests and historical data"
[FV-11]: https://finviz.com/blog/introducing-portfolio-notes-track-your-thinking-behind-every-trade/ "Finviz: Portfolio Notes, 2026-03-19"

### Koyfin

[KY-01]: https://www.koyfin.com/help/charts-and-graphs/ "Koyfin: Historical Graph"
[KY-02]: https://www.koyfin.com/help/my-screens/ "Koyfin: Equity Screener"
[KY-03]: https://www.koyfin.com/help/release-notes/v3-67-historic-data-in-watchlist-screener/ "Koyfin: Historical data in Watchlist and Screener"
[KY-04]: https://www.koyfin.com/help/mywatchlists/ "Koyfin: My Watchlists"
[KY-05]: https://www.koyfin.com/help/building-lightning-quick-earnings-and-economic-calendars-with-koyfin/ "Koyfin: Earnings and economic calendars"
[KY-06]: https://www.koyfin.com/help/earnings-history/ "Koyfin: Earnings history and price reaction"
[KY-07]: https://www.koyfin.com/help/faq/is-your-data-live-or-delayed/ "Koyfin: Live, delayed and EOD data with last-trade timestamp"
[KY-08]: https://www.koyfin.com/help/mydashboards-myd/ "Koyfin: Linked dashboard widgets"
[KY-09]: https://www.koyfin.com/help/release-notes/v3-66-desktop-alerts/ "Koyfin: Desktop, email and mobile alerts"
[KY-10]: https://www.koyfin.com/help/release-notes/v3-47-linear-regressions-line-r2/ "Koyfin: Market Scatter"
[KY-11]: https://www.koyfin.com/help/mobile-application/ "Koyfin: Mobile application"
[KY-12]: https://www.koyfin.com/help/mobile-app-feautres/ "Koyfin: Mobile alerts, watchlists and charts"

### StockCharts

[SC-01]: https://help.stockcharts.com/charts-and-tools/stockchartsacp/multi-chart-layouts-in-stockchartsacp "StockCharts: ACP multi-chart layouts"
[SC-02]: https://help.stockcharts.com/charts-and-tools/stockchartsacp/editing-acp-charts "StockCharts: Indicators and overlays"
[SC-03]: https://help.stockcharts.com/scanning-and-alerts/technical-scans "StockCharts: Technical Scans"
[SC-04]: https://help.stockcharts.com/charts-and-tools/reports-and-galleries/scan-reports "StockCharts: Scan Reports"
[SC-05]: https://help.stockcharts.com/charts-and-tools/sharpcharts/chartlists "StockCharts: ChartLists"
[SC-06]: https://help.stockcharts.com/data-and-ticker-symbols/data-availability/real-time-data "StockCharts: Real-Time Data"
[SC-07]: https://help.stockcharts.com/learning-more/step-by-step-instructions/sharpcharts-how-tos/sharpcharts-workbench-how-tos/how-to-view-indicator-and-overlay-values-for-specific-days "StockCharts: Historical end date and date slider"
[SC-08]: https://help.stockcharts.com/charts-and-tools/sharpcharts "StockCharts: Chart header latest-data time"
[SC-09]: https://help.stockcharts.com/charts-and-tools/research-tools/earnings-calendar "StockCharts: Earnings Calendar"
[SC-10]: https://help.stockcharts.com/charts-and-tools/reports-and-galleries/historical-chart-gallery "StockCharts: Historical Chart Gallery"
[SC-11]: https://help.stockcharts.com/charts-and-tools/other-charting-tools/marketcarpets "StockCharts: MarketCarpets"
[SC-12]: https://help.stockcharts.com/charts-and-tools/sharpcharts/stockcharts-mobile-app "StockCharts: Mobile App"
[SC-13]: https://help.stockcharts.com/scanning-and-alerts/overview-of-technical-alerts "StockCharts: Technical Alerts"
