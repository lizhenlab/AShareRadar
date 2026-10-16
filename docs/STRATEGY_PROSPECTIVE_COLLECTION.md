# 三策略前瞻采集与执行证据

`tools/collect_strategy_prospective.py`在未来信号出现之前固定三模板、日期、评分合同和源码身份，随后按时保存完整扫描输入，再为已冻结篮子收集执行资料和追加结果。它解决的是“从现在开始留下可复验的样本”。历史缺失不能补成当时已经取得的证据；当前流程也不授予策略采用资格，不改变生产评分或执行真实交易。

三个固定模板是`bounded_medium_trend`、`medium_momentum`和`low_volatility_trend`。模板规格、版本、策略指纹、名义资金与比较合同均写入计划，不随未来收益调整。净执行口径及回溯统计工具见[策略模板对照](STRATEGY_TEMPLATE_TRACKING.md)。

## 命令与文件

在项目根目录执行。第一阶段计划`strategy-templates-20260921-phase1-rev2`已在实现冻结后初始化；其历史记录必须继续使用同一计划名。2026-09-22运行维护优化改变了源码，该计划当前仅供状态与原档案核对，不能继续采集或计算。下列命令说明原流程，不表示可以绕过源码漂移保护。全局参数`--root`、`--plan-id`放在子命令之前。

```bash
.venv/bin/python tools/collect_strategy_prospective.py \
  --plan-id strategy-templates-20260921-phase1-rev2 init \
  --start-date 2026-09-21 --end-date 2026-12-31 \
  --horizon 10 --notional-cash-cny 1000000 --cutoff-local 20:00:00

.venv/bin/python tools/collect_strategy_prospective.py \
  --plan-id strategy-templates-20260921-phase1-rev2 status

.venv/bin/python tools/collect_strategy_prospective.py \
  --plan-id strategy-templates-20260921-phase1-rev2 capture \
  --database data/ashare_radar.sqlite3

.venv/bin/python tools/collect_strategy_prospective.py \
  --plan-id strategy-templates-20260921-phase1-rev2 evidence \
  --history-root data/ashare_radar.fuyao/history \
  --public-root data/research/public_execution

.venv/bin/python tools/collect_strategy_prospective.py \
  --plan-id strategy-templates-20260921-phase1-rev2 evaluate
```

`init`必须在起始日期之前执行；上述日期过期后，不能用它补登记历史试验，应另选未来区间和新的计划名。

| 子命令 | 当前行为 |
| --- | --- |
| `init` | 冻结计划；相同参数重试保留原时间与摘要，不覆盖已有计划 |
| `status` | 校验计划和收据链，显示下一信号日、到期/逾期数量、收据状态及源码漂移 |
| `readiness` | 只读分层核对实现、最新首批输入、样本成熟度和正式材料需求；诊断不登记信号 |
| `capture` | 只读本地数据库，在采集窗口内冻结三模板篮子和完整扫描输入；不启动扫描，不读取后续价格 |
| `evidence` | 只读现有扶摇历史档案及免费公开事实归档，按冻结股票清单抽取所需日线、关联公司行动、停复牌资料及来源摘要；不请求供应商 |
| `evaluate` | 仅为按时准入的冻结篮子追加独立批次净执行报告；没有正式执行材料时如实显示不可用 |

六个命令都不请求行情或供应商额度。除`status`和`readiness`只读外，其余命令会在本地计划目录写入不可变文件。默认根目录为`data/research/strategy_prospective`，可用`--root`指定其他本地目录；该目录应另行备份，不提交Git。

`readiness`的v2报告将`basket_input.completeness_status`与`freshness`分开：前者说明首批输入是否满足原准入合同，后者包含来源日期、期望已发布交易日和`current/stale/unknown/ahead_of_expected`状态。以本地可信交易日历和上海15:15发布阈值判定，盘前和周末仍使用最近应发布的交易日；查询不触发日历远端获取。完整旧来源显示`source_stale`，日历无法判定时显示`freshness_unknown`，共享来源校验缺少日历路径时保留`source_integrity_unknown`。`current_readiness.pending_conditions`同时包含输入、日期及源码/日历漂移问题；其ready范围不包含运行依赖、已捕获样本或正式执行材料。

```text
<root>/<plan-id>/
  plan.json
  receipt-YYYY-MM-DD.json
  snapshot-YYYY-MM-DD-<摘要>.json.gz
  research/
    execution-<摘要>.json
    outcomes-<摘要>.json
```

收据按固定日期顺序串联前一摘要，每个信号日只登记一次；报告按内容摘要另存。不要手工修改计划、收据或快照。`evidence`和`evaluate`会重新检查已引用扫描归档的压缩字节、原字节摘要、来源身份及全部结果行，并在独立内存数据库重放准入和三模板选股；重放篮子必须与原收据完全一致。报告固定同一次读取的收据前缀，并记录`head_digest`、`receipt_count`和`as_of`，不依赖运行数据库永久保留这些行。

## 计划、日历与源码变化

`init`冻结可信交易日历原文及摘要，按完整日历每`H+1`个交易日安排一个信号锚点。支持`H=1/5/10/20`；默认`H=10`、名义资金100万元，净执行为`D+1`开盘入场至`D+H+1`收盘退出。该间隔从计划日期轴产生，不根据成功采集日期重新排队。

2026-09-19核对的第一阶段设计为2026-09-21至2026-12-31、`H=10`。当前内置日历截至2026-12-31，得到以下7个锚点，其中6个退出日已被该日历覆盖；“可覆盖”不等于已经成熟或已经取得数据。

| 信号日D | 固定退出日D+11 |
| --- | --- |
| 2026-09-21 | 2026-10-14 |
| 2026-10-14 | 2026-10-29 |
| 2026-10-29 | 2026-11-13 |
| 2026-11-13 | 2026-11-30 |
| 2026-11-30 | 2026-12-15 |
| 2026-12-15 | 2026-12-30 |
| 2026-12-30 | 当前冻结日历不覆盖，保持空值 |

后续扩展本地日历不会改写原计划中的退出日。不能把日历外退出日临时猜成某个工作日，也不能将最后一个锚点静默删除。第一阶段主要验证采集与证据流程；7个计划锚点也未达到回溯选择工具要求的20个完整锚点，不能据此承诺确定收益赢家。

每次执行前还会比对运行时实际使用的交易日轴及每个锚点的完整持有路径。运行时日历修订改变这些日期，或扩展后使原本未知的退出日变为可推导时，`status`显示`calendar_drift`，采集和计算拒绝继续；原冻结记录仍可读取。仅源码摘要一致不能绕过日历检查。

源码清单包含`app/`和`tools/`下全部Python文件的摘要。新增、删除或修改这些源码后，`status`显示`source_drift`，其他采集及结果操作拒绝继续使用原计划。应保留旧计划，核对变更后为新的未来区间另建计划，不能重算摘要或修改旧`plan.json`来掩盖变化。计划创建后应优先保持实验实现稳定。

## 按时冻结信号

默认采集窗口为信号日上海时间15:15至20:00。先由现有全市场扫描功能完成`official`全市场批次，再执行`capture`。它读取一个只读SQLite事务，先确定当日最早发布的`success/degraded`批次，再校验评分合同、原始封印、全市场输入状态与PIT绑定。第一批不合格时保留失败，不用稍后的重扫替换。

当有批次可捕获时，归档包含扫描头、评分合同和全部结果行，包括`success`、`skipped`、`missing`；三策略候选只从计划冻结的规格及合格输入生成。保存时不读取未来行情，不按后续可得价格删除股票。单批最多10000条结果，压缩快照上限64MiB、解压原文上限512MiB，超限明确失败，不截断股票池。

| 收据状态 | 含义与后续处理 |
| --- | --- |
| `captured` | 窗口内完成留档且来源通过准入；篮子可进入后续证据收集 |
| `source_incomplete` | 批次已完整归档，但封印、合同、PIT或输入状态等未通过准入；保留具体安全原因，不参与净执行计算 |
| `missing` | 到截止时间仍未捕获；以后运行也保持缺失，不回查旧批次补成按时信号 |
| `late` | 捕获或来源可得时间跨过窗口；保留已有材料，但不纳入按时前瞻篮子 |

窗口内尚无批次时暂不写收据，可以稍后重试；截止后再次执行`capture`才会登记`missing`。仅执行`status`会报告逾期而不会补写收据。计算或归档跨过20:00截止可能变为`late`，因此应留出扫描和归档时间。缺失日期始终留在计划轴上，不能按成功日期压缩样本。

合格来源的`available_at`使用实际发布与封印时间中的较晚者，`observed_at`另外记录本次读取时刻。时间字段损坏的首批也会归档为`source_incomplete`；此时用实际观察时刻记录采集，不伪造源发布时间，也不改选后一批。

## 研究资料与正式执行证据

`evidence`先固定已准入三模板的入选股票并集、目标权重和来源身份，再要求每只股票从D至D+H+1的完整交易日资料。股票没有对应行情也仍在需求及缺口清单中；尚未完成的交易日记为待到期。没有已捕获篮子时返回`needs_source`和0个股票，不虚构执行样本。

扶摇现有规范历史档案的未复权日线可提供开高低收、成交量和成交额；公司行动原始行按股票及所需日期关联，保留同日多条及负股本变动记录。输出绑定原始及规范文件摘要、档案实际观察时刻与本次收集时刻。首次使用档案版本会完整校验文件摘要；同进程复用校验缓存时仍检查文件身份、大小和修改状态，避免每次重新加载全部历史。

这些材料固定标为`source=public_vendor_research`、`official_execution_admitted=false`、`point_in_time_verified=false`。哈希能检查内容是否变化，不能证明许可或历史时点可得。无成交记录不等于停牌，无企业行动行不等于明确`none`，正成交量不等于入场及退出可成交。未知状态必须继续列为未知。

正式净执行还需已核验的许可来源注册表、独立注册表摘要、原始交付及其收据、历史交易数量/涨跌幅规则、交易日状态、开盘入场和收盘退出状态、完整公司行动覆盖、交易所昨收参考。用[官方执行入库工具](RESEARCH.md)准备后，`evaluate`才可显式载入四项材料：

```bash
.venv/bin/python tools/collect_strategy_prospective.py \
  --plan-id strategy-templates-20260921-phase1-rev2 evaluate \
  --official-registry '<来源注册表.json>' \
  --official-registry-digest '<独立确认的64位SHA-256>' \
  --official-raw-root '<许可原始交付目录>' \
  --official-session-directory '<已封存执行会话目录>'
```

四项参数必须全部提供；无有效会话或验证失败时退出，不把普通历史档案自动升级为正式证据。全部省略仍可生成缺口报告；非空篮子不能因此产生净收益数值。基础与成本压力情景沿用[固定模板净执行合同](STRATEGY_TEMPLATE_TRACKING.md)，未知路径或固定退出受阻时整篮结果为空，不删股、不归一、不改退出日。

## 日常跟进与当前进度

调度应先执行`status`。只在计划锚点且没有当日可用首批时，按需启动一次正式全市场扫描，然后在截止前捕获；非锚点不为本实验重复扫描。日线更新与本地`evidence`分开：每个交易日最多执行一次`history_incremental`，发起前检查本地持久预算及既有后台任务，避免重复下载。一次成功增量路径需要两个签名接口请求及文件下载；预算计数不是供应商余额或免费证明。不得为补证据擅自购买订阅或向外部联系人发消息。

晚于20:00的检查用于登记缺失、检查结果与故障，不能补录按时信号。Codex任务现改为工作日上海时间19:45一次只读巡检，名称为“策略前瞻样本采集”，自动化ID为`automation`。实际触发以任务配置为准，上述CLI本身没有内置定时器。本地执行需要电脑与Codex保持运行，并能访问8010服务；状态不变时不发送常规通知。

rev2计划摘要保持`dcb45b579e39ca7aed9287af638c63be017a6f2b2dd40ce6a4f623fc853ba2e5`。2026-09-21已按时归档首批正式扫描176：5565条输入中5421成功、127跳过、17缺失，收据为`source_incomplete`，并未形成合格篮子。缺失原因不能通过后续公开资料或重扫改写；目前合格前瞻样本和正式净执行结果均为0。

2026-09-22经用户要求优化后台调度、行业传输和概率维护后，原计划进入`source_drift`保护，保留原计划、收据和快照。修改前源码已保存到忽略提交的`data/research/strategy_prospective_source_archives/<原计划摘要>.tar.gz`，其中577项源码逐项与原清单匹配；该本地备份不代表独立认证。定时跟进须在发现漂移后停止依赖旧计划的采集和计算，不自动另建试验。

如用户确认续采，可在实现再次冻结后为未来区间建立新版本：建议从原下一个锚点2026-10-14开始，保持原剩余6个信号日、10交易日持有期、100万元名义资金、20:00截止及2026-12-31阶段结束。旧9月21日失败仍单独保留，新版只有5个退出日受现有日历覆盖；两版本不得合并宣称同一源码试验的完整样本。当前尚未创建该续采计划。

2026-09-23已按冻结日历复核的续接草案如下；它是供确认的实施方案，不是新试验登记：

| 新计划信号日 | 固定入场日 | 固定退出日 |
| --- | --- | --- |
| 2026-10-14 | 2026-10-15 | 2026-10-29 |
| 2026-10-29 | 2026-10-30 | 2026-11-13 |
| 2026-11-13 | 2026-11-16 | 2026-11-30 |
| 2026-11-30 | 2026-12-01 | 2026-12-15 |
| 2026-12-15 | 2026-12-16 | 2026-12-30 |
| 2026-12-30 | 2026-12-31 | 当前冻结日历不覆盖，保持空值 |

拟用ID为`strategy-templates-20261014-phase1-rev3`。先完成全部生产源码验证，用户确认后最晚于上海时间10月13日初始化；检查新摘要、源码及日历匹配、零收据、6个锚点和5个已覆盖退出日，再按新源码清单备份。只有这些步骤成功，才将现有自动化切换到新ID、目录和摘要，保留原检查时刻、预算及证据边界。首次锚点前不为该实验扫描或下载；后续再次源码漂移仍停止依赖该计划的操作。正式执行材料与独立认证缺口不因续接而消失。

### 固定运行环境与程序采集（2026-10-08已实现）

工程修改与实验运行分开：`tools/strategy_prospective_runtime.py`离线实复制`app/`、`tools/`、JSON资源和Python环境，封存完整文件摘要、依赖锁、实际安装包、解释器/共享标准库及显式非敏感配置。生产依赖须逐项匹配`requirements-lock.txt`，额外安装包也记录并校验全部文件。该副本仍使用完整Python源码清单，没有缩小旧计划的准入范围。

```bash
PROSPECTIVE_PROJECT="$(pwd)"
.venv/bin/python tools/strategy_prospective_runtime.py prepare \
  --source-root "$PROSPECTIVE_PROJECT" \
  --environment-root "$PROSPECTIVE_PROJECT/.venv" \
  --destination "$PROSPECTIVE_PROJECT/data/research/strategy_prospective_runtimes/prepared-20261008"

.venv/bin/python tools/strategy_prospective_runtime.py inspect \
  "$PROSPECTIVE_PROJECT/data/research/strategy_prospective_runtimes/prepared-20261008"
```

`prepare`只接受新的目录，不能覆盖旧副本。启动使用副本的解释器、实复制的包和`-I -S -B`，不继承主项目环境、`PYTHONPATH`、用户包或`.pth`执行钩子，关闭shell配置回退及供应商/自动调度。它不复制项目`.env`或`data/`，不加载凭据；读取生产数据必须显式传入绝对路径。质量门槛默认固定为50，旧审计时区固定为上海；修改配置须形成新的副本。Python基础安装的标准库仍共享，但每轮校验其内容；基础解释器升级或标准库改变同样阻断。运行副本是本地完整性记录，不是独立认证。

副本运行前完整校验。持续采集每轮再次核对启动摘要、全部文件及实际非敏感配置，依赖或资源改变不能在运行中继续。主项目后续源码变化不会修改副本，但来自生产数据库的扫描仍必须通过固定实现的合同、封印和PIT检查。旧rev2继续源码漂移，不能因创建新副本而恢复其资格。

所有示例在项目根目录运行，`PROSPECTIVE_PROJECT`取当前项目的绝对路径。

**分层就绪。** 新增离线`readiness`命令，只读SQLite一致事务和已验证计划/收据链，区分以下四层；SQLite可维护WAL协调侧文件，不改变数据库内容或研究归档。

```bash
PROSPECTIVE_PROJECT="$(pwd)"
.venv/bin/python tools/collect_strategy_prospective.py \
  --plan-id strategy-templates-20260921-phase1-rev2 readiness \
  --database "$PROSPECTIVE_PROJECT/data/ashare_radar.sqlite3"
```

| 层 | 检查与边界 |
| --- | --- |
| `framework` | 计划/收据完整性、源码、日历及逾期；源码匹配不代表依赖已受检，依赖与配置由runtime封存工具检查 |
| `basket_input` | 最近已发布日期的首个official全市场批次，逐项诊断评分合同、封印、完整性、PIT、缺失原因；它只是生产输入诊断，不产生或修复前瞻样本 |
| `sample_maturity` | 已captured收据按固定退出日分为成熟、未到期、日历不覆盖；时点成熟不代表归档重放或净执行通过 |
| `formal_execution` | 未向本命令提供并核验四项材料时保持not_assessed，列出材料需求；不自动搜索文件，也不把公开资料升级为正式证据 |

缺失诊断只根据已经记录的原因分类，保留`missing`和未知交易状态，不把日K滞后或单一价格猜成停牌。输入第一批不合格时不会用后批替代。新副本和只读诊断不能解决供应商缺口，也不能重写旧失败。

**程序负责截止与去重。** `tools/run_strategy_prospective.py`仅从受检runtime启动，计划摘要、生产数据库路径和控制目录必须显式绑定。控制目录固定为计划根目录的兄弟`strategy_prospective_control`，换`--control-root`不能重置当日已尝试状态。锁覆盖检查、预占及提交；任何模式的活动扫描均先等待，不取消用户任务。

```bash
PROSPECTIVE_PROJECT="$(pwd)"
.venv/bin/python tools/strategy_prospective_runtime.py run \
  --directory "$PROSPECTIVE_PROJECT/data/research/strategy_prospective_runtimes/prepared-20261008" \
  --entrypoint run_strategy_prospective.py -- \
  --root "$PROSPECTIVE_PROJECT/data/research/strategy_prospective" \
  --plan-id '<未来已登记计划ID>' --expected-plan-digest '<已确认的计划摘要>' \
  --database "$PROSPECTIVE_PROJECT/data/ashare_radar.sqlite3" \
  --control-root "$PROSPECTIVE_PROJECT/data/research/strategy_prospective_control" --watch
```

采集窗口内每60秒检查并优先捕获已发布首批。没有首批、没有活动扫描、当日没有预占记录，且服务就绪并返回与绑定数据库一致的不透明身份时，才提交一次`official`扫描。请求前先持久保存attempt；超时、坏响应、进程中断或失败都不删除attempt、不重提。返回其他模式/日期/范围的批次明确报告冲突；已提交批次转为failed/cancelled/interrupted也报告，不重扫。提交前及attempt落盘后重新检查截止，防止写入跨截止才发请求。

截止后沿用原collector登记missing，不读取旧批次补填。归档跨截止保留late；源码/日历漂移阻断并退出watch，原文件不变。记录的head、收据分类、逾期数和安全故障变更用于通知；时间戳及运行中等待本身不触发通知。即使source_drift未改变，新增逾期仍能检测。非信号窗口最多每小时核验一次，窗口内按分钟核验；采集阶段完成后退出，不延长计划。电脑和程序停止时不能保证按时留档，恢复后如实登记缺口。

runner当前只负责扫描、捕获、截止与控制记录，不自动下载供应商资料，也不自动执行evidence/evaluate。资料收集仍遵守每交易日最多一次扶摇增量、公开来源每日12次逻辑预算、失败记账、已有任务跟进和不购买订阅的边界。只有新收据、资料版本或新成熟日期才追加研究报告；正式净执行四项材料仍需严格验证。

### 独立健康记录与离线检查（2026-10-15）

新版runner在控制目录的计划子目录写入独立`health.json`，绑定计划、数据库、控制目录及受检runtime摘要。记录上限16KiB，采用原子替换和目录同步；持有覆盖整个会话的操作系统锁。健康状态不进入研究收据链，也不作为采集或正式执行成功的证据。旧封存目录保持原样；要使用新健康能力，须另行准备受检副本及匹配的未来计划。

健康记录区分`checking/waiting/stopped/complete/fault`，包含最后完成巡检时间、下一检查时间及安全故障码。检查状态的心跳判定期限300秒，正常等待最多3600秒，超时宽限120秒；这些期限不会强制中止本轮执行。锁文件可读取但未被进程持有时，未完成状态立即报告`runner_not_active`；锁文件缺失或不可读取时返回`unknown`及退出码`2`。时钟倒退、过期记录及读取期间的替换都有明确状态。重试开始时保留尚未解决的故障，仅在本轮成功完成后清除；普通等待不触发业务通知。

从当前工程执行以下只读检查，所有路径及摘要须来自已经确认的计划和运行清单：

```bash
PROSPECTIVE_PROJECT="$(pwd)"
.venv/bin/python tools/inspect_strategy_prospective_runtime.py \
  --directory '<受检runtime绝对路径>' \
  --expected-runtime-digest '<已确认的runtime摘要>' \
  --root "$PROSPECTIVE_PROJECT/data/research/strategy_prospective" \
  --plan-id '<已登记计划ID>' --expected-plan-digest '<已确认的计划摘要>' \
  --database "$PROSPECTIVE_PROJECT/data/ashare_radar.sqlite3" \
  --control-root "$PROSPECTIVE_PROJECT/data/research/strategy_prospective_control"
```

输出JSON，退出码`0`表示健康（包括已完成），`3`表示故障或身份无效，`2`表示无法判定或读取不可用。工具不调用HTTP、不触发扫描或采集、不创建控制记录；它会执行已受摘要校验的目标Python探针核对解释器身份，依赖包另按安装元数据、锁文件及内容摘要校验。外部指定的runtime摘要在任何目标解释器运行前核对；运行清单有16MiB读取上限且拒绝重复JSON键。计划检查核对机械身份、源码和日历与该副本的绑定，不用当前工作区的业务合同重判旧封存计划，也不重新评估研究证据。

该工具是单次诊断入口；进程启动、重启和告警调度仍由外部托管负责。缺少健康记录的旧runner不会被推断为健康。

**当前定时任务。** rev2已阻断且没有合格篮子，Codex现有同一自动化改为工作日上海时间19:45一次只读巡检。检查已准备runtime及旧计划身份，运行单次程序循环建立/核对本地观察记录；预期source_drift和退出码3不作为每天新故障重复通知。只在摘要/档案校验故障、新增逾期、就绪条件实质变化或阶段结束时通知。不会每天三次重复尝试旧计划的capture，也不会为它扫描或下载。

未来试验尚未登记，未启动watch。待固定实现的生产输入就绪、未来范围确定后，才在信号日前从受检副本初始化独立计划并绑定新摘要、开启上述watch；原剩余6个锚点/5个已覆盖退出日只是续接草案，不能宣称已启动或与rev2合并。Codex届时负责检查程序健康及证据需求，按分钟的截止归档由程序承担。正式来源、独立时间认证、连续账户及完整前瞻统计仍属缺口，策略采用资格保持关闭。

截至2026-09-19，真实扶摇增量同步已成功，规范未复权档案覆盖至2026-09-18、共5565只股票，仍是研究资料。默认官方执行来源注册表尚不存在，许可和交易状态等正式证据仍缺。增量成功只解决历史价格/事件资料的新鲜度，不完成正式净执行准入。

此前15个最早历史批次中，2个封印失败、13个因存在`missing`输入状态未准入；后者不是未保存股票。最新检查批次`run_id=166`共有5558条：5399成功、144跳过、15缺失，其中9条疑似停牌、6条为单一价格形态。这些只是缺失原因线索，不能据此将其改成已知停牌或`skipped`。新归档会完整保留这些状态，生产评分保持原规则。

当前已实现计划、按时输入归档、研究资料抽取和独立批次结果追加。本地时钟、收据链及摘要仍只提供`local-only-unverified`记录，尚无独立时间戳认证；连续资金/持仓账户验证和完整前瞻统计评估尚未实现。`adoptable_template_id`保持`null`、`promotion_eligible=false`。后续必须先补齐正式执行材料并积累真正成熟的计划样本，再讨论策略采用。

## 2026-09-20公开事实扩展

`evidence`新增`public_execution_facts`，复用同一收据前缀、冻结股票/日期清单和报告截止时间。公开归档先完整重放验证，再只保留所需股票，避免多月全市场批次常驻内存。晚于报告截止时间取得的资料保持待定；北交所目前不支持，缺行和空公告都不能当作正常交易证明。联网采集另由`collect_public_execution_facts.py`完成，详见[免费来源与实测](PUBLIC_EXECUTION_SOURCES.md)。

本次在起始日前确认旧`strategy-templates-20260921-phase1`收据为0后，另建rev2以绑定公开事实扩展后的源码。旧计划及摘要`18c779b0c0024c745d6e5cc0357e33c7a41e8e2084f8e5fdf818f19f066ecb52`保留，不改写、不拼接收据；同一自动化已切到rev2。日期轴、持有期、模板、资金与20:00截止时间保持相同。每日第一次检查可先完成信号采集，免费资料在17:45以后读取，避免早于BaoStock日线更新；晚间检查仍只处理缺口和结果。
