# 同类产品与功能完善取舍

同类产品调研日期：2026-09-07；扶摇 API 接入评估与实施日期：2026-09-10。本文按日期记录对照 AShareRadar 当时实现的调研与实施取舍，区分官方功能说明、公开接口/源码和本项目的工程判断。2026-09-07 的竞品比较未登录商业产品账户，未验证付费端实时操作；2026-09-10 的扶摇真实账号样本及全量归档验证另列于下文，不证明收费政策或全部权限。官方手册的发布日期与该次网页读取日期分别看待。现行功能合同见[架构设计](DESIGN.md)。

## 官方证据

| 产品 | 已确认的能力与实现信息 | 对本项目的启发及边界 |
| --- | --- | --- |
| TradingView | [自定义筛选说明](https://www.tradingview.com/support/solutions/43000718804-how-to-create-save-and-update-a-custom-screen/)明确保存条件、排序、列集和视图，区分保存与复制；[预警管理](https://www.tradingview.com/support/solutions/43000595311-manage-alerts/)提供触发日志 | 该次调研时项目已有筛选方案，缺口在操作语义与历史可查阅性，后续修复见下文。官方没有公开数据库事务或幂等实现，不能据此照搬 |
| TradingView Pine | [Alerts 文档](https://www.tradingview.com/pine-script-docs/concepts/alerts/)说明创建预警时服务端保存脚本、输入、证券和周期的副本，后续图表修改不改变已创建预警 | 历史事件必须保留当时的上下文。AShareRadar 旧事件未保存完整历史方案正文，不应以当前名称/条件冒充历史版本 |
| 东方财富 Choice / EMQuantAPI | [Choice 官方手册](https://choice.eastmoney.com/FileDownload/CFTG20210727.pdf)第 12–13 页给出证券范围、指标、条件表达式和数据展示流程，可保存条件模板；[EMQuantAPI Java 文档](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Java.html)公开 cps 的 codes、indicators、conditions、options 分离契约及排序/Top N 等选项 | 借鉴显式筛选输入和执行参数。手册为 2021 版本证据，不能认定是当前客户端的全部布局；公开 API 也不证明 GUI 内部直接调用该接口 |
| FINVIZ | [官方方案管理介绍](https://finviz.com/blog/create-stock-screens-then-save-and-trade-them/)说明筛选方案的保存、管理和分享 | 方案是可持续使用的对象，列表不能在第 100 项静默截断。分享不是当前本地研究应用的必需交付项 |
| QuantConnect / LEAN | [回测结果](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/results)提供订单事件、成交、费用和多维结果；[LEAN 报告](https://www.quantconnect.com/docs/v2/lean-cli/reports)公开从指定结果 JSON 生成报告，并可附策略版本、通过 Report 模板与键定制 | 借鉴从已保存结果生成可审阅视图。AShareRadar 当时已有冻结执行、纸面委托、复盘和导出；该次增加了现存筛选变化证据的可读视图，未引入新的交易引擎 |

## 2026-09-07调研时确认的问题与修复

1. 原另存操作在有选择时发送 PUT，按钮含义与写入行为相反。现已始终用 POST 创建副本，独立更新操作绑定已有 ID 和修订；前端验证回执中的身份与提交定义。
2. 原方案列表固定读取第一页 100 条，却按 total 提示已全部读取。现已接入后端分页，选择身份独立于当前列表页；翻页和回读失败保留已确认状态。
3. 原筛选变化 POST 返回进入、退出、当前不可排名的完整集合，SQLite 也保存了事件，但页面只展示数量且没有读取历史的接口。现已增加轻量摘要页、单条明细分页及即时记录明细，避免每页同时传输多份全市场名单。
4. 原多市场恢复后再次设置 select.value 会清除其余选择，部分研究上下界也无法由表单表达。现已修复多选恢复，并验证条件完整往返；兼容方案继续使用原定义，禁止以不完整表单另存或覆盖。

## 2026-09-07方案与工程判断

本次调研对应的方案管理和变化记录两条闭环已实现，现行契约见[架构设计](DESIGN.md)。筛选变化继续区分新进入、退出与当前不可排名，最后一类不能伪装为退出。历史读取直接使用已保存事件，不重新运行筛选、不请求在线行情，也不赋予自动入队或交易权限。

事件列表只返回 ID、修订、来源批次、记录时间、摘要和计数；打开单条记录后按类别分页取证券代码。旧记录没有历史名称和完整 ScreenSpec，展示时明确引用方案 ID/修订，保留原摘要但不声称已重新验证其历史条件。用户修改当前方案不会改变旧记录内容。

该次调研未纳入：桌面通知点击后精确定位触发事件、自选分组作为冻结筛选范围、复盘报告之间的可比性分析。前两项分别需要明确事件身份和成员集合时间点；第三项需先对齐数据区间、成本与模型假设。它们有产品价值，但不是给现有按钮增加入口就能可靠完成的功能。自动筛选交易和全量动态预警也不从竞品能力推导为本项目默认行为。

## 2026-09-10 扶摇 API 接入与迭代状态

本节以[同花顺官方 Financial-API 契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/README.md)为接口依据。**已实现可关闭的 REST 接入、财务事实、估值观察、板块/情绪后台采集及独立 Parquet 全量/增量研究归档与导出；真实全量历史归档已完成发布和 CLI 摘要校验。** 真实账号验证包括 30 只有效沪深京股票的三报表、指标及估值观察，以及本次板块和情绪采集。样本通过不代表全部证券类型覆盖或长期权限稳定。费用政策、金额单位、历史更正版本与首次披露时点仍未确认。公开资料不足以认定所有接口免费；[费用咨询](https://github.com/HiThink-Tech/Financial-API/issues/57)也不能替代账号计费条款。以下取舍是本项目的工程判断，不是供应商效果承诺。

### 本次真实验证事实

| 范围 | 2026-09-10 观察结果 | 验证边界 |
| --- | --- | --- |
| 财报与估值 | 30 只有效股票完成三报表、财务指标及估值观察：25 只沪深股票，加 `920002.BJ`、`920001.BJ`、`920003.BJ`、`920005.BJ`、`920006.BJ`；旧代码 `430047.BJ` 独立返回业务码 `3001` | 不证明金额单位、首次披露、修订版本或全部证券类型均已核实；失败股票未被替换为零值 |
| 板块与情绪 | 板块目录和行情各 710 条；2026-09-10 涨停 34、跌停 11、炸板 22、龙虎榜 60 条，选定股票异动 0 条 | 异动零条仅对应选定查询，不代表全市场无异动；一次成功不证明长期分页覆盖和权限稳定性 |
| 全量历史 | 已发布并通过 CLI `verify`：5,560 只股票、10,275,240 条日线、2,427 个交易日期，2016-09-12 至 2026-09-10；本地校验、规范文件写入及发布共 165.65 秒 | 耗时不含下载；数据保持 `none`，没有生成前复权序列或正式评分输入 |
| 企业行动 | 57,184 条原始记录去除 1 条完全重复，保留 57,183 条；保留并标记 2 组同证券同日的不同记录及 2 条负股本变动记录 | 不同记录可能为独立事件或修订，禁止直接求和或选最后一条；负比例可表示缩股，不能假定各类股东都按同一比例变动 |
| 十日增量 | 真实近十日文件 55,479 行已校验；增量归并、幂等和发布有离线测试 | 尚未执行真实联网增量同步；不能把文件校验等同于增量发布验收 |

### 接入前确认的缺口与本轮响应

- 原财务体检只有市场估值和交易体征，正式评分与报告期不可用。本轮新增 `FinancialReportBundle`，有缓存后显示实际报告期和财务原值；单位和评分规则未核实，财务分继续不可用。
- 原行情能力无法完整表达财报期间、来源及指标口径。本轮采用独立模型、侧库和 `FuyaoService`，不把三张报表拼入 `Quote`；正式行情来源优先级保持。
- 既有 `ProviderRuntime` 已做请求合并、并发准入、超时和失败冷却，本轮复用其研究能力，并增加账号节流和项目持久请求预算，不建立新的行情运行时或定时调度器。
- 既有估值与主题分析继续使用原证据；新增估值快照按指标和本地观察日积累，新增板块行情/成员供当前解释，不将不同口径历史混为一个分位。
- 原财报问答因缺证据拒答。本轮只开放能与已缓存记录对应的有限财务事实意图；没有匹配字段或期间时保持证据不足，不产生交易动作。
- 当前运行日线要求前复权。本轮 Parquet 始终保留 `none`，另存企业行动和审计清单，未实现前复权转换或直接进入现有正式回测/评分的桥接。

### 可以接入的接口

以下为原方案筛选出的 `GET` 接口范围；表后实施状态区分已提供产品入口与后续候选，不能把端点白名单误认为全部功能已完成。真实验证范围以上表为准。表中通用前缀用于缩短路径，不表示通配符端点。

| 顺序 | 接口组与路径 | 项目用途及边界 |
| --- | --- | --- |
| 第一批 | `/api/a-share/financials/` 下的 `income-statements`、`balance-sheets`、`cash-flow-statements`、`indicators` | 补财报、盈利/现金流/偿债事实；先展示，后评估规则。报表按单股取数，指标按单股和报告期取数。[财务契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-financials.md) |
| 第一批配套 | `/api/meta/tickers/search`、`/api/meta/tickers/list` | 代码消歧、名称补齐及当前股票池校验；固定 A 股资产类别，不能仅按六位代码猜证券身份。[元信息契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-meta.md) |
| 第二批 | `/api/dump/market-dumps/` 下的 `daily-k/download-url`、`daily-k-10d/download-url`、`adjustment-factors/download-url` | 十年未复权日线、近期增量和公司行为，服务批量研究与数据准备。需要另行下载 Parquet；获取三个链接不等于三个请求已完成文件下载。[批量下载契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-market-dumps.md) |
| 第二批配套 | `/api/a-share/prices/historical`、`/api/a-share/corporate-actions/adjustment-factors` | 小范围补缺、复权对账和除权样本检查。个股历史端点支持日线，不能用作分钟源。[行情契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-prices.md) |
| 第二批配套 | `/api/a-share/valuations/snapshot` | 补 PE TTM/MRQ、PB MRQ、PS TTM、PCF TTM，并逐日保存来源与口径；仅最新快照，不能立即补成历史估值库。[估值契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-valuations.md) |
| 第三批 | `/api/a-share-index/` 下的 `catalog/ths-index-list`、`constituents/ths-stock-list`、`prices/snapshot`、`prices/historical` | 增强现有行业/概念解释、板块比较及基准。成员是当前名单，不能反推历史成员。[指数契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-index.md) |
| 第三批 | `/api/a-share/special-data/` 下的 `limit-up-pool`、`limit-down-pool`、`limit-break-pool`、`dragon-tiger-list`、`anomaly-analysis-stock` | 补涨跌停、炸板、龙虎榜及异动解释。先显示事实与提供者观点；取完分页后才统计市场数量。[特色数据契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-special-data.md) |
| 后续观察 | 同一特色数据前缀下的 `limit-up-ladder`、`hot-stock-list`、`hot-stock-list-history`、`hot-stock-rank-trend`、`skyrocket-list` | 研究热度和拥挤程度，避免与现有动量/涨幅重复计分；榜单缺席不等于热度为零。 |
| 小范围校验 | `/api/a-share/prices/snapshot`、`/api/a-share/calendar/trading-days` | 报价对账及近期日历交叉检查。快照缺逐股时间；日历只覆盖过去一年至今天，不能替代现有长区间及未来日历。[行情契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-prices.md)、[日历契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-calendar.md) |
| 暂缓 | `/api/a-share/auction/snapshot`、`/api/a-share/auction/short-term-benchmark` | 可扩展盘前观察，需先处理竞价状态、时间证据和单位，不能直接作为可成交报价。[竞价契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-auction.md) |
| 独立产品扩展 | `/api/fund/profile/detail`、`/api/fund/portfolio/holdings`、`/api/fund/market/snapshot`、`/api/fund/market/historical` | 基金/ETF 研究另作后续版本；定期披露重仓不等于机构当前仓位。先把 A 股核心流程补完整。[基金契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-fund.md) |

分钟、tick、Level-2 不在其公开覆盖范围；当前也没有核实到能补齐本项目个股公告、新闻或研报原文缺口的端点。基金资讯与个股公告不能混为一谈。[官方能力边界](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/capability-map.md)

### 接入前必须处理的具体差异

1. **财务期间和版本。** 财报有报告期末与报告日期字段，但公开契约未充分保证首次披露时刻、历史更正版本和单季/累计口径。适配时保留供应商报告日期，只有核实披露语义后才赋予 `published_at`；另存 `fetched_at`、本系统首次观察时间、单位、来源和内容摘要。季度口径未确认前不计算 TTM；今天取得的修订后历史值不能视作当年已经知道。金融企业与普通企业分别解释，负债合计不能冒充有息负债。指标字符串需显式解析，缺失不得补零。[财务契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-financials.md)
2. **行情时间与字段拼接。** 报价显式批量的总时间可以为空，分页总时间是最新有效时间，行内没有逐股报价时间。当前 `Quote.timestamp` 是新鲜度判断依据，不能填入请求完成时间。首轮将这类数据留在辅助观察模型；补齐时间证据和兼容契约后再评估正式报价准入。行情快照也不能单独补齐换手、市值与估值字段；跨端点拼接须保留各自来源和口径。[行情契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-prices.md)
3. **估值口径。** 五项指标的批次最新时间不代表每项同时更新；负 PE/PCF 保留原值，不能取绝对值后评为便宜。现有 `Quote.pe` 不足以区分 TTM 与 MRQ，应先用独立估值记录，禁止无标记合并不同口径的历史分位。[估值契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-valuations.md)
4. **批量文件与复权。** 原始文件先进入独立数据集，校验身份、日期、OHLC、重复/冲突与覆盖后发布。日线同证券同日冲突拒绝；企业行动只去除完全相同的规范记录，同日不同记录保留并标记歧义，需核对事件与版本，不能直接求和或选最后一条。保持未复权、前复权和后复权隔离；新公司行为可能修改较早复权序列，不能只拼接最近若干天。REST 公司行为列与批量文件列不完全相同，后者另含配股比例和价格，转换前分别核对。十日增量不能修复所有旧历史修订或长时间断更，需按实际日期覆盖决定补缺和周期性全量对账。[批量下载契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-market-dumps.md)
5. **样本与未来数据。** 当前板块成员只能用于当前解释；从接入日开始保存观察版本，不伪造过去的成员关系。连板天梯每梯最多展示四只股票，不能据此计算全市场梯队数量；其中 `seal_nextday` 是次日结果，必须排除出当日特征。异动理由是提供者分析，不能升级为已核实公告事实。[指数契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-index.md)、[特色数据契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-special-data.md)
6. **单位和竞价状态。** 普通行情成交量为股，竞价成交量为手；竞价 `timestamp` 是响应组装时间。适配必须转换单位并保留 `auction_phase`、`data_status`，未就绪/停牌不能补成零价格。[竞价契约](https://github.com/HiThink-Tech/Financial-API/blob/main/docs/api/endpoints-auction.md)

### 当前交付与后续验收

| 批次 | 当前交付 | 尚待完成的验收或迭代 |
| --- | --- | --- |
| 0：接入基础 | 已有默认关闭配置、REST 客户端、独立研究能力、ProviderRuntime 接入、同账号节流、有限退避和持久尝试预算；专用本地启动入口保留显式环境优先级；真实样本及全量归档已验收 | 补齐费用政策、允许用途和各能力权限证明；本次成功不扩展为全部接口或长期可用承诺 |
| 1：财务功能 | 已有追加观察仓储、显式后台任务、个股财报原值/报告期面板和有限财务事实问答；30 只沪深京股票的财务与估值观察已完成，不生成财务分 | 继续核对已采样财报的期间、币种、单位及特殊证券口径；确认首次披露和修订语义后再讨论比率、TTM及财务规则，不能视为所有证券类别已覆盖 |
| 2：历史数据效率 | 已有 Parquet 全量/增量、分批校验、磁盘去重归并、不可变版本、失败原子回退及未复权 CSV/企业行动/manifest 导出；真实全量已发布校验，日线冲突拒绝，企业行动不同记录保留并标记歧义 | 真实十日文件已校验，联网增量发布待验收；小范围 REST 对账、复权转换和正式研究消费桥接未实现；正式执行仍缺历史成员、披露版本、交易状态及独立来源准入 |
| 3：解释与筛选 | 已有估值观察与本地同口径观察日分位、板块目录/行情/选定成员，以及涨跌停/炸板/龙虎榜/当日个股异动的采集与展示；本次板块及情绪采集结果已核验 | 继续验证实际分页与覆盖稳定性、积累足够观察日；不把数据不足视为零风险。热度系列、板块历史行情、竞价和基金产品入口仍为后续候选 |
| 4：评分研究 | 未启用新增财务或情绪分；正式规格与现有排名保持 | 单独冻结合格数据与试验集合，做消融、前瞻积累和样本外比较，复用时点完整性、成本、回撤、换手、暴露与多重检验要求；缺证据不得新晋级 |

当前财报/估值任务每次显式选择最多100只股票，财报按报告期展示，页面读取缓存并显示任务状态。全市场财报覆盖是后续独立任务，不能把“已抓取的热门候选”当作全市场样本训练财务因子。按每股三张报表及一个报告期指标各调用一次估算，100只约需400次数据请求，5,000只约需20,000次，尚不含更多指标报告期、重试和其他接口；这只是请求量估算，不能推导费用。当前记录请求尝试预算及文件摘要/大小，仍无已核实的计价器。

本轮测试与账号证据分开记录于[测试计划](TEST_PLAN.md)，运行入口、变量和导出方法见[运行手册](OPERATIONS.md)。下一步目标是：**对已落地的真实行情与企业行动做小范围对账，核实财报单位、首次披露和修订版本等 PIT 证据及费用政策；完成联网增量验收后，再单独评估前复权转换和正式研究准入。**
