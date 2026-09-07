# Choice 独立研究数据

Choice 用于显式授权下的独立采集与离线核验，不替换生产 Tencent 数据，也不向真实运行库写入行情。研究包的 `official`、`formal_equivalent_pit` 与 `filter_qualified` 保持 false。原始回执、执行参考和日历校验有助于发现缺口，但不能自行证明交易所来源、历史可获得性或可成交性。总入口见 [研究指南](RESEARCH.md)。

## 采集与恢复

入口：`.venv/bin/python tools/backfill_choice_research.py --help`。账号所有者先完成官方 SDK 安装、许可和机器激活；凭据、SDK 与许可原始数据不提交仓库。接口说明见 [EmQuantAPI Python 文档](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Python.html)。

| 操作 | 行为 |
| --- | --- |
| `plan` | 输出日期、股票和请求预算的计划预览，不登录 SDK；实际采集将计划写入研究包 |
| `quota` | 登录并查询账户额度；属于网络／账户读取，不是离线核验 |
| `collect` | 按计划串行采集日线、元数据及股息相关资料 |
| `supplement-plan` / `supplement` | 计划／采集执行参考和近期股票池补充 |
| `universe-plan` / `universe` | 计划／补齐逐日股票池，支持复用已核验包 |
| `resume` | 按原计划、进度和回执继续，保留失败，不暗中扩大范围 |
| `status` / `verify` | 不登录 SDK，读取进度或重放原始记录核验 |

```bash
.venv/bin/python tools/backfill_choice_research.py plan \
  --start-date '<YYYY-MM-DD>' --end-date '<YYYY-MM-DD>' \
  --symbol-limit 60 --output-dir '<新Choice研究目录>'
.venv/bin/python tools/backfill_choice_research.py status --output-dir '<Choice研究目录>'
.venv/bin/python tools/backfill_choice_research.py verify --output-dir '<Choice研究目录>'
```

联网采集前核对计划与 `--max-requests`、`--max-csd-cells`、`--max-css-cells`、股息／股票池调用数预算；提高本地上限不能增加供应商授权。进程采用单次登录、串行请求、`ForceLogin=0` 和硬超时；超时属于失败／可恢复状态，不能标为采集完成。

所有数据包共享 `data/research/choice_ingestion_control` 的互斥锁与持久额度预留，不能通过删除控制目录“重置”预算。周额度必须获得当前完整周的证据；仅看到上周记录时为 `paused_rollover_unconfirmed`，安全可用量为 0，含义是未知，不是证明额度耗尽。不得用行情试探请求判断是否已重置。

研究包保留 `plan.json`、`selection.json`、`raw/`、`account/`、独立 `choice_research.sqlite3` 与进度摘要。规范化视图包括 `daily_bars`、`universe_membership`、`metadata_snapshots`、`dividend_reports` 和 `dividend_events`；`request_key` 绑定原始请求／回执。核验已有记录成功不等于覆盖全部声明范围：应同时查看 `existing_records_verified` 与 `complete_for_declared_research_scope`，退出 2 或 `incomplete_resumable` 不表示完整数据。

## 离线审计与静态历史

显式传入实际目录，不依赖审计工具中保留的默认目录名：

```bash
.venv/bin/python tools/audit_choice_research.py \
  --history-dir '<历史包>' --supplement-dir '<执行补充包>' \
  --universe-dir '<逐日股票池包>' --control-dir '<共享额度控制目录>' --compact

ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0 TRADE_CALENDAR_AUTO_FETCH=0 \
.venv/bin/python tools/build_choice_experimental_history.py build \
  --source-dir '<核验通过的Choice包>' --output-dir '<新静态历史目录>'

ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0 TRADE_CALENDAR_AUTO_FETCH=0 \
.venv/bin/python tools/build_choice_experimental_history.py verify \
  --manifest '<Choice历史manifest.json>' --database '<Choice静态历史.sqlite3>'
```

审计默认输出 stdout；显式 `--output-dir` 才发布不可变审计报告。`--audited-at` 是离线比较时刻，不能成为实时额度授权。转换按原回执、参数和固定日历逐行核验，生成独立 `choice-experimental-history-v1`，不能改名为 Tencent manifest。

研究 qfq 由未复权价格乘以当日 `TAFACTOR / 每股最后有效日TAFACTOR` 构造，成交量保留股数。停牌、非交易、受限或因子无效记录不被插值为正常交易；覆盖率分母保留全部声明股票。11 个特征对统一价格缩放不变，不能据此推导成本、整手和容量口径也相同。

## 方向概率与跨来源比较

```bash
ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0 TRADE_CALENDAR_AUTO_FETCH=0 \
.venv/bin/python tools/validate_experimental_direction.py \
  --choice-history-manifest '<Choice历史manifest.json>' \
  --choice-history-database '<Choice静态历史.sqlite3>' --output-dir '<新验证目录>'

ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0 TRADE_CALENDAR_AUTO_FETCH=0 \
.venv/bin/python tools/build_experimental_probability.py \
  --prediction-kind close_d1 --choice-history-manifest '<Choice历史manifest.json>' \
  --database '<Choice静态历史.sqlite3>' --output-dir '<独立候选模型目录>'

ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0 TRADE_CALENDAR_AUTO_FETCH=0 \
.venv/bin/python tools/compare_choice_tencent_history.py \
  --choice-dir '<Choice原始研究包>' --tencent-database '<Tencent静态历史.sqlite3>' \
  --tencent-manifest '<Tencent历史manifest.json>' --output-dir '<新比较目录>'
```

方向验证固定 D+1／D+2／D+5 的共同末尾 60 个成熟信号日期作为测试段，前置 40 日校准及至少 120 日训练，并在两条边界按目标 purge。缺失标签不能压缩日期，异常特征保留拒绝；报告比较日期等权指标、基线及区块区间，保留三个目标，不自动选择最优。`completed / evaluated` 只表示计算完成，证据属于历史隔离重放；它评估独立重拟合模型，不能给在线模型补上测试成绩。

候选构建支持 `close_d1`、`close_d2`、`close_d5`，必须独立输出，不能覆盖在线模型目录。目标与实验 UI 的限制见 [个人实验概率](PERSONAL_EXPERIMENTAL_PROBABILITY.md)。

跨来源比较复验原始 Choice 包、Tencent 静态历史与日历，检查同日价格／成交量、11 个特征和固定标签，以及复权锚点与追加未来数据不变性。有限交集的一致不证明两个完整数据集可替换，也不允许共用校准器。部分执行参考、零成交与明确停牌需要分别处理；历史股票池偏差、公司行动、许可和外部 PIT 证据仍是独立限制。
