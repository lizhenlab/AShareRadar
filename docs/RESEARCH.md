# 离线研究与验证

研究工具用于核验数据、冻结实验和比较声明假设下的结果。生产 v5 分数是排序分，不是收益预测或上涨概率；研究报告、约束预算与本地摘要都不自动改变生产评分、概率准入或账户。Choice 和个人实验概率另见 [Choice 研究](CHOICE_RESEARCH.md)、[个人实验概率](PERSONAL_EXPERIMENTAL_PROBABILITY.md)。

## 入口与输入

从仓库根目录运行 `.venv/bin/python tools/<工具>.py --help`；含子命令的工具还支持 `<子命令> --help`。以下是当前研究入口，完整参数以帮助和严格输入校验为准。路径占位符需替换为已核验的真实输入；输出使用独立新路径，不覆盖原始材料。

| 工具 | 当前用途与主要参数 |
| --- | --- |
| `audit_market_scan_research.py` | `--database --as-of-date`：只读盘点冻结批次、输入身份与缺口，不读取收益 |
| `audit_market_scan_availability.py` | `--database --plan --output`：按完整日历、截止时刻和唯一批次规则审计可用性；缺失日期保留 |
| `backfill_market_scan_probability_history.py` | `--source-database --target-database --output-dir`：从有备份 manifest 的运行库副本选择股票，**联网请求 Tencent qfq**，建立独立研究库 |
| `backfill_market_scan_probability_replay.py` | `--database --start-date --end-date --output-dir`：从静态研究库生成不可变历史重放产物 |
| `maintain_market_scan_probability.py` | `--database --source-dir --outcome-dir --as-of-date`：只读价格库，按固定交易日成熟 source 的 outcome |
| `build_market_scan_probability_historical_context.py` | `--artifact --output-dir`：深验历史重放，生成同目录的紧凑研究上下文 |
| `ingest_market_scan_official_execution.py` | `contract / status / verify / ingest`：输出格式合同、核验或原子安装执行会话；实际核验依赖独立 registry 摘要及许可原始材料 |
| `evaluate_market_scan.py` | `--database`：评估已保存的全市场排序，支持固定批次和输出报告 |
| `evaluate_market_scan_shadow.py` | `--database --variant`：当前支持的 Shadow 公式离线对照；紧凑摘要供证据读取，不能自行晋级 |
| `evaluate_market_scan_probability.py` | `--database --output-dir`：构建概率研究产物；`--output-dir` 必填 |
| `evaluate_market_scan_future_range.py` | `--database --output-dir`：构建固定 D+1/D+2/D+3 区间研究产物；`--output-dir` 必填 |
| `evaluate_individual_probability.py` | `--history-manifest --output-directory`，可重复 `--official-source`：构建个股 D+2/D+3/D+4 compact Shadow 评估；受管发布还要求匹配的 `--database` |
| `run_market_scan_research.py` | `register / run / verify / replay / export-registration`：冻结试验、共享账户重放和报告恢复 |
| `audit_market_scan_frontier.py` | `execution / coherence / holdings`：执行时点归因、同条件概率自洽、真实持仓市值暴露审计 |
| `manage_market_scan_prospective.py` | `create / record / verify / seal / bind-result`：独立未来采集计划与逐日收据链 |
| `manage_market_scan_feedback.py` | `create / append / verify`：逐条事件窗口的延迟反馈账本 |
| `manage_market_scan_cohort_feedback.py` | `create / append / verify`：冻结日历、完整日期群组与日期等权反馈 |
| `plan_market_scan_allocation.py` | `--input --policy --output` 加账户、候选、市场和政策四个独立摘要：已有持仓约束下的确定性预算规划 |
| `run_market_scan_sensitivity.py` | `--plan --plan-digest --bundle --output`：完整成本 × 资金 × 参与率网格重放 |
| `benchmark_market_scan.py` | `--database`：日 K 暖／冷缓存读取性能基准，不调用 provider；会读取指定数据库并使用临时空库作冷缓存比较，不衡量选股收益 |
| `backfill_choice_research.py` | Choice 采集、计划、恢复与原始收据核验；网络与预算边界见 [Choice 文档](CHOICE_RESEARCH.md) |
| `audit_choice_research.py` | 只读核对 Choice 数据、额度预留与原始回执 |
| `build_choice_experimental_history.py` | `build / verify`：从已验证 Choice 包生成或核验独立静态历史 |
| `compare_choice_tencent_history.py` | 核对 Choice 与 Tencent 的同日数据、特征和标签差异 |
| `build_experimental_probability.py` | 构建明确目标的个人实验模型，Choice 候选必须隔离输出 |
| `validate_experimental_direction.py` | 在共同日期切分上评估 Choice D+1/D+2/D+5，保留全部目标和基线 |

运维工具 `runtime_data.py`、`provider_canary.py`、`runtime_contract.py`、`api_inventory.py`、`architecture_inventory.py`、`generate_sbom.py` 不属于金融研究流程。不要把所有工具无参数批量执行，或把 `--help` 当作任意脚本都支持的安全模式；例如 `runtime_contract.py` 是直接运行检查的脚本。

严格离线核验时设置 `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0` 和 `TRADE_CALENDAR_AUTO_FETCH=0`，使用已具备覆盖的本地日历。禁用日历获取不等于禁用采集 CLI 的网络请求。`quota` 会访问 Choice 账户；Tencent 回补和 Choice 采集需要另行确认账户、许可、范围和预算。受管产物发布可能创建研究文件并核对运行库，不能将“只读输入”理解成“没有输出写入”。

## 冻结试验与可恢复报告

`run_market_scan_research.py` 的输入是 `market-scan-research-input-bundle-v1`：原始冻结批次、完整交易日历、训练／校准／测试分区、探索截止日以及执行证据。官方模式必须提供独立 `--official-registry-digest`；显式 synthetic 行只用于合成研究，不能提升为官方证据。

```bash
.venv/bin/python tools/run_market_scan_research.py register \
  --registry-root '<新登记根目录>' --registration-id '<试验ID>' \
  --bundle '<冻结输入.json>' --output '<新登记回执.json>'
.venv/bin/python tools/run_market_scan_research.py run \
  --registry-root '<登记根目录>' --registration-id '<试验ID>' \
  --bundle '<同一冻结输入.json>' --output '<新报告.json>'
.venv/bin/python tools/run_market_scan_research.py verify \
  --registry-root '<登记根目录>' --registration-id '<试验ID>' \
  --bundle '<同一冻结输入.json>' --report '<已保存报告.json>'
```

官方输入在上述各步都需加独立 registry 摘要。`run --resume` 恢复未封存登记；活跃执行由独立租约排他保护。成功登记但回执输出失败可用 `export-registration` 重取；已封存报告可用 `replay` 写到新路径，`verify` 则逐内容核对已有报告。不要删除日志或锁文件来伪造恢复。

当前已准备好的 bundle 只能登记为 `retrospective`；即使 CLI 的兼容选项列出 `prospective`，冻结历史数据也不能获得事前身份。登记绑定源码、输入、候选、执行政策与统计口径，源码变化可能使原运行的数值重放拒绝，不能重新盖章覆盖旧报告。

优化主检验是三个预声明候选相对**同账户政策下 production_v5 Top100** 的配对增量，保留固定日期轴；v5 自身不进行优化检验。候选相对全市场的指标另作诊断，不能将整手交易造成的现金基准差当成评分增益。合同冻结单侧检验、6 日区块、至少 40 日样本及完整三候选 Benjamini–Yekutieli FDR 校正。校正不证明底层 bootstrap p 值有效，也不是 FWER 或真实收益保证；缺失项不能缩小检验族。

共享账户 v3 按整手、费用、独立资金 sleeve、前日成交额参与率、暂停／锁板及延期退出重放。未知估值保留未知。退出日必须符合入场后 H 个交易日；旧报告留存窗口外的目标仍需当前可信日历补验，不能宣称旧格式已经冻结完整未来日历。持仓延续、分批退出和模型驱动再平衡仍需要独立合同，未接入当前账户。

## 前瞻收据与延迟反馈

前瞻工具在第一计划日之前冻结可信日历原文、每日逻辑采集槽位、截止时间、唯一选择规则、missing 政策以及候选／统计／代码身份。槽位不是尚未生成的数据库 run_id；收据绑定实际 run_id、原字节摘要、声明可用时间和 plan 摘要。每日只登记一次，迟到和缺失不能补成 PIT；封存要求每个计划日都有收据，结果只能在封存之后绑定。历史验证使用已冻结日历，不因本地日历扩展而重解释计划。

这些是 `local-only / unverified` 证据：本地时钟、自报 available_at、收据链和文件摘要均不证明外部事前可获得性，也没有核实通用 envelope 的市场内容或批次选择合格性。输出保持不可生产晋级。

新日期群组账本 `market-scan-date-cohort-feedback-ledger-v1` 与逐条账本 `market-scan-delayed-feedback-ledger-v1` 是两种不同合同，互不自动迁移。新账本一次冻结同日全部预测成员，使用固定目标时点和标签可用时点；新预测不能读取其决策时点之后才到账的标签，旧预测不会被回写。

新账本对完整日期先取日均残差，再跨日期等权更新；标签到账顺序和同日股票数量不能提高日期权重。窗口按全部计划日期推进。已成熟的未声明／不完整日期，即使滚出更新窗，也会阻止更新并退回固定基线；空日期占日期位置，但不制造零标签。`ready` 只表示工程门槛满足，不能解读为校准或收益提升。输入必须是真正的概率，不能直接填入排序分。

```bash
.venv/bin/python tools/manage_market_scan_cohort_feedback.py create \
  --config '<完整日期配置.json>' --output '<新账本-0.json>'
.venv/bin/python tools/manage_market_scan_cohort_feedback.py append \
  --ledger '<账本-0.json>' --ledger-digest '<独立固定摘要>' \
  --events '<预测群组或成熟标签数组.json>' --output '<新账本-1.json>'
.venv/bin/python tools/manage_market_scan_cohort_feedback.py verify \
  --ledger '<账本-1.json>' --ledger-digest '<独立固定摘要>'
```

## 预算规划与情景诊断

预算规划先跨 sleeve 合并老仓的同股／行业占用，检查决策时点可获得的估值和分类，预留费用上界，用共同的成本后 NAV 下界约束所有新增单票与行业暴露。未知老仓证据或已有超限会阻止新增订单；候选槽按现金等分，无法购买的槽保留现金，不递补或重新分配。最终复核整手、现金、费用与暴露。`planned` 是提案，`orders_submitted=false`；没有成交、自动卖出或接入旧 v3 重放的承诺。

```bash
.venv/bin/python tools/plan_market_scan_allocation.py \
  --input '<账户候选市场输入.json>' --policy '<冻结政策.json>' \
  --account-digest '<账户对象摘要>' --candidates-digest '<候选对象摘要>' \
  --market-digest '<市场对象摘要>' --policy-digest '<政策对象摘要>' \
  --output '<新预算计划.json>'
.venv/bin/python tools/run_market_scan_sensitivity.py \
  --plan '<完整情景计划.json>' --plan-digest '<独立固定计划摘要>' \
  --bundle '<冻结输入.json>' --output '<新网格报告.json>'
```

规划的四个 pin 各自绑定相应 canonical JSON 对象，不是整个输入文件的同一个摘要。格式／pin 无效返回 1；预算被阻止保留原因并返回 2；`planned` 和 `no_orders` 返回 0，均不表示成交。

敏感性工具在读取 forward 输入前检查完整网格计划；每一格重新计算账户，候选只能与同资金／费用／参与率／日期的 v5 比较。高成本可能改变手数或令账户留在现金，不能用同一成交路径减一个费用常数代替重放。费用加回指标仅解释已发生路径。

所有失败格、未知日期、现金和事件原因保留；未知不是 0，也不能静默删日。顶层 `complete` 要求所有格运行且同情景比较完整，部分完成返回 2，格式／pin 失败返回 1。网格是描述性的执行假设敏感性，未识别市场拥挤容量，不自动择优、不提供跨情景显著性。网格应在观察结果前冻结；后续正式择优需要另行预声明主情景、完整检验族与推断方法，本地摘要不能认证预登记。

## 保留的证据格式与方法依据

仍有消费者的旧格式读取用于验证原始哈希、显示缺口或拒绝旧证据越权，不表示继续生成所有历史版本。例子包括概率 source 对原评分规范身份的校验、旧逐条反馈的原义重放，以及旧 assessment 的严格读取／不合格状态。策略证据面板仍读取 `app/resources/strategy_shadow_baseline.json` 的静态 v4 研究基线作对照，其原摘要与兼容门槛保留，当前 v5 执行不会自动适配为合格证据。真实研究材料保存在受管数据目录或用户选定的独立目录；测试样本仅在 `tests/fixtures`，不应成为运行时回退数据源。

摘要证明选定字节未变，不证明来源可信；必须另行固定期望摘要和来源证据。缺失会话、暂停与公司行动不能通过补齐价格、前移标签或删除失败样本变为可交易观测。历史股票池、探索过程、重叠标签、选择偏差和 A 股交易制度仍限制统计结论。

保留少量支持这些边界的研究依据，而非把论文结果当成本项目收益：

- [Forecast Collapse in Time-Series Foundation Models](https://arxiv.org/abs/2608.14106)：预测误差、幅度和横截面排序相关性应分开检查；排序相关性不赋予分数收益单位。
- [An Entropic Factor Model for Robust Portfolio Replication](https://arxiv.org/abs/2609.03552)：论文表格并未显示所有组合／费用设置都优于 OLS；复杂模型或低换手不能代替逐情景对照。
- [Robustness or Crowding: Experimental Design for Trading Strategy Capacity](https://arxiv.org/abs/2608.08405)：冲击成本设定与实际拥挤容量不同；当前网格只回答给定执行假设的敏感性。

这些预印本没有在本项目复现训练和实证收益。日历完整性、固定比较族、账户约束和恢复性测试验证的是工程合同；数据许可、外部 PIT 认证、真实成交和稳定收益需要独立证据。
