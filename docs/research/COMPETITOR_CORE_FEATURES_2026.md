# 主流单股研究功能调研与取舍（2026）

> 调研日期：2026-07-15  
> 产品边界：本地、单用户、A 股研究辅助；不接券商、不下单，不把抽样或推导结果包装成全市场或权威数据。  
> 资料口径：只采用产品官网、官方帮助中心与交易所页面。官方资料没有公开功能使用频次，因此本文将“主流”定义为至少三类产品反复出现、且服务于日常单股研究的工作流。

## 1. 共同工作流

国内外产品的界面规模不同，但核心路径高度一致：

1. 从搜索或观察列表确定研究对象。
2. 在固定股票上下文中查看价格、周期和关键事件。
3. 用图表精确值、财务趋势和同业比较核对结论。
4. 通过自选、笔记、提醒和事件记录保存研究状态。
5. 再次进入时延续上次上下文，而不是重新开始一次孤立分析。

这意味着 AShareRadar 的优先级不应由“新增多少卡片”决定，而应由研究上下文是否连续、数据口径是否可证明决定。

## 2. 官方证据矩阵

| 产品 | 官方资料 | 可复用的核心模式 |
| --- | --- | --- |
| 东方财富 | [个股行情](https://quote.eastmoney.com/sh600519.html)、[F10](https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?code=SH600519&color=b&type=web)、[公司日历](https://data.eastmoney.com/stockcalendar/600519.html)、[公告](https://data.eastmoney.com/notices/stock/600519.html) | 固定股票上下文；多周期与复权；公司、经营、财务、分红和行业分层；公告按类别和时间组织。 |
| 同花顺 | [个股 F10](https://basic.10jqka.com.cn/600519/index.html)、[技术分析帮助](https://www.10jqka.com.cn/ad_mar/man/05-2.htm)、[公司大事](https://basic.10jqka.com.cn/600519/event.html) | 分时与 K 线快速切换；周期、复权、叠加股票和指标；区间涨跌、成交量与换手统计；近期事件到专题明细。 |
| 雪球 | [个股图表](https://xueqiu.com/S/SH600519/time)、[自选股帮助](https://xueqiu.com/about/faq/3/1)、[个股公告](https://xueqiu.com/S/SH600519/notices) | 周期分段控件；区间统计和多股比较；自选分组与价格提醒；公告回到原始内容。本文不采用社区功能。 |
| 富途 | [个股行情](https://support.futunn.com/topic38?lang=zh-cn)、[技术指标](https://support.futunn.com/topic68)、[财务数据](https://support.futunn.com/topic85?from_platform=1&lang=zh-cn)、[提醒](https://support.futunn.com/topic597) | 股票状态和成交时间常驻；主副图独立配置；报告期与同比；提醒类型、阈值、频率和集中管理。 |
| TradingView | [比较工具](https://www.tradingview.com/support/solutions/43000543053-how-to-use-the-compare-tool/)、[基本面图](https://www.tradingview.com/support/solutions/43000763376-fundamental-graphs-learn-to-chart-financial-metrics/)、[图表同步](https://www.tradingview.com/support/solutions/43000629992-how-to-sync-the-charts-of-my-layout/)、[观察列表](https://www.tradingview.com/support/solutions/43000745825-mastering-the-tradingview-watchlists/) | 百分比比较；一股多指标或一指标多股；标的、十字线、周期和日期范围同步；观察列表承载分段、列与笔记。 |
| Koyfin | [财务分析模板](https://www.koyfin.com/help/financial-analysis-templates/)、[历史图表](https://www.koyfin.com/help/charts-and-graphs/)、[观察列表](https://www.koyfin.com/help/mywatchlists/) | 财务序列复用；表图切换、多轴和模板；观察列表分组、排序、统计和拖入研究视图。 |
| Yahoo Finance | [股票比较](https://finance.yahoo.com/compare)、[公司事件图层](https://help.yahoo.com/kb/finance-for-web/show-events-yahoo-finance-web-charts-sln5686.html)、[自定义视图](https://help.yahoo.com/kb/SLN5231.html) | 有上限的并列比较；公司事件投影到图表；保存字段与顺序；导入、导出和笔记。 |
| TIKR | [财务数据](https://support.tikr.com/hc/en-us/articles/5381719620891-How-to-view-detailed-financial-data-on-100-000-stocks-globally-on-TIKR)、[预测](https://support.tikr.com/hc/en-us/articles/39071375390235-How-do-I-use-TIKR-s-Estimates-feature)、[观察列表](https://support.tikr.com/hc/en-us/articles/5365387794203-How-to-set-up-a-watchlist-on-TIKR) | 财务行直接成图并加入同业；历史实际值与预测分区；观察列表定位为研究跟踪而非持仓收益管理。 |

## 3. 对 AShareRadar 的选择

### 3.1 本轮落地

| 能力 | 选择原因 | 数据与维护边界 |
| --- | --- | --- |
| 股票代码 / 名称自动补全 | 当前后端已有 `/api/stocks?keyword=` 和缓存股票池，但页面只能输入六位代码；补齐发现入口能直接降低切换成本。 | 复用现有股票池；输入触发才请求；防抖、取消旧请求、缓存结果；失败不得影响当前工作台。 |
| 日线 / 分钟图精确值检查 | 多周期、十字光标和精确值是国内外图表产品共同基础；当前画布只能看形状，不能核对某根 K 线。 | 只读取已加载 K 线；不增加 provider 请求；鼠标、触控和键盘共享同一命中模型。 |
| 本地研究活动时间线 | 项目已有建议留痕、提醒事件和笔记，但分散在三个面板，不能按时间回答“最近发生了什么”。 | 只合并现有本地数据；不得增加切股请求；必须标明部分来源读取失败，而不能伪装为空。 |

### 3.2 暂缓

| 能力 | 暂缓原因 | 放行条件 |
| --- | --- | --- |
| 官方公告 / 公司事件日历 | 主流且高价值，但当前没有稳定、明确授权并可长期维护的统一事件源。 | 每条记录具备官方来源、发布时间、事件日期、类别、原文链接、内容哈希、去重和修订策略。 |
| 正式 F10 财务趋势与历史估值 | 当前财务面板仍主要是市场估值与交易体征，不能用行情字段冒充正式报表。 | 合法财报源；报告期、披露时间、重述、币种和单位完整；有防未来函数测试。 |
| 任意多股公式、复杂绘图与脚本市场 | 初始和持续维护成本高，且偏离本地单股研究主路径。 | 核心连续性与数据可信度长期稳定后重新评估。 |

## 4. 横向验收条件

本轮新增不以视觉存在为完成标准，必须同时满足：

- 股票切换仍只产生既有股票域请求，搜索请求只能由用户输入触发。
- 旧搜索、旧时间线或旧图表状态不能覆盖新股票。
- 数据不可用、确实为空和尚未加载必须是不同状态。
- 桌面和窄屏均不遮挡图表、输入框或相邻内容。
- 键盘能够完成搜索候选选择和图表逐点检查。
- 所有文本均来自转义后的数据；错误信息不泄露 provider 凭据或本地路径。

