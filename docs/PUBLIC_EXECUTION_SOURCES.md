# 公开执行事实资料：来源、费用与核验边界

公开行情可以补充停牌声明、历史ST标记、未复权价格及昨收参考，用于定位当前缺失原因和交叉核验。它们不能仅凭接口名称、付费账号或多个来源一致，就取得项目的正式执行证据资格。当前目标是保存真实供应商事实及其缺口，不改写历史扫描状态，不改变生产评分，不据此执行交易。

以下官方资料核对于2026-09-20。BaoStock官网已改为动态文档页面；下表同时记录公开页面与其实际读取的原文路径，避免继续引用失效的旧Wiki页面。

## 使用

在项目根目录运行。`collect`联网访问无用户凭证的BaoStock及两所公开网页；`inspect/compare`只读本地缓存，不重新请求来源。

```bash
.venv/bin/python tools/collect_public_execution_facts.py collect --date 2026-09-18
.venv/bin/python tools/collect_public_execution_facts.py inspect --date 2026-09-18
.venv/bin/python tools/collect_public_execution_facts.py compare \
  --date 2026-09-18 --symbol 600519.SH --symbol 000001.SZ
```

默认缓存目录是`data/research/public_execution`，全局`--root PATH`应放在子命令之前。`collect`可重复指定`--provider baostock / sse_public_notice / szse_public_notice`选择来源；省略日期时使用最近完成交易日。当天17:45之前不会采集当天日批次；缺少可信日历覆盖时不猜交易日、不刷新日历。

每个自然日最多12次逻辑采集，每个供应商/目标日期一天最多尝试一次，失败也消耗该次预算。已验证成功的日期复用缓存；交易所分页及一致性复读会产生多次HTTP请求，因此逻辑采集数不等于HTTP请求数。原始响应按摘要保存，成功指针须重新验原文及规范化结果才可读取；失败材料不能只因解析器后来放宽就追认为成功。

前瞻计划的`evidence`会自动加载这份全局缓存，按已冻结的股票/日期需求进行对照；可以显式指定目录：

```bash
.venv/bin/python tools/collect_strategy_prospective.py \
  --plan-id '<已冻结且源码仍匹配的计划名>' evidence \
  --public-root data/research/public_execution
```

对照结果逐项保留`reported/missing/pending/unsupported/error`状态和来源摘要；`reported`仅表示BaoStock有对应声明，不是所有执行证据齐全。交易所公告单独展示，未找到对应公告不会被解释成正常交易。当前北交所明确标为`unsupported`，不会从需求集合删除。

## 官方文档与SDK

| 官方来源 | 页面及原文定位 | 本次核对内容 |
| --- | --- | --- |
| BaoStock平台介绍 | [home.md页面](https://www.baostock.com/mainContent?file=home.md)；`POST /helpdocs/api/markdown/home.md` | 免费、无需注册及数据更新时间 |
| BaoStock历史K线 | [stockKData.md页面](https://www.baostock.com/mainContent?file=stockKData.md)；`POST /helpdocs/api/markdown/stockKData.md` | 复权方式、日线字段、停牌填值及除权昨收参考 |
| BaoStock每日更新 | [DailyUpdates.md页面](https://www.baostock.com/mainContent?file=DailyUpdates.md)；`POST /helpdocs/api/markdown/DailyUpdates.md` | 按日全市场日线、ETF日线和当日复权因子接口 |
| BaoStock数据说明 | [dataExplain.md页面](https://www.baostock.com/mainContent?file=dataExplain.md)；`POST /helpdocs/api/markdown/dataExplain.md` | 停牌时不同K线频率的返回行为 |
| BaoStock官方发布包 | [PyPI 0.9.3](https://pypi.org/project/baostock/0.9.3/) | 2026-07-10发布；本项目已锁定该版本，核对安装包的`security/history.py`、`data/resultset.py`及`util/socketutil.py` |
| Tushare日线 | [daily](https://tushare.pro/document/2?doc_id=27) | 未复权、停牌缺行、除权昨收、量额单位 |
| Tushare停复牌 | [suspend_d](https://tushare.pro/document/2?doc_id=214) | 停牌/复牌类型、日内时段、权限和行数上限 |
| Tushare涨跌停价格 | [stk_limit](https://tushare.pro/document/2?doc_id=183) | 每日涨跌停价格及可选参考字段 |
| Tushare权限与费用 | [权限频次与价格](https://tushare.pro/document/1?doc_id=290)、[积分说明](https://tushare.pro/document/1?doc_id=13) | 免费档、2000积分档、有效期及调用限制 |
| 上交所停复牌 | [股票和可转债停复牌](https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/)、[法律声明](https://www.sse.com.cn/home/legal/) | 公开停复牌记录、非商业使用条件及完整性限制 |
| 深交所停复牌 | [交易提示](https://www.szse.cn/disclosure/memo/index.html)、[法律声明](https://www.szse.cn/application/laws/index.html) | 按证券类型、停复牌日期及日内时段解释的公开记录 |

上述BaoStock原文路径均位于`https://www.baostock.com`，网页以空JSON请求读取公开帮助内容，不是市场行情接口。URL是文档定位信息，不是不可变文件身份；采集数据仍应保留当次接口参数、版本、原始响应与摘要。

## 费用与优先顺序

BaoStock官方声明免费、无需注册，标准示例直接使用`bs.login()`；SDK支持匿名会话。优先以它核验已有问题股票，再验证按日批量读取，避免为同一天逐一请求数千只股票。免费不代表无限流量、完整市场覆盖或可转授权分发，SDK的开源许可也不能代替行情来源许可。[BaoStock平台介绍](https://www.baostock.com/mainContent?file=home.md)

Tushare当前公开个人档位为：120积分可调用非复权日线，价格0元、50次/分钟、8000次/日；2000积分档200元/年、200次/分钟，具体接口还须满足各自门槛。`suspend_d`和`stk_limit`均要求2000积分。积分是权限门槛，在有效期内不按调用扣减；机构价格、独立权限及最终账号资格另计。本次不购买或升级订阅。[价格表](https://tushare.pro/document/1?doc_id=290)、[积分有效期](https://tushare.pro/document/1?doc_id=13)

`daily`接口页仍有较高频次描述，与权限表不完全一致。工程上按账号实际权限和更低限制执行，遇权限/限频错误明确停止；不把错误或空响应解释为当天没有交易。Tushare只有在已有授权账号和相应权限时才作为补充，不因发现可用SDK就自动调用。

## BaoStock日批次的字段合同

`query_daily_history_k_AStock(date="YYYY-MM-DD")`一次查询指定日期的股票日线，官方示例返回18列，按以下顺序展示：

```text
date,code,open,high,low,close,preclose,volume,amount,adjustflag,
turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST
```

日批次接口只有日期参数，不能传复权参数；应逐行核验返回的`adjustflag`为`"3"`，不能凭示例默认全批未复权。字段名及次序、日期、代码、重复行和完整读取状态都属于核验内容。[官方每日更新文档](https://www.baostock.com/mainContent?file=DailyUpdates.md)

| 字段 | 官方语义 | 本项目应保留的限制 |
| --- | --- | --- |
| `date/code` | 交易日期及`sh./sz.`证券代码 | 必须与请求和股票身份匹配；未返回的代码保留缺口，不补零或自动映射成另一股票 |
| `open/high/low/close` | 人民币价格 | 必须是有效有限值并符合价格关系；停牌填充值不是实际成交 |
| `volume/amount` | 股数/人民币元 | 显式0与空值不同，不能用缺失值构造零成交 |
| `preclose` | 当日行情的前收盘参考 | 除权除息日与前日实际收盘可能不同，首发日为发行价 |
| `tradestatus` | `1`正常交易、`0`停牌 | 作为供应商声明保存；正常交易不等于开盘或收盘订单能够成交 |
| `isST` | `1`是、`0`否 | 历史状态线索，不单独确定板块、历史涨跌幅规则或可投资资格 |
| `turn` | 换手率，百分数 | 停牌时可为空，原始空值应保留 |
| `pctChg` | 基于当日前收参考的百分比涨跌幅 | 不直接当作持仓总收益；不能代替公司行动账本 |

BaoStock说明日线在停牌日仍有行：OHLC填为前一交易日收盘价，量额为0，换手率为空；分钟线则无停牌数据。单一价格或零成交量只能交叉检查，不能反向推出已知停牌。其日线通常17:30入库，复权因子18:00、分钟线20:00；这些是供应商更新安排，不是本项目可以提前生成的可得时间。[K线字段及填值](https://www.baostock.com/mainContent?file=stockKData.md)、[数据说明](https://www.baostock.com/mainContent?file=dataExplain.md)、[更新安排](https://www.baostock.com/mainContent?file=home.md)

本轮真实响应与文档填值存在差异：2026-09-07及09-18的明确停牌行中，成交量、成交额会返回空字符串。适配器只在`tradestatus="0"`时允许这两个字段为空，规范化为`null`并保留原字符串，不补成0；其他状态的缺失量额仍拒绝准入。它保留的是供应商停牌声明，不能从空值反推出停牌。

北交所及全部退市历史的实际覆盖不能从接口名称推定。应以冻结需求股票集合逐项检查；供应商不支持、无行、请求失败、响应无效分别保留，不能删除这些股票后声称全市场完整。

## SDK调用与原始资料保存

0.9.3实际返回`ResultData`；官网部分文字称DataFrame，但其示例也是从结果集构建DataFrame。日批次本身不分页，SDK通用`next()`却可能在结果恰为2000行时错误尝试分页。因此本项目直接保存已交付的`fields/data/error_code`，不调用此端点的通用迭代器。内部单页参数为20000，不等于服务保证完整覆盖；达到潜在截断上限或超过字节限制时明确失败，不静默截断。

官方SDK使用进程内全局会话。其socket代码未设置连接/读取超时，断连时还可能留在接收循环。因此批量采集需要独立子进程、硬超时和结果大小限制，超时后结束子进程并保留失败记录，避免常驻服务工作线程被长期占用。一次采集不应与其他SDK会话共享全局状态；不要修改SDK安装包来绕过这些边界。

原始归档应绑定供应商、接口、请求日期、字段、SDK版本、开始/完成时间、响应状态、行数和内容摘要。字符串空值、停牌填值及原字段顺序应原样保留，规范化结果另存。实际抓取时刻与历史交易日期分别保存；今天补取历史行情不能标成过去当天已取得。

## Tushare补充资料的歧义

`daily`是未复权行情，停牌期间不返回行；`pre_close`为除权昨收，`vol`单位为手、`amount`单位为千元。跨源对账须先统一单位及日期，价格用未复权字段，不能与现有前复权缓存直接比较。缺行可能涉及停牌、未上市、不支持、截断或请求失败，不能单凭日线缺行判定停牌。[日线官方合同](https://tushare.pro/document/2?doc_id=27)

`suspend_d`提供`S/R`事件及日内停牌时段，更新不定期，单次最多5000行。应保存时段原文并区分全天和日内停牌；一条停牌事件不自动证明整日都无法交易，空结果也不证明没有停牌。[停复牌官方合同](https://tushare.pro/document/2?doc_id=214)

`stk_limit`通常在交易日9点左右更新，包含A/B股及基金，单次最多5800行。`pre_close`默认不显示，需显式请求。涨跌停价是价格限制；即使开高低收都等于涨停价，也不能凭日线证明某笔买单排队成交或绝对无法成交。缺少上下限的行不能解释为无涨跌幅限制，必须补充当时市场规则及上市阶段事实。[涨跌停价格官方合同](https://tushare.pro/document/2?doc_id=183)

## 交易所公开停复牌记录

交易所网页可以提供另一类直接事实，用于核验供应商的停牌声明。上交所页面使用`query.sse.com.cn/commonSoaQuery.do`的`GW_PL_JYTS_TFPXX`查询，深交所页面使用`www.szse.cn/api/report/ShowReport/data`的`CATALOGID=1798/TABKEY=tab1`。这些是公开网页查询入口，需保留请求区间、分页参数、原始响应和抓取时刻，不能假定是稳定、有服务保证的正式数据交付接口。

2026-09-20本轮公开查询验证中，2026-09-18上交所结果17条，含持续停牌；深交所结果6条，均为ETF日内停牌。上交所9月1日至18日查询还验证了25条加12条的分页。单日结果不构成全市场逐证券正常/停牌状态表；ETF停牌不能当成股票停牌，区间事件需按其起止日期和时段解释，空列表不能反推全部正常交易。

深交所还存在“取消停牌”记录：开始时刻为空，恢复时刻有值。规范化保留为`resumption_notice`及原时段，与停牌公告分开；不会将它当成停牌或开盘可成交证明。

两所公开法律声明允许符合条件的非商业浏览、下载，对出售牟利等用途要求书面许可；公开页面也不提供完整性和及时性保证。本地研究核验不等于已取得正式行情再分发授权。项目保存公开记录和具体事实，不凭网页可访问就伪造许可注册表。[上交所法律声明](https://www.sse.com.cn/home/legal/)、[深交所法律声明](https://www.szse.cn/application/laws/index.html)

## 与当前项目证据的关系

已有BaoStock提供者只读取前复权OHLCV，Tushare提供者读取前复权`pro_bar`；这些缓存没有保存本节所需的完整原始状态。新增事实核验应使用独立研究资料目录，不把未复权行混入前复权缓存，不回写已封存扫描，不把`missing`改成`skipped`。

不同来源字段提供的是不同事实：日线说明价格与成交汇总，停复牌接口说明状态事件，涨跌停接口说明价格边界，公司行动资料说明权益变化。相互印证可以定位冲突，却不自动证明来源独立、数据完整或订单可成交。错误相同的两个供应商也可能使用同一个底层来源。

公开资料继续标记`source=public_vendor_research`、`official_execution_admitted=false`、`point_in_time_verified=false`。正式净执行还需要许可来源登记、独立固定摘要、原始交付绑定、历史规则、完整公司行动覆盖和入场/退出状态等严格准入；不能由公开采集器生成已验证正式会话。既有边界见[策略模板对照](STRATEGY_TEMPLATE_TRACKING.md)和[前瞻采集](STRATEGY_PROSPECTIVE_COLLECTION.md)。

## 验证顺序

1. 用无用户凭证的独立进程查询一只股票和一个已完成交易日，明确`adjustflag="3"`，验证字段及有限数值；控制硬超时。
2. 再查询同一天的日批次，校验原始字段、完成状态、唯一代码及日期；保留全部响应后按冻结需求关联。
3. 对既有缺失股票逐项给出供应商声明、无行、无效或冲突结论；原生产状态保持不变。
4. 仅在取得实测响应后记录覆盖和费用情况；不能由文档示例宣称日批次已成功、北交所已覆盖或正式证据已补齐。

## 本轮真实采集状态（2026-09-20）

| 目标日期 | BaoStock规范行数 | BaoStock报告停牌 | 上交所公告 | 深交所公告 |
| --- | --- | --- | --- | --- |
| 2026-09-07 | 5216 | 9 | 6 | 本次未准入；首次解析遇到2条取消停牌记录，缺少完整一致性复读 |
| 2026-09-18 | 5221 | 12 | 17 | 6，均为ETF日内停牌 |

09-07深交所解析规则已按原始事实修正，但原请求缺少完整复读材料，不能追认合格；当天不重复请求以绕过尝试预算。两日BaoStock数字仅表示本次实际响应行数，均不代表全A股覆盖。

对原`run_id=166`的15条缺失股票保持固定集合核验，全部取得BaoStock行：9条报告停牌、6条报告正常交易；3只沪股停牌还关联到上交所公告。六条正常交易声明中的单一价格形态仍不能证明开盘成交。生产扫描及其缺失状态保持原样；该核验改善的是缺口解释，没有完成正式执行证据准入。

## 本轮验证与前瞻衔接

2026-09-20整合验证覆盖30个相关测试模块，1092项全部通过；Ruff、457个登记源码文件的mypy、Python编译/Pyflakes、仓库一致性及密钥泄漏扫描通过。覆盖率报告为91.56%：未修改源码沿用上一轮覆盖数据，本轮7个新增/修改生产文件先清除旧覆盖记录再重测；这不是重新跑完整项目测试套件。

9月18日三路采集复跑均返回`cached`，9月7日深交所失败保留为`attempted_today`，未产生额外网络请求。真实原始响应、结果摘要及历史批次166核验报告保存在忽略目录`data/research/public_execution`，API密钥继续只保存在本地，未新增付费订阅。

本轮代码加入不可变前瞻计划`strategy-templates-20260921-phase1-rev2`，旧计划在开始前、零收据时被明确替代并完整保留。现有自动采集任务已切到rev2，工作日15:45、17:45、21:45检查，免费资料仅17:45以后读取；首个信号日仍是2026-09-21。运行前必须检查源码和日历一致性。当前没有成熟前瞻结果，也没有依据正式净收益采用任一策略。
