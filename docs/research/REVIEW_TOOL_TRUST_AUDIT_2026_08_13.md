# 复盘工具可信度审计与整改记录（2026-08-13）

> 审计对象：当前共享工作树中的复盘计划、到期评估、固定条件历史扫描、模拟交易、调度、用户数据迁移、备份恢复、API 与浏览器合同。
>
> 结论边界：复盘与模拟均为本地 Research Shadow，`production_effect=none`。本文不把历史命中、收益、MFE/MAE 或模拟成交解释为未来概率、因果绩效、真实成交或投资建议。

## 1. 结论

本轮终审未保留需要停用复盘工具的 P0。审计发现的主要风险集中在审计链可变、时间/交易会话证据不完整、并发版本漂移、停牌执行语义、损坏记录进入汇总、导入备份关系完整性，以及浏览器/API 对坏数据的拒绝能力。这些问题已经按“缺证据即不可用、旧证据不改写、并发写入要比较版本”的原则收口。

整改后的可信边界是：

1. 计划修订和评估尝试是追加式审计账本，不再用覆盖/级联删除表达历史。
2. 当前视图只是经过账本摘要验证的投影；摘要、身份、会话或合同不一致即拒绝或保守降级。
3. `as_of`、15:15 日线发布边界、可信交易日历、PIT `qfq`、停牌/开盘成交/公司行动元数据共同决定可用性。
4. 停牌可以占据一个固定交易会话，但不能触发价格屏障、模拟成交、虚假盯市或清除延迟退出。
5. 模拟策略冻结当前计划 revision 与 digest，并在写事务中再次比较；模拟仍不连接券商。
6. 复盘/模拟 API 全路径 `no-store`，未知 5xx 不披露内部细节；浏览器在渲染前做第二层严格合同校验。
7. 用户数据迁移保留不可变账本语义；备份不仅检查 SQLite 页面完整性，也检查外键关系。

## 2. 发现与处置

| 优先级 | 原问题 | 风险 | 当前处置 |
| --- | --- | --- | --- |
| P1 | 计划只有可变 current row | 修改后无法证明旧版本内容；A→B→A 会丢失中间语义 | `advice_review_plan_revision(plan_id, revision)` 保存 canonical JSON 与 SHA-256；每次实质修改追加一行，当前投影读时反向核对 |
| P1 | 同一 revision/`as_of` 的评估可能被覆盖，排序字段可被篡改 | 后到计算会重写当时结论，或篡改 attempt/审计时间改变 current | 相同语义 input/result 对幂等复用；变化即追加 `attempt`；evidence v2 把 attempt 与服务器评估时间纳入 input digest；当前值按 `as_of`、attempt、可信评估时间和 ID 排序，v1 只保守读取 |
| P1 | 删除计划会抹掉历史 | 用户操作破坏审计链，导入/备份也无法恢复因果关系 | 删除改为 expected-revision CAS tombstone；active/due/summary 隐藏，advice、revision、result 全部保留 |
| P1 | 评估时间可越界或混用写入时间 | 未来 `as_of`、客户端伪造 `evaluated_at` 或晚写早观察可能成为“最新” | 公开输入只接收 `as_of` 与 `expected_revision`；未来时间在行情/存储 I/O 前拒绝；`evaluated_at` 由服务器生成；最新观察以 `as_of` 优先 |
| P1 | 仅有 K 线日期/价格不足以证明固定会话 | 缺交易日、降级源、混合复权/合同或冲突重复可能造成移位与未来函数 | 逐固定交易会话要求 unique、连续、非 fallback、PIT `qfq`、受支持数据/执行/公司行动合同；完整源窗口进入 digest |
| P1 | 停牌的携带 OHLC 可能被当作触发/成交 | 产生不存在的止盈止损、成交或账户净值变化 | 评估屏障跳过非 trading session；模拟禁止停牌/不可执行 fill，open position 不用停牌 close 盯市，pending exit 保留并写 blocked event；benchmark 只使用 trading session |
| P1 | 损坏或 legacy 结果可影响详情/汇总 | 改一列可能改变收益、命中率与平均值 | 读时重算 input/result digest，并核对 plan revision digest 与 snapshot provenance；不可信行保持物理存在，但投影为 `insufficient_data`、指标 null，不进入有利/不利统计 |
| P1 | paper freeze 有读后写窗口 | 计划在查询与策略插入之间更新，模拟绑定旧参数 | 服务先校验以给出清晰反馈，repository 再于 `BEGIN IMMEDIATE` 内读取 plan+ledger 并比较 revision/digest，插入值只取事务内行 |
| P1 | Paper 运行只有输入 fingerprint，派生输出或孤儿边可逃逸 | SQL 改动成交、净值、事件或汇总，或插入 FK 合法但不属于该 run result 的 trade，可能仍被 dashboard/export 接受 | output digest v3 密封完整持久化运行投影；dashboard/export 读时重算；canonical 身份排除 surrogate ID/审计时间但绑定 plan revision/digest/symbol，拒绝非同-run strategy-result/symbol 子记录并重算四类声明计数 |
| P1 | 前端只检查局部 shape，面板失败互相污染 | 错股票/错版本/孤儿成交/坏计数可被渲染；一个接口失败导致整个复盘首页空白 | 独立 Review/Paper 合同校验 identity/digest/time/status/window/metric、child strategy/symbol/run ownership 与 count conservation；A-B-A owner/sequence 保护；summary/due/list 使用 `Promise.allSettled` 独立降级 |
| P1 | 导入既可能摘要失配，也可能“改历史后重签” | ID collision 后 canonical payload/digest 失配；partial merge 改 allocation/account 后重算摘要会洗白 immutable run | 完整语义等价图 remap 重写 advice ID 并重算/传播 digest；纯 surrogate ID remap 摘要稳定；已有 Paper 历史后的 account update 或已入 run strategy 语义 update 直接整单拒绝，不允许重签 |
| P1 | 历史 Paper performance 使用 current account | 旧 run 的本金/收益分母可随当前配置漂移，且 output digest 不变 | 运行冻结 `configuration.initial_cash`；dashboard/export 始终用 selected run 本金重建 performance；一旦存在 strategy 或 run，普通 API 与 import 都不能改本金 |
| P1 | 备份 `integrity_check=ok` 仍可能有断裂外键 | 页面结构正常但 review/paper 关系已损坏，恢复后才暴露 | 创建、verify、restore 后校验都要求 `PRAGMA foreign_key_check` 无行 |
| P1 | 敏感复盘错误可缓存或泄露内部细节 | 代理/浏览器缓存研究结果；数据库、验证或依赖错误暴露实现 | `/api/reviews...`、advice history/timeline 与 `/api/paper-trading...` 全路径 `no-store`；未知/依赖/内部/响应验证 5xx 统一为通用不可用信息 |

## 3. 当前计算与汇总口径

### 3.1 计划与证据

- 冻结字段包括 advice ID、股票、market time、snapshot price、`qfq` anchor、data/contract version、假设、触发/失效条件与执行 basis、target/stop、horizon 和证据引用。
- 价格必须满足 `target > snapshot > stop`；周期为 1–60 个固定交易会话。
- 系统结构化证据由后端拥有，客户端只能增加人工文字引用；证据日期不能晚于 snapshot 可见的已完成日线。
- 当前投影的 `plan_payload_digest` 必须等于 canonical revision payload 的 SHA-256。

### 3.2 评估窗口

- snapshot 当日不进入 forward window；`as_of` 之后的行不影响结果，之后出现的冲突也不能推翻已隔离的历史观察。
- 15:15 前当日日线尚未成熟；today 由服务器当前上海时间决定，历史日期才发送上海本地日末 cutoff。
- 每个预期交易会话必须有可验证 session evidence。停牌行可以满足日期覆盖，但不能触发 high/low barrier。
- 首次 target/stop 事件终止观察；同日双触发为 `target_stop_ambiguous`，不猜测先后。
- 没有屏障事件时，只有完整 horizon 且 terminal session 为 trading 才给 `horizon_gain/loss/flat`。
- `evaluated` 必须同时有 return、MFE、MAE 和完整来源会话；pending/insufficient 不允许携带这些指标或命中状态。

### 3.3 最新值与汇总

- Detail 只把当前 revision 的最新 verified attempt 显示为 current；历史接口保留所有 revision/attempt。
- 全局汇总只包含未 archive 的计划，每个计划最多贡献一个当前 verified result。
- favorable 是 `target_hit + horizon_gain`，unfavorable 是 `stop_hit + horizon_loss`；favorable rate 的分母只包含两者。ambiguous、flat、pending 和 insufficient 不进入该分母。
- return/MFE/MAE 平均值只对 verified evaluated 的非 null finite 指标计算；缺失不补零。
- 这些统计是本地描述性复盘，不做样本独立性、选择偏差、交易成本或因果有效性的额外声称。

## 4. 模拟交易边界

- 输入冻结一个精确 plan revision/digest，激活后才允许寻找首个可买 daily open；等待期间先触发 target/stop 会取消迟到入场。
- 股票 T+1：买入日信号只锁定，下一可卖会话开盘退出；停牌、零量、涨停锁买、跌停锁卖都不生成假成交。
- 规则按交易日选择主板、科创板、创业板、北交所 lot/price-limit profile；ST/listing 元数据不完整时保留 degraded，不伪装确定。
- commission、minimum commission、stamp duty、transfer fee、buy/sell slippage 分项建模，且与真实券商/冲击成本不等价。
- strategy、market evidence、rule、cost、benchmark、configuration 都进入 fingerprint；run/results/trades/equity/events 追加保存并可比较、JSON/公式安全 CSV 导出。output digest v3 另行密封完整持久化投影并在 dashboard/export 前重算；只改 surrogate ID 不改变摘要，完整语义等价图的 identity remap 才能在目标库重算。partial merge 不得修改既有 run 引用的 strategy 后重签。
- 历史 performance 的本金和收益分母来自该 run 冻结的 `configuration.initial_cash`，不是当前账户投影；存在任一 strategy 或 run 后账户本金锁定。
- daily bar 不知道盘中 high/low 先后、盘口深度、排队位置、真实 spread/impact 或可成交量；没有 broker adapter，也不允许把模拟结果当作下单记录。

## 5. 保留限制与下一步（P2）

1. **摘要承诺不等于独立重放包**：review 保存完整来源窗口的 digest/count，paper 保存 market hash、来源摘要和派生交易/净值/事件，但都没有把逐行行情输入作为 content-addressed artifact 随记录保留。缓存日后变化或被清理时，可以发现给定 payload 与摘要不一致，却未必能仅靠导出重建当时输入。若需要长期可复现审计，应增加有大小上限、可移植、可备份且由现有 digest 寻址的原始证据 artifact。
2. **Paper 配置意图不是追加账本**：尚未进入任何 run 的策略行允许删除，删除后同一 plan revision 可以再次冻结；不可删除边界从首个 immutable run 开始。若需要审计“创建后又撤销”的研究意图，应另加 append-only strategy lifecycle event，而不是禁止正常清理。
3. **全局计划列表仍是浏览器全量分页**：dashboard 并行读取 summary、due 和每页 100 条的 active-plan 列表，列表分支最多 100 页；第 100 页仍满时该分支会拒绝显示，虽然 summary/due 继续可用。若活跃计划接近 10,000，应增加服务端筛选/游标或专用投影，避免一次打开产生大量请求。
4. **摘要不是统计检验**：当前 summary 没有独立样本、基准、成本、置信区间或选择偏差校正；继续保持描述性标签，若要研究有效性应另建预注册离线评估。
5. **日 K 路径不可识别**：双触发只能 ambiguous；不要增加靠收盘方向猜先后的规则。
6. **交易日历有覆盖边界**：缺可信 session 时应保持 unavailable；不要恢复 Monday–Friday fallback。
7. **停牌估值仍是模型选择**：当前 open position 沿用最后可交易价而非停牌 carry row；如以后接入官方估值/复牌规则，必须新增版本化合同和 replay。
8. **无公开 unarchive**：tombstone 保留审计，但用户不能在 UI 恢复。若增加恢复，必须使用 expected revision、单独审计事件，并明确 paper references 的处理。
9. **Paper 不代表执行质量**：真实成交需要新的授权、券商、订单状态、幂等指令、风控与审计子系统，不能复用当前模拟接口绕过。

## 6. ChatGPT/Codex 可运行 Goal

```text
目标：对 AShareRadar 复盘工具完成一次端到端可信度审计与整改。覆盖 advice snapshot→plan→revision→evaluation attempt→summary/due scheduler、固定条件历史扫描、paper simulation、SQLite schema/migration、user-data portability、runtime backup、API、frontend 与 export。优先识别会导致未来函数、覆盖审计历史、错股票/错版本、停牌假触发/假成交、损坏证据进入统计、并发漂移、导入关系断裂、缓存敏感结果或泄露 5xx 细节的问题；P0/P1 必须直接修复，P2 记录边界。

不可妥协约束：
- 生产 SQLite 只读并记录前后 hash/size/mtime；所有写测试使用临时数据库。
- plan revision 与 evaluation attempt 追加保存；update/evaluate/archive/paper freeze 使用 expected revision CAS。
- canonical finite JSON + SHA-256 绑定 plan/input/result，并把完整 source-window digest/count 密封进 input；读时重算可重建的 plan/input/result，legacy/tamper fail closed；若没有原始行 artifact，不声称仅凭导出可重算 source-window digest。
- public as_of 不得未来；evaluated_at 由服务器生成；15:15 与可信 exchange sessions 为唯一完成日线口径。
- missing/conflicting/cross-contract/non-PIT evidence 不移位不补零；suspended session 不触发 barrier/fill/mark-to-carried-close。
- Review/Paper 明示 Research Shadow/production_effect=none/no broker；全路径 no-store，未知 5xx generic。
- portability 保持不可变账本、关系与 digest；仅完整语义等价图 remap 可重写 canonical embedded identity 并按目标语义重算 paper output digest，partial merge 不得改写已入 run strategy/account 后重签；backup 必须同时过 integrity_check 与 foreign_key_check。
- frontend 在 state/DOM 前验证 exact identities、digests、times、counts 与 cross-field invariants；不同面板独立降级且拒绝 stale A-B-A response。

完成定义：
1. P0/P1 finding 有可复现测试，修复后全绿；P2 与模型限制进入文档/UI。
2. Ruff、mypy、JS syntax、API/FUNCTION inventories、docs/config/architecture guards、focused Playwright 与 full pytest line+branch coverage ≥90% 全通过。
3. README、REQUIREMENTS、DESIGN、OPERATIONS、MAINTENANCE、TEST_PLAN、API_REFERENCE、FUNCTION_INVENTORY 与实现一致。
4. git diff --check 通过；本 Goal 不对生产数据库执行写操作，并记录前后只读指纹。若并发运行环境改变生产库字节，则必须明确披露漂移、复核完整性与复盘/Paper 作用域行数，不能伪报“完全不变”；输出 finding→fix→test→remaining boundary 的验收摘要。
```

## 7. 验收证据

最终命令、通过数量、覆盖率、浏览器矩阵、inventory 与生产数据库只读指纹记录在 [Test Plan and Test Report](../TEST_PLAN.md) 的最新 2026-08-13 条目。生产库只通过只读/immutable 连接核验；任务期间检测到字节级指纹漂移，但文件大小与复盘/Paper 作用域行数未变，最终 `quick_check=ok` 且 `foreign_key_check` 为空，因此文档披露漂移而不把它归因于临时测试。该记录是当前工作树的工程验收，不是市场有效性证明。
