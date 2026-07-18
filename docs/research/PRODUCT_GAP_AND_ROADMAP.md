# 产品差距与可执行路线图

> 审计日期：2026-07-16  
> 产品边界：本地、单用户、A 股单股研究工作台；不接券商、不下单、不承诺交易所级实时行情。  
> 证据范围：`README.md`、`docs/REQUIREMENTS.md`、`docs/DESIGN.md`、[主流单股研究功能调研与取舍（2026）](COMPETITOR_CORE_FEATURES_2026.md)、`docs/research/` 其他调研文档、当前工作树代码，以及主代理浏览器实测。  
> 决策原则：先阻断会误导判断的数据与语义错误，再做失败隔离和请求复用，随后用现有合法数据补齐研究闭环。竞品能力只证明需求与交互模式，不证明数据可免费取得或再分发。

## 1. 执行结论

AShareRadar 已有可复用的单股底座：共享 workbench context、报价与日/分钟 K 线、多类规则研究报告、本地自选/笔记/提醒、建议历史、图表标记、SSE 和 provider/缓存诊断。

本轮已完成并纳入当前能力：主查询与自选输入的代码/中文名称自动补全；日线与分钟 Canvas 的鼠标、触控、键盘精确检查；由建议变化、提醒事件和笔记组成的“本地研究动态”；以及冷启动 12 请求、TTL 内切股 4 请求的分域基线。名称搜索请求只由非完整代码的用户输入触发，不计入切股基线。

当前主要差距仍不是卡片数量，而是三类契约尚未完全闭环：

1. **可信度闭环未成立**：新抓取 K 线可绕过业务时效校验；健康状态偏重 `fetched_at`；主快照与 SSE 状态混称；启发式权重、代理资金和缺财报评分可能被读成统计概率、真实资金或财务结论。
2. **故障边界仍有泄漏**：部分独立面板仍需统一业务可用性与 HTTP 状态，分钟分析的 HTTP 成功与业务可用性仍不完全一致；本地研究动态已单独区分真实空、加载和部分/全部不可用。
3. **正式数据与跨股契约未成立**：公告授权与修订维护、财务报告期与披露时点、跨股复权/时间对齐和长期维护成本均未闭环，因此本轮不接正式公告、正式财务趋势或多股聚合。

因此路线图只保留三类动作：**A 优化现有**优先，**B 精选主流新增**只使用现有或可证明合法的数据，**C 拒绝或后置**所有授权、覆盖率、合规或防未来函数条件不成熟的能力。

## 2. 证据化差距

| ID | 差距与影响 | 仓库/实测证据 | 竞品与产品证据 | 决策 |
| --- | --- | --- | --- | --- |
| G01 | **新数据“结构合法但业务陈旧”仍可能记成功并落库**，工作台、规则和告警会消费旧 K 线，且新 `fetched_at` 延长假绿状态。 | [逻辑审计](LOGIC_AND_ARCHITECTURE_GAPS.md) P1-1；`app/services/datahub_klines.py` 的 provider 成功路径未复用缓存业务时效判断。 | 全球图表产品普遍逐组件展示市场时间、来源及实时/延迟/EOD；[全球图表调研](COMPETITOR_GLOBAL_CHARTING.md) §5.6。 | A / P0 |
| G02 | **健康状态把抓取活跃度和市场数据新鲜度混为一谈**，周末可能假红，旧市场数据重新抓取可能假绿，调度器与诊断阈值还可能不一致。 | [逻辑审计](LOGIC_AND_ARCHITECTURE_GAPS.md) P1-2；`app/repositories/cache_stats.py`、`app/services/scheduler.py`、`app/services/system_diagnostics.py`。 | 国内竞品调研要求个股顶部常驻交易状态、最新时间、延迟/回退与权限标签；[国内竞品调研](COMPETITOR_CHINA.md) §9。 | A / P0 |
| G03 | **页面“实时连接正常”只代表 SSE 侧栏**，不代表主报价、主图或派生分析已刷新。 | [能力审计](CURRENT_CAPABILITY_AUDIT.md) §5 P0；`static/app.js::startStream` 只调用 `renderQuotes`，主工作台来自 `GET /api/stock/workbench`。 | TradingView、Koyfin、StockCharts 均把最后成交/数据时间放到对应组件，而非用一个全局绿灯代替。 | A / P0 |
| G04 | **启发式结果与代理指标存在过强命名**：规则情景被写成概率/置信度；缺核心财报仍可能给“财务体检”；量价代理简称“资金”；估计盘口可能被读成真实订单压力。 | [能力审计](CURRENT_CAPABILITY_AUDIT.md) §3.1、§5；`app/services/research_risk_reward*.py`、`app/services/financial_health*.py`。 | 东方财富/同花顺/Futu 的资金与筹码能力依赖 Level-2 或明确计算假设；国内调研明确要求“观测/推导/估算/缺失”分层。 | A / P0 |
| G05 | **因子校准与画像有可触发逻辑错误**：固定零样本估值锚令汇总样本恒为 0；换手率缺失被转成 0 可误选稳健画像。 | [逻辑审计](LOGIC_AND_ARCHITECTURE_GAPS.md) P1-3、P2-5；`research_factor_current.py`、`research_factor_report.py`、`research_regime.py`、`research_factor_weights.py`。 | 研究型竞品的评分只用于导航，原值、样本、缺失与方法必须可下钻；[研究工作流调研](COMPETITOR_RESEARCH_WORKFLOWS.md) §1。 | A / P0 |
| G06 | **局部失败可扩散或被界面抹掉**：告警只捕获少数异常族；本轮部分失败摘要与事件列表共用容器；本地读取失败可能像空数据。 | [逻辑审计](LOGIC_AND_ARCHITECTURE_GAPS.md) P2-4、P2-6；[能力审计](CURRENT_CAPABILITY_AUDIT.md) §5 P1。 | 国内主流预警均强调持续跟踪；本项目更应保留冷却、恢复、确认和证据，不可让一条故障使整批静默失效。 | A / P0 |
| G07 | **分钟分析业务不可用可用 HTTP 200 表达**，调用方和监控难以区分 `ok/degraded/unavailable`；路由周期说明与服务能力不一致。 | [能力审计](CURRENT_CAPABILITY_AUDIT.md) §3.1、§5 P1；`app/services/minute_analysis.py`。 | 数据不足时明确不可用是所有可信研究视图的前提。 | A / P0 |
| G08 | **已完成：切股请求按股票域/全局域分离**。冷启动仍为 12，在全局缓存 TTL 内每次切股为 4；搜索请求仅由非完整代码的用户输入触发。 | `static/app.js` 的全局缓存/生命周期与股票请求作用域；[test_frontend_chart_workspace.py](../../tests/test_frontend_chart_workspace.py)、[frontend-flow.spec.js](../../tests/e2e/frontend-flow.spec.js) 固化预算。 | Finviz/Koyfin/StockCharts 的价值在共享研究对象与视图状态，而不是每次切换重建全页面上下文。 | A / **已完成** |
| G09 | **市场概览与强势榜重复计算且容易被误读为全市场**：前者是固定种子，后者是自选/种子/抽样池。 | [能力审计](CURRENT_CAPABILITY_AUDIT.md) §3.2、§5 P1；`market_overview.py`；`/leaderboard` 还是同义路由。 | 热力图、筛选或排行必须显示 universe、覆盖数、失败数和截至时间；否则不得使用全市场措辞。 | A / P1 |
| G10 | **多个研究卡片共享同一价量证据却形成“多模型一致”观感**，用户难区分独立证据和重复派生。 | [能力审计](CURRENT_CAPABILITY_AUDIT.md) §2、§5 P1；workbench 一次上下文派生 diagnosis/factor/alpha/strategy/risk 等。 | 五家基本面产品共同采用“摘要导航 -> 原始值/方法/来源下钻”，AI 只能位于证据层之上。 | A / P1 |
| G11 | **已完成：建议变化台账与版本化比较**。当前时间线保留结论基础、规则/模型版本、市场时间和可比/不可比状态。 | `app/services/research_conclusion_change.py`、`app/repositories/advice.py`、`static/js/advice-timeline.js` 及对应测试。 | Simply Wall St Narrative、MarketScreener revisions、TipRanks/Seeking Alpha 观点历史共同证明“为何变化”是高价值研究任务。 | B / **已完成** |
| G12 | **已完成：自选研究队列**。当前包含分组、研究状态、优先级、复核日、置顶和未读变化等本地字段。 | watchlist 路由/仓储、`static/js/watchlist.js` 及队列测试。 | 国内四类产品都用自选+事件+预警维持跟踪；全球产品支持分组、列视图、入选原因和筛选结果回写。 | B / **已完成** |
| G13 | **已完成当前范围：日线/分钟上下文与精确检查**。日线支持 20/60/120/240，分钟支持 5/15/30/60m；桌面/移动端保留股票上下文，并可用悬停、点击或键盘检查单根 K 线精确值。 | `static/js/chart.js`、`static/js/chart-inspector.js`、`static/app.js`；[test_frontend_chart_inspector.py](../../tests/test_frontend_chart_inspector.py)、[frontend-flow.spec.js](../../tests/e2e/frontend-flow.spec.js)。 | TradingView/Finviz/同花顺/Futu 的共同价值是日线结构与分钟细节不丢上下文；当前不复制任意多图布局。 | B / **已完成** |

### 2.1 当前 12/4 请求基线

| 请求域 | 当前请求 | 当前触发 | 当前策略 | 切股现状 |
| --- | --- | --- | --- | --- |
| 股票域 | `/api/stock/workbench`、`/api/stock/minute-analysis`、`/api/advice/history`、`/api/stream/quotes` | 首次加载、每次切股 | 保持独立失败域；前三项绑定 `symbol/loadSeq`；SSE 仅在订阅集合变化、恢复可见或断线时重建 | **4 项**，且旧响应不得覆盖新股票 |
| 页面全局域 | `/api/market`、`/api/strong-stocks`、`/api/plates`、`/api/watchlist` | 首次加载、TTL/页面恢复、显式刷新或相关写后失效 | 使用独立全局生命周期和有界缓存；不随普通切股重取 | **0 项**（TTL 到期/写后刷新除外） |
| 运维全局域 | `/api/data/status`、`/api/tasks/status`、`/api/tasks/runs`、`/api/monitor/events` | 首次加载、诊断激活、15s 轮询或显式操作 | 与股票 `AbortController` 解耦，不随普通切股重建 | **0 项** |

当前回归基线：冷启动仍允许 12 个端点以独立失败域加载；在 TTL 内连续切换股票时，每次只新增 4 个股票域请求，且全局面板保留上次成功值。完整 6 位代码不触发搜索；名称或非完整代码只在用户输入、250 ms 防抖完成且缓存未命中时新增一次 `/api/stocks` 请求。这里不以“大一统接口”换取数字好看，避免一个慢 provider 拖垮整个页面。

## 3. A / B / C 产品取舍

### A. 优化现有

1. **统一 Snapshot/Freshness 契约**：每个数据域输出 `market_time`、`fetched_at`、`source`、`freshness_class`、`session`、`adjustment_mode`、`coverage_count/expected_count`、`degraded_reason`；前端不自行猜“实时”。
2. **修正时效与逻辑缺陷**：新抓取 K 线先过交易日历感知校验再记成功/落库；健康策略共用事件时间；修复校准恒零和 `None -> 0` 画像错误。
3. **重写高风险语义**：概率改“规则情景权重”，置信度改“证据充分度/综合可信等级”；无财报最小字段集时不生成财务分；资金/筹码/订单压力常驻标明观测或估算。
4. **部分已完成，继续补齐失败隔离**：本地研究动态已区分 loading、真实 empty、部分/全部 unavailable；其余规则批次、分钟业务状态和独立面板继续按统一契约收敛。
5. **已完成请求按域复用**：股票切换只刷新 4 个股票域请求；全局域使用独立生命周期、TTL、可见性恢复和写后失效。
6. **证据族去重**：摘要只展示一个当前判断、主要反证与冲突；每项结论标记原始证据 ID、共享依赖和独立来源数。
7. **已完成名称发现入口**：主查询与自选添加复用 `/api/stocks`，具备 250 ms 防抖、Abort/陈旧响应保护、有界缓存、键盘选择和显式空/失败状态；完整代码直接提交。

### B. 精选主流新增

1. **已完成结论变化台账**：当前建议时间线保存版本与结论基础，给出结构化变化或明确的无前序/旧版/版本变化状态；不依赖新闻或研报。
2. **已完成自选研究队列**：本地自选已包含分组、研究状态、优先级、复核日、置顶、最近查看和未读变化等字段。
3. **已完成当前日线 + 分钟范围**：日线 `20/60/120/240` 与分钟 `5/15/30/60m` 保持同股上下文；Canvas 精确检查支持桌面悬停、触控点击和键盘逐点移动，不建任意布局编辑器。
4. **首版已完成现有事件复核流**：工具页“本地研究动态”合并建议变化、告警触发/恢复和笔记，默认最近 12、单类最多 20，并区分分来源状态；它不是公告/新闻日历，未取得授权前不接外部正文或摘要。
5. **受控条件复用**：在覆盖率可证明后，让少量可审计条件复用于“当前自选扫描 -> 规则提醒 -> 历史验证”；每条条件版本化并带缺失值策略，不开放任意脚本。

### C. 拒绝或后置

| 能力 | 决策 | 放行条件 |
| --- | --- | --- |
| 正式公告/公司事件、无授权新闻、研报、分析师共识、评级、transcript 或竞品成品数据 | **本轮暂缓；无授权内容拒绝**抓取、缓存、翻译、摘要或再分发 | 逐来源取得明确 API/许可，并完成署名、时间、去重、修订、删除、存储和长期维护条款审查 |
| 券商连接、账户同步、模拟/真实下单、条件单 | **明确排除** | 与当前产品定位冲突，不设近期放行条件 |
| “全市场扫描/热力图/强势榜”却只用固定种子或抽样池 | **拒绝伪全市场表述** | 可给出 universe 版本、预期数、成功/缺失数、停牌/ST 规则、时间和来源；否则只称“样本观察” |
| Level-2、逐笔、真实主力资金、可靠盘口全覆盖 | **后置** | 合法行情授权、明确订单分类口径、覆盖与缺失策略、成本可持续；无源时不得同名代理 |
| 正式财务趋势、财务健康、DCF、预期修正 | **本轮暂缓数据接入，保留信息架构** | 合法财报/预期源、报告期与披露时间、重述、币种/单位、估值与复权口径、未来函数测试齐备 |
| 多股聚合、横向比较或公式叠加 | **本轮暂缓** | 股票上限、统一交易日/复权/币种口径、缺失与退市策略、请求预算和长期维护契约齐备 |
| 严格逐 bar 回放、全市场历史排名、组合回测 | **P2 研究项** | 所有读取支持同一 `as_of`；复权、历史成分、退市样本、事件回填和交易成本可审计 |
| 任意脚本/公式市场、12/16 图布局、复杂画线 CAD | **拒绝近期建设** | 核心研究闭环稳定后重新评估；当前只做受控条件和少量持久化标记 |
| 社区、达人排行、跟单、模型荐股、黑箱总分 | **拒绝** | 不符合本地单用户、证据优先定位 |
| 强制云账号、跨设备全量同步、原生双端 App | **后置** | 本地导入导出成熟后，再评估可选加密备份；移动端先做响应式研究伴侣 |

## 4. 优先级评分

评分均为 `1-5`：用户价值、使用频率、数据可得性、误导风险和移动端影响越高越应优先；实现复杂度越高越难。优先级不是机械总分：**误导风险 5 的现有缺陷直接进入 P0**；B 类新增不得越过数据授权、覆盖率和 `as_of` 闸门。

| 优先级 | 项目 | 类别 | 用户价值 | 频率 | 数据可得性 | 实现复杂度 | 误导风险 | 移动端影响 | 排序理由 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| P0 | P0-1 新拉取时效校验 + 统一健康时钟 | A | 5 | 5 | 5 | 3 | 5 | 4 | 错数据会污染所有下游结论和告警 |
| P0 | P0-2 高风险语义与因子逻辑修复 | A | 5 | 5 | 5 | 3 | 5 | 4 | 当前首屏可直接造成过度解读 |
| P0 | P0-3 告警/本地数据/分钟状态失败隔离 | A | 5 | 4 | 5 | 3 | 5 | 4 | 不能把故障显示成成功或空数据 |
| 已完成 | P0-4 12/4 请求分域、缓存与刷新策略 | A | 4 | 5 | 5 | 3 | 3 | 5 | 当前切股基线为 4；继续由回归测试防漂移 |
| P1 | P1-1 市场 snapshot 复用与样本语义 | A | 4 | 5 | 5 | 3 | 4 | 4 | 同时降低重复计算和“全市场”误读 |
| P1 | P1-2 证据族去重 + 结论变化台账 | A+B | 5 | 4 | 5 | 4 | 4 | 4 | 直接回答研究最关键的“为何改变” |
| 已完成 | P1-3 自选研究队列 | B | 5 | 5 | 5 | 3 | 2 | 5 | 高频、完全本地、形成持续研究入口 |
| 已完成 | P1-4 日线/分钟周期联动与精确检查 | B | 4 | 5 | 5 | 3 | 3 | 5 | 复用已加载 K 线，不增加检查请求 |
| 首版已完成 | P1-5 本地事件复核时间线 | B | 4 | 4 | 5 | 3 | 3 | 4 | 当前连接建议变化、提醒事件和笔记，不依赖外部内容授权 |
| P2 | P2-1 受控条件复用于自选扫描/提醒/验证 | B | 5 | 3 | 4 | 5 | 4 | 3 | 价值高但需版本化条件与覆盖语义 |
| P2 | P2-2 严格 as-of 单股逐日回放 | B | 4 | 2 | 3 | 5 | 5 | 3 | 防未来函数未证明前不可上线 |
| P2 | P2-3 行业/概念热力图 | B | 4 | 3 | 2 | 4 | 5 | 4 | 受完整 universe、分类和市值口径制约 |

## 5. 第一轮：可信基线、失败隔离与请求复用

目标：先让“数据是否可用、截至何时、为何降级”可靠，再把切股请求从 12 项收敛为 4 个股票域请求。第一轮不新增外部数据源。

| 工作包 | 文件范围（最小责任边界） | 依赖 | 验收标准 | 必测项 |
| --- | --- | --- | --- | --- |
| R1-1 时效校验与统一状态契约 | `app/services/datahub_klines.py`、`datahub_cache.py`、`data_quality_time.py`、`trading_calendar.py`；`app/repositories/cache_stats.py`；`app/services/system_diagnostics.py`、`scheduler.py`；相应 `app/models/*`、`app/api/routes/data.py`；`static/js/diagnostics.js`、`static/js/workbench.js` | 共享交易日历策略；明确历史区间查询与“最新数据”查询语义 | provider 返回旧日/分钟 K 时继续尝试后备源；全部陈旧时不记成功、不落为新鲜缓存；健康输出同时含市场时间与抓取时间；周末/午休/节假日结论一致；前端分别显示行情流、主报价、分析快照状态 | `tests/test_datahub_klines_modules.py`、`test_datahub_cache_modules.py`、`test_data_quality_modules.py`、`test_scheduler_modules.py`、`test_system_diagnostics_modules.py`、`test_frontend_diagnostics.py`；新增旧首选源、全旧、休市边界契约用例 |
| R1-2 误导语义与逻辑修复 | `app/services/research_factor_current.py`、`research_factor_report.py`、`research_regime.py`、`research_factor_weights.py`、`research_risk_reward*.py`、`financial_health*.py`、资金/订单压力对应 report 模块；`app/models/*`；`static/js/research-risk-reward.js`、`static/js/workbench.js`、`static/js/research-panels.js` | 定义“参与总分”与“参与校准”因子；定义财务最小字段集；统一 provenance 枚举 `observed/derived/estimated/unavailable` | 真实七因子组合不再令校准样本恒 0；换手缺失不命中低换手画像；无统计校准不出现“概率”；无核心财报不出现财务分；所有资金/筹码/订单压力入口常驻显示口径 | 因子真实组合和 `None/0/1.9/2.0` 参数化测试；`tests/test_research_regime_modules.py`、`test_financial_health_modules.py`、`test_research_risk_reward_modules.py`、`test_frontend_research_panels.py` |
| R1-3 局部失败隔离 | `app/services/alerts.py`、`minute_analysis.py`；`app/api/routes/alerts.py`、stock 分钟路由；`app/models/*`；`static/js/alerts.js`、`notes.js`、`watchlist.js`、`workbench.js` | 领域异常包装；业务状态契约 `ok/degraded/unavailable`；取消异常必须继续传播 | 首条规则抛 SQLite/I/O/领域异常时后续规则继续，`failed_count` 正确且错误脱敏；评估摘要完成后仍可见；空态区分空/失败/未加载；分钟不可用不被统计为成功 | `tests/test_rules_alerts.py`、`test_api_alert_routes.py`、`test_minute_analysis_modules.py`、`test_frontend_notes_alerts_requests.py`、`test_frontend_watchlist_requests.py`；增加完整异步流程最终 DOM 断言 |
| **已完成** R1-4 请求分域与复用 | `static/app.js`、`static/js/diagnostics.js`、`watchlist.js`、`api.js` | 页面级全局缓存、独立请求生命周期、TTL/可见性恢复和写后失效 | 冷启动 12；TTL 内切股只发 workbench、minute、history、SSE 共 4 项；全局数据不随普通切股重取；搜索请求只来自非完整代码的用户输入 | `tests/test_frontend_app_flow.py`、`test_frontend_chart_workspace.py`、`test_frontend_diagnostics.py`、`test_frontend_watchlist_requests.py`、`tests/e2e/frontend-flow.spec.js` |

**第一轮退出条件**：所有 P0 回归通过；浏览器网络日志满足 `12 -> 4/次切股` 基线；任一 UI “正常/实时/财务/资金/概率”文案均能由返回契约证明；局部故障不会覆盖其他模块或伪装为空。

## 6. 第二轮：研究连续性与多周期核心能力

目标：只用现有行情、规则、本地用户数据和合法元数据，形成“当前结论 -> 变化原因 -> 研究队列 -> 多周期核验 -> 更新记录”的闭环。

| 工作包 | 文件范围（最小责任边界） | 依赖 | 验收标准 | 必测项 |
| --- | --- | --- | --- | --- |
| R2-1 证据族与结论变化台账 | `app/repositories/advice.py`；`app/db/schema_definitions.py`、`schema_migrations.py`、mappers；`app/models/workbench.py` 及 advice/research 模型；`app/workflows/workbench_pipeline.py`、`individual.py`；新增单一职责 `app/services/research_conclusion_change.py`；`static/app.js`、`static/js/research-panels.js` | R1 Snapshot 契约；稳定 `evidence_id`、`rule_version`、`model_version`；兼容旧 advice 行 | 相同结论+相同证据不制造新变化；结论、风险、支撑/压力、数据状态或版本变化生成结构化 diff；界面展示“自上次以来”，可展开旧/新值和证据；旧记录明确“版本未知/不可直接横比” | schema 兼容、并发去重、diff 单测、工作台契约、前端空/单条/多条/旧版记录；`tests/test_schema_compat.py`、`test_workbench_pipeline_modules.py`、`test_frontend_app_flow.py` |
| R2-2 自选研究队列与结构化 Thesis | `app/repositories/watchlist.py`、`notes.py`；DB definitions/migrations/mappers；`app/api/routes/watchlist.py`、`notes.py`；watchlist/note 模型；`static/index.html`、`static/js/watchlist.js`、`notes.js`、`static/css/sidebar.css`、`responsive.css` | R2-1 变化未读数；继续保持本地优先；当前股票 canonical symbol | 支持状态、优先级、关注理由、复核日、最近查看、未读变化；从当前股票一键入队，无重复代码输入；按“需复核/优先级/最近变化”排序；360/390/430px 完成查股->入队->写 thesis->确认，不进入运维区 | CRUD/未知字段/迁移/排序/并发写测试；`test_api_watchlist_routes.py`、`test_api_notes_routes.py`、前端请求作用域测试、移动端 Playwright 截图和键盘顺序 |
| **已完成当前范围** R2-3 日线与分钟周期联动 | `static/index.html`、`static/app.js`、`static/js/chart.js`、`static/js/chart-inspector.js`、相关 CSS | 现有日/分钟 K 线和分钟 availability | 同股上下文切换 `20/60/120/240`、`5/15/30/60m`；桌面/移动端可精确检查时间、OHLC、涨跌幅、量、启用 MA 与来源/缓存元数据；检查纯前端、零请求 | `test_frontend_chart_workspace.py`、`test_frontend_chart_inspector.py`、`tests/e2e/frontend-flow.spec.js` |
| **首版已完成** R2-4 现有事件复核流 | `static/app.js`、`static/js/research-activity.js`，复用 advice/alerts/notes 现有加载与刷新链 | 统一可解析时间；不引入外部内容或新聚合请求 | 建议变化、告警触发/恢复和笔记进入同一可筛选时间线；默认最近 12，单类最多 20；部分/全部 unavailable、loading、empty 分离；写后沿既有链更新 | `test_frontend_research_activity.py`、`test_frontend_local_activity_state.py`、advice/alerts/notes 前端测试及 `tests/e2e/frontend-flow.spec.js` |
| R2-5 受控条件复用（条件性 P2） | 新增 condition schema/model/service；复用 `app/services/alerts.py`、`research_replay.py`；只扫描 watchlist；对应 route/UI | R1/R2-1；字段计算版本；缺失策略；质量门槛；历史 `as_of` 设计评审通过 | 同一条件序列化后用于即时自选扫描、提醒和历史验证，字段/周期/阈值/版本不漂移；结果显示范围、命中、缺失和截至时间；不得宣称全市场 | schema round-trip、三入口一致性、缺失/陈旧数据抑制、版本迁移、防未来数据负向测试 |

**第二轮退出条件**：用户能在一分钟内回答“当前结论、关键证据、主要反证、上次以来变化、下一复核动作”，并能在手机上完成高频闭环。任何新增视图都不依赖无授权内容，不把抽样称为全市场，不把历史统计称为逐 bar 回放。

## 7. 发布与停止规则

- 任一 P0 项若缺少负向测试（陈旧、缺失、局部异常、旧响应、休市边界），不得进入下一批。
- Snapshot/Freshness 契约未统一前，不开发热力图、全市场筛选或自动变化摘要。
- 数据来源没有许可证明时，只允许保存用户手工录入或公开链接元数据；不得缓存全文或生成二次摘要。
- `coverage_count/expected_count`、universe 版本或失败数缺一项，页面只能使用“样本/观察池”措辞。
- `as_of` 未贯穿 provider、缓存、规则和事件读取前，不上线严格回放或对外声称无未来函数。
- 移动端验收失败不阻塞纯后端 P0 修复上线，但阻塞对应 UI 工作包完成；运维面板不应回到移动研究主路径。

## 8. 最终完成审计清单

- [x] 阅读 `README.md`、`docs/REQUIREMENTS.md`、`docs/DESIGN.md`。
- [x] 研究索引已纳入 [主流单股研究功能调研与取舍（2026）](COMPETITOR_CORE_FEATURES_2026.md) 和 [国内竞品调研](COMPETITOR_CHINA.md)。
- [x] 对照当前代码核验 workbench、市场/强股、分钟、建议历史、SSE、自选、板块、数据状态和 monitoring 请求入口。
- [x] 保留冷启动 12 请求，并将 TTL 内切股固化为股票域 4 项；名称搜索请求仅由非完整代码的用户输入触发。
- [x] 将代码审计的 6 个高置信逻辑/失败缺陷映射到 P0 工作包、验收标准和测试。
- [x] 分离 A 优化现有、B 精选主流新增、C 拒绝或后置，并保留授权、交易、全市场覆盖和防未来函数红线。
- [x] 按用户价值、频率、数据可得性、实现复杂度、误导风险、移动端影响完成 P0/P1/P2 评分。
- [x] 两轮每项均明确文件范围、依赖、验收标准、测试和退出条件。
- [x] 路线图未建议无授权新闻/研报、交易接入或全市场伪扫描。
- [x] 本轮只更新指定文档，不修改生产代码、测试或 [主流单股研究功能调研与取舍（2026）](COMPETITOR_CORE_FEATURES_2026.md)。
- [x] 执行 `git diff --check`，无空白错误。
