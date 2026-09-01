# Choice 历史研究数据采集

Choice 接入目前用于独立、可续传的历史研究数据集，不参与生产行情路由，不修改
`data/ashare_radar.sqlite3`、冻结扫描、已有 Tencent 回放或正式概率授权。
实时快照、分钟线权限与历史接口分开；SDK 激活不代表它们已经开通。

## 准备与边界

- 在项目 Python 环境中安装官方 `EmQuantAPI`，并由账户持有人完成官方激活。
- SDK 是可选依赖；导入项目、离线 `plan/status/verify` 和测试不加载 SDK，也不登录。
- 不把 SDK、`userInfo`、账号信息或原始行情提交到 MIT 代码仓库。代码许可不代表行情再分发授权。
- 采集器使用独立子进程、一个登录会话、串行请求；`ForceLogin=0`，不踢掉别处登录。
- 原生调用超过硬超时会终止该工作进程；失败请求不记为完成，也不自动无限重试。
- `data/research/choice_ingestion_control/` 的共享文件锁阻止本项目多个采集器同时登录。
  额度预留跨退出/重启持久保存，不要删除该目录来绕过额度控制。

## 命令

查询最新账户额度与跨运行预留台账（不取行情、不购买套餐）：

```sh
.venv/bin/python tools/backfill_choice_research.py quota
```

账户统计可能延迟，显示余额不能直接理解为还可以再下载同样数量的数据。
本命令使用相同会话锁，并在控制目录的 `account/` 中保留查询结果。
查询窗口固定为截至上海当日的最近 30 个自然日，避免当天尚无统计行时把“无数据”误报为
额度耗尽。输出只保留额度白名单字段，并同时列出：供应商显示余额、当前套餐周期的本地
累计预留、配置的本地上限、保守安全可用量，以及 CSD+CSS 成对扩样的算术容量示例。
白名单包含官方 `PERIOD` 和 `THRESHOLD`，但不保存套餐名、使用率或供应商返回的其他账户字段。
项目只接受完整的周周期（`W`、周一至周日），并要求阈值严格等于已用量加剩余量；不一致、
重叠的当前周期或畸形日期均在归档和行情请求前关闭。

30 天窗口可能只返回已经结束的旧周，也可能同时返回旧周和当前周。唯一当前周记录会按
日期选择，不依赖供应商返回顺序；只有旧周时状态为 `paused_rollover_unconfirmed`，旧余额和
阈值仅用于诊断，安全量及配对容量保持 0。这不是“额度耗尽”，而是当前周统计尚未确认。
采集器不会用一条 CSD/CSS 行情请求试探重置，也不会在此前先下载日历或股票池；下一步仅是
稍后重查 `datastatistics` 或向 Choice 确认。完全命中本地回执的离线重放不受此限制。

容量示例默认按 60 只股票、502 个交易日、14 个 CSD 日线字段和 3 个 CSS 参考字段计算；
完整研究样本预算还默认纳入 26 个元数据快照和 11 个分红报告期，可用
`--planning-symbols`、`--planning-sessions`、`--planning-snapshot-dates`、
`--planning-report-dates` 改变假设。默认 30 股完整样本估算为 CSD 210,840 / CSS 53,280，
60 股为 CSD 421,680 / CSS 106,560，另须按实际受限股票日补状态字段。示例只是需要先锁定
不重叠样本并复核缺口的算术候选，不是调用授权、PIT 证据或生产晋级依据；命令不会自动取数、
释放历史预留或提高保护上限。CFC 只报告供应商余额，明确标记为尚未接入采集器。
行动建议分别报告 CSD+CSS 配对历史与可选 CTR 事件查询：CTR 周记录缺失或套餐暂停不会误阻断
仍有完整配对预算的历史样本，反之亦然。即使 CSD/CSS 都有非零余额，也必须足以覆盖最小的
SH/SZ/BJ 各一只、共 3 只股票的完整均衡样本，才会显示为可复核的算术候选。

以下在项目根目录执行。先查看计划，不取数、不写库：

```sh
.venv/bin/python tools/backfill_choice_research.py plan \
  --output-dir data/research/choice_history_example \
  --start-date 2024-07-31 --end-date 2026-08-25 --symbol-limit 60
```

将 `plan` 换成 `collect` 才会登录取数。日期必须明确，结束日必须早于今日。
样本数为 3 的倍数，按 SH/SZ/BJ 均衡抽取，默认 60、最大 225。
可重复使用 `--prefer-symbol` 纳入诊断样本。最多三个 `--event-symbol` 同时纳入
抽样及分红事件表查询；不指定则不消耗该专题表额度。

续传已经建立的计划，不必重复全部参数：

```sh
.venv/bin/python tools/backfill_choice_research.py resume \
  --output-dir data/research/choice_history_20260825
```

只读查看和完整回放校验，不登录、不扣额度：

```sh
.venv/bin/python tools/backfill_choice_research.py status \
  --output-dir data/research/choice_history_20260825
.venv/bin/python tools/backfill_choice_research.py verify \
  --output-dir data/research/choice_history_20260825
```

同一目录的 `plan.json` 不允许改变。修改日期或抽样方案必须使用新的目录。
`resume` 会验证原始响应、请求参数和派生记录，已完成请求不重复下载。
原始文件写好而 SQLite 尚未提交时中断，也可直接从该文件恢复。
缺失或损坏的原始文件会阻断续传，不会悄悄重新取一份数据覆盖历史。

## 优先补缺，而不是重复下载

对完整的首批历史数据包，可以先生成补缺计划：

```sh
.venv/bin/python tools/backfill_choice_research.py supplement-plan \
  --source-dir data/research/choice_history_20260825 \
  --output-dir data/research/choice_supplement_20260825
```

将 `supplement-plan` 换成 `supplement` 才执行请求。原数据包以只读方式打开，
补缺计划绑定原计划和全部请求回执摘要，原始响应必须通过重放校验。
不修改、不覆盖原包；源数据后来变化会阻断续传。补缺顺序为：

1. 复用月末价格参考记录，只为其余交易日查询 `PRECLOSEEXCH/LIMITUPPRICE/LIMITDOWNPRICE`。
2. 只对日线已标出的停牌或盘中受限日期查询状态、原因及起止日期。
3. 补齐最近 30 个交易日的全市场股票池，复用原来已经存在的日期。
4. 在未查询过分红事件表的股票中，按复权因子变化次数优先，并优先覆盖不同市场，
   最多选 3 只。因子变化仅用于排优先级，不冒充公司行动事件本身。

`--recent-universe-sessions` 可设 0–34，`--event-limit` 可设 0–3；原计划确定后不能修改。
补缺与首批采集共享周预留台账。默认一次 100 个新请求，大范围补缺可显式使用
`--max-requests 1000`；分红事件仍须满足既有台账与最新账户余额，
必要时显式提高本地累计上限，例如 `--max-dividend-event-calls 6`，不会绕过服务器检查。

补缺包也使用 `resume/status/verify --output-dir ...`；续传不需重新传源目录。
`verify` 同时验证补缺包与绑定的原包，报告合并覆盖。新视图为
`execution_references`、`suspension_details`，股票池和分红记录使用既有视图。
原包和补缺包必须一起保留，不能单凭补缺库的行数认定总覆盖。

例如，在项目根目录用只读 SQLite 将两包连接查询，不必再复制日线：

```sh
sqlite3 -readonly data/research/choice_supplement_20260825/choice_research.sqlite3
```

```sql
ATTACH DATABASE 'file:data/research/choice_history_20260825/choice_research.sqlite3?mode=ro' AS base;
WITH refs AS (
  SELECT symbol, session_date, previous_close_reference, limit_up_price, limit_down_price
  FROM execution_references
  UNION ALL
  SELECT symbol, snapshot_date, previous_close_reference, limit_up_price, limit_down_price
  FROM base.metadata_snapshots
)
SELECT d.symbol, d.session_date, d.close, d.volume_shares, d.trade_status,
       r.previous_close_reference, r.limit_up_price, r.limit_down_price
FROM base.daily_bars AS d
LEFT JOIN refs AS r USING (symbol, session_date)
WHERE d.symbol = '600519.SH'
ORDER BY d.session_date;
```

逐日参考记录齐全不代表每个价格都有效：上市前、退市后、无涨跌停限制或供应商缺失，
可能返回空值或零；这些情况不会合成为价格。停牌截止日期也是事后资料，不作为
停牌开始时已经知道的特征。两类补缺仍不能证明开盘订单可成交。

## 本阶段采集范围

### 补齐整个区间的逐日股票池

优先补缺包只补最近 30 天。需要继续补旧日期时，用 `universe-plan` 做离线规划，
可通过多个 `--reuse-dir` 复用已验证的其他数据包，避免重复抓取同一天：

```sh
.venv/bin/python tools/backfill_choice_research.py universe-plan \
  --source-dir data/research/choice_history_20260825 \
  --reuse-dir data/research/choice_supplement_20260825 \
  --output-dir data/research/choice_daily_universe_20260825
```

将 `universe-plan` 换为 `universe` 才会取数；为本次明确的缺口增加本地预算时，
可以显式传 `--max-universe-calls 512 --max-requests 500`。
512 是本地一周累计请求保护上限，包含已有预留，不是供应商授权额度。
默认仍为 60，硬上限 600；不允许通过删除台账来增加额度。
账户统计未单独列出该板块接口流量，不代表无限制或已确认免费。
任何权限、流量、频次或超时错误均停止，并保留原始响应和进度。

续传使用 `resume --output-dir ... --max-universe-calls 512 --max-requests 500`。
请求按最近缺失日期优先，已存在的日期只读复用。所有输入包均绑定回执摘要，
复用包的同日成分必须一致，不能悄悄选用其中一份。
返回日期必须等于请求日期，且必须是 `001071` 全 A 股板块。
SH/SZ/BJ 均必须存在；相邻交易日任一市场数量比低于 95% 会暂停审查，
不会把按月间隔的正常增长误判为截断。

即便区间每一天都已查询，结果仍是 Choice 的事后历史成分观测，不是独立交易所核对、
原始历史版本或正式概率授权。特别是 BJ 历史成分仍须独立验证；
下载时的股票名称不能作为当时已知名称。完成报告分别列出请求覆盖、逐市数量范围及缺失日期。

### 首批数据包

1. 从 Choice 读取区间交易日历，并保留原始响应。
2. 区间首尾及每个月最后一个交易日的**全市场**股票池快照；名称标为下载时名称，
   不冒充历史名称。不是逐交易日全市场成分。
3. 对上述股票池的并集做固定哈希均衡抽样，不仅从当前仍上市股票抽样。
   明确列出优先诊断样本；这不是完整历史股票池的随机统计样本。
4. 样本各月末的历史名称、上市退市资料、交易状态、停牌原因、交易所昨收参考价及涨跌停价。
5. 样本季度报告期的已通过正式分红快照，包括公告、登记、除权、派息、送转上市日期。
6. 样本全部请求交易日的 14 个日线字段：不复权 OHLC、量额、昨收、换手、交易状态、
   涨跌停标记、ST/*ST、后复权因子。
7. 显式指定的至多三只股票的分红事件表样本。配股、增发、合并等其他公司行动尚不完整。

当日有报价但成交量/额为空的停牌日保持为空；未知状态、上市前、部分停牌、缺失日分别保留。
单一价涨跌停单独标记，`execution_eligible` 恒为 `null`：没有以日线推断开盘订单一定成交。
`STATUS` 为当前存续状态，上市/退市日期也不能直接当作历史已知特征。

## 额度和中断

默认本地周上限为 CSD 450000 个估算数据单元、CSS 200000 个估算数据单元、CTR 3 次请求预留；
日历最多 6 次、股票池最多 60 次。后两项是本地保护上限，不是供应商赠送无限额度。
实际 CSD/CSS/CTR 还必须有有效套餐及服务器剩余流量；本地预留再从报告剩余量中扣除，
有意保守地避免统计延迟导致超用。CSD/CSS 单元数按证券数 × 字段数 × 日期数估算，
**不是已确认的供应商计费公式**；CTR 的按次预留也不是按记录计量保证。

额度在发请求之前提交，失败/超时不退回预留，避免不确定调用被反复计费。
每 10 个新请求及首尾检查是否应刷新账户统计，相邻额度查询至少间隔 30 秒；
复用短期快照时仍逐请求持久预留，失败时不会使用旧额度继续取数。
数据请求默认最少间隔 1 秒，可用 `--request-interval` 调高；
遇到 `10000016`（请求频次过高）停止并保留进度，不密集自动重试。
单次运行默认至多 100 个数据请求。

可使用 `--max-requests` 限制单次进度，或调低 `--max-csd-cells`、`--max-css-cells`、
`--max-dividend-event-calls`。本地上限增大不能绕过服务器余额检查。
默认不会自动续订、购买套餐或增加付费权限，也不会创建定时任务。

退出码 2 / `incomplete_resumable` 表示计划未完成，原因写入进度文件。
停止原因可能是额度、权限、请求数上限或严格数据校验；不能将其算作成功补齐。
遇到权限不足先检查账户，不要删除缓存或不断重试。已停止的任务需要再次执行 `resume`。

## 数据文件与查询

- `plan.json`：固定范围、字段与限制。
- `selection.json`：最终样本、日历和快照日期。
- `raw/`：原始 SDK 业务响应、真实采集时间、参数及内容摘要；不含登录令牌。
- `account/`：额度查询快照，可能有供应商统计延迟。
- `choice_research.sqlite3`：可续传派生库，不是生产库。
- `summary-*.json`：完成声明范围并通过原始响应重放后的摘要。
- `progress-*.json`：中断位置和已完成数量。

查询视图：`daily_bars`、`universe_membership`、`metadata_snapshots`、`dividend_reports`、
`dividend_events`。保留 `request_key`，可关联 `requests` 找到原始响应摘要及采集时间。
额外日线字段可从 `payload_json` 取出，例如：

```sql
SELECT symbol, session_date, amount_yuan,
       json_extract(payload_json, '$.TURN') AS turnover_pct,
       quality
FROM daily_bars
WHERE symbol = '600519.SH'
ORDER BY session_date;
```

`verify` 逐请求重放原始响应并核对派生记录，而非只运行 SQLite quick_check。
`existing_records_verified` 只说明现有记录正确；只有收集完成时的
`complete_for_declared_research_scope` 表示本计划完成，仍不等于全市场/所有字段完整。

所有数据保持 `official=false`、`formal_equivalent_pit=false`、`filter_qualified=false`。
要进入正式概率流程，还需要完整逐日股票池和执行状态、公司行动与规则的覆盖证明、
真实有效的来源授权登记、当时可见特征、同规则成熟样本及样本外模型检验。
本工具不降低这些门槛，也不自动将新数据适配成旧 Tencent 研究或正式执行凭据。

## 复用已有日线做独立方向研究

现有完整首批包可离线转换为专用的 Choice 实验历史，不需要再次登录或下载。
转换器先重放原始响应、核对固定请求参数及完整交易日历，再发布新的静态 SQLite 和
`choice-experimental-history-v1` 清单；它不是 Tencent 的历史清单。
每次加载还会重放原包并逐行比较派生库，不能只重签一个修改过的清单绕过校验。

```sh
ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0 TRADE_CALENDAR_AUTO_FETCH=0 \
  .venv/bin/python tools/build_choice_experimental_history.py build \
  --source-dir data/research/choice_history_20260825 \
  --output-dir data/research/choice_experimental_history_20260826
```

只对确有成交、OHLC 和后复权因子均有效的记录重建价格：
`研究前复权价 = 未复权价 × TAFACTOR(当日) / TAFACTOR(该股最后有效成交日)`。
成交量保持原始股数。非交易、停牌、盘中受限和无效因子分别排除，不插值、不填零、
不补成连续行情；完全没有有效行情的样本股票也保留在覆盖率分母中。
所用 11 个方向特征对同股价格的共同正比例缩放不变，但这不是原始历史版本或 PIT 证明，
也不能推广到最低佣金、整手买卖和容量等成本/执行模型。
Choice 的乘法复权与腾讯数据不保证一致，禁止直接混池或复用对方校准器。

命令输出真实 manifest 和 database 路径。使用这两个路径，可分别做只读重放校验、
三周期隔离回放，以及独立候选模型训练：

```sh
.venv/bin/python tools/build_choice_experimental_history.py verify \
  --manifest '<CHOICE_MANIFEST>' --database '<CHOICE_STATIC_DATABASE>'
.venv/bin/python tools/validate_experimental_direction.py \
  --choice-history-manifest '<CHOICE_MANIFEST>' \
  --choice-history-database '<CHOICE_STATIC_DATABASE>' \
  --output-dir data/research/choice_direction_validation_20260826
.venv/bin/python tools/build_experimental_probability.py --prediction-kind close_d1 \
  --choice-history-manifest '<CHOICE_MANIFEST>' --database '<CHOICE_STATIC_DATABASE>' \
  --output-dir data/research/choice_experimental_candidates_20260826
```

候选训练目标另可指定 `close_d2`、`close_d5`。方向验证报告与候选模型都必须使用原始及派生历史档案
之外的独立输出目录，不能写入这些档案目录或自动写入线上
`research/personal_experimental_probability` 模型目录；原 H5 和腾讯模型保持不变。
离线入口拒绝开启交易日历自动联网的环境，执行这些命令时应确保上述两个自动抓取开关为 0。

验证固定使用三个周期共同的最后 60 个已成熟信号日作为测试区间，各周期各自保留 40 个
校准日、至少 120 个训练日，并在训练/校准/测试之间隔离相应目标周期。
目标缺失不改变日期边界；超过训练范围 8 个标准差的记录与线上相同地拒绝预测。
报告同时提供全部周期和 SH/SZ/BJ 分组的覆盖率、逐日等权评分、校准期比例基线、
0.5 基线及按目标周期分块的置信区间，不根据最终测试结果挑周期或自动发布模型。

报告 `completed/evaluated` 只表示计算完成，不是模型有效性通过。
数据可能已被旧研究看过，结果恒标注 `historical_isolated_replay_not_prospective`；
不能把重新切分同一历史叫作新前瞻证据，也不能把本次重新拟合的验证结果移植到
使用全部历史训练的候选模型或既有线上模型。正式授权门槛不变。

## 只读审计与跨来源核验

三个数据包可一起做原始回执重放、异常解释和额度对账；不调用 SDK、不释放预留，
也不把账户显示余额直接变成可花费额度。下面的输出目录只存新的内容寻址报告，
必须位于输入数据包和额度控制目录之外：

```sh
.venv/bin/python tools/audit_choice_research.py \
  --history-dir data/research/choice_history_20260825 \
  --supplement-dir data/research/choice_supplement_20260825 \
  --universe-dir data/research/choice_daily_universe_20260825 \
  --control-dir data/research/choice_ingestion_control \
  --output-dir data/research/choice_research_audit_20260826 --compact
```

报告会区分必须补查与不推荐的重复抽查。上市窗口内参考价为零、已有停牌日期记录等
解释，不会升级成已证明的无涨跌停规则或分钟级可成交证据。没有逐请求结算标识时，
账户流量变化与本地估算不一致也不能成为自动释放预留的理由。

另一条离线命令独立重建 Choice 价格，深校验腾讯历史清单，在相同交易日网格上比较
复权价、成交量、全部 11 项特征和 D+1/D+2/D+5 标签；缺失日不压缩成下一个可用日。
同时验证改变复权锚点及追加未来行情是否影响已有信号特征：

```sh
ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0 TRADE_CALENDAR_AUTO_FETCH=0 \
  .venv/bin/python tools/compare_choice_tencent_history.py \
  --choice-dir data/research/choice_history_20260825 \
  --tencent-database '<TENCENT_STATIC_DATABASE>' \
  --tencent-manifest '<TENCENT_MANIFEST>' \
  --output-dir data/research/choice_tencent_comparison_20260826
```

比较器只重放 Choice 日线请求，完整三包审计由前一条命令负责。两个命令均只读校验输入，
比较器还核对读取前后哈希不变；拒绝符号链接、活跃 SQLite 旁文件和受保护路径，不替换既有模型。
标签一致不代表特征或校准器可互换；少量交集也不能证明全市场数据源等价。

## 验证代码

```sh
.venv/bin/python -m pytest -q tests/test_choice_research.py
.venv/bin/python -m pytest -q tests/test_choice_experimental_history.py tests/test_experimental_direction_validation.py tests/test_choice_research_audit.py tests/test_choice_history_comparison.py
.venv/bin/python -m ruff check app/services/choice_*.py tools/backfill_choice_research.py tests/test_choice_research.py
```

官方接口及复权/流量字段说明：<https://quantapi.eastmoney.com/Upload/EMQuantAPI_Python.html>。
