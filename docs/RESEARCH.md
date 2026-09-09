# 离线研究与验证

## 全市场选股前沿审查：2026-09-10

本轮核对 2026 年正式论文及截至 9 月的预印本，复现概率排序晋级统计的时间依赖与命名问题，并优化保持结果不变的整手搜索和分页组装。新旧授权边界、原文读取范围、未采用方法和后续经济验证见[前沿研究与具体方案](MARKET_SCAN_FRONTIER_RESEARCH.md)；当前验收见[测试计划](TEST_PLAN.md)。

研究工具用于核验数据、冻结实验和比较声明假设下的结果。生产 v5 分数是排序分，不是收益预测或上涨概率；研究报告、约束预算与本地摘要都不自动改变生产评分、概率准入或账户。Choice 和个人实验概率另见 [Choice 研究](CHOICE_RESEARCH.md)、[个人实验概率](PERSONAL_EXPERIMENTAL_PROBABILITY.md)。

带日期的章节记录对应主题的研究与实施取舍，现行工具用法从“入口与输入”开始。文中的 `/tmp/...` 路径仅定位当时本机会话的证据，不随仓库发布、不保证长期存在，也不是运行工具或复验仓库测试的前提；可复用的依据是原始来源链接、现行代码和测试。

## 评分下行风险与量价校准：2026-09-10研究依据

[研究与实施方案](SCORING_RISK_RESEARCH.md)核对 Markowitz、Lo、Ang/Chen/Xing 三篇主流论文及 CFA 官方方法原文，区分方差、目标下行偏差与市场条件 beta。主/影子评分将负收益子集标准差改为零目标、全 20 日收益分母的经验下行偏差；量价当前与历史校准统一既有规则和两位量比输入，缺量窗口不伪造中性样本。旧主维度按原定义审计，新规范、概率特征与影子候选分别隔离；阈值和权重没有按收益调优。该次修复不证明预测或投资收益改善。

## 笔记写入可靠性：2026-09-09研究依据

[专题报告](NOTE_WRITE_RESEARCH.md)比较 PaPoC 2026、POPL/UIST/ProvenanceWeek 2025 四篇原始研究，区分证明前提、形成性案例与工程适用范围。方案聚焦完整状态条件写入、冲突与待提交草稿、显式字段的本地创建；不由合并原型推出自动合并必要性，不把状态摘要当成历史账本。

## 问答证据与笔记时间：2026-09-09研究依据

[专题报告](QA_EVIDENCE_RESEARCH.md)比较八篇原始论文，包含 ACL/ICLR 2026、NeurIPS 2025 与 2026-09-02 预印本，列明版本、方法、限制及具体取舍。工程方案针对空事件提示被当作证据、问答取消后控件无法恢复、笔记审计时间不能形成图表日期三项已复现问题；不将记忆基准成绩或自审结果外推为投资收益，也不据此引入自动笔记改写。

## 通知历史连续性：2026-09-09研究判断

查阅日期2026-09-09。以下两篇均阅读官方正式全文的方法、实验与限制，未运行作者系统；版本、阅读定位和原文摘要曾记录于 `/tmp/ashare-notification-continuity-review-20260909/paper-review.md`，原文文件身份记录于同目录 `paper-sources.json`。该主题的现行实现合同见[设计文档](DESIGN.md)，回归定义见[通知API](../tests/test_alert_notification_feed.py)、[备份恢复](../tests/test_runtime_restore_alert_stream.py)与[浏览器消费](../tests/test_notification_stream_continuity.py)。研究阅读本身不计为验收通过。

| 原始论文与实际版本 | 方法及限制 | 本地工程判断 |
| --- | --- | --- |
| Li等，[Distributed Speculative Execution，OSDI 2026，7月，pp.2027–2045](https://www.usenix.org/conference/osdi26/presentation/li-tianyu)；[正式全文](https://www.usenix.org/system/files/osdi26-li-tianyu.pdf)，§2.2–5.2、§6 | 恢复点绑定对象、故障代次和局部进度；隔离恢复前后消息，外部输出等待依赖可恢复。仅提供正确性草图，假设可靠通道、持久存储及有界重启；旧实例不能并写持久状态仍由使用方保证 | 历史身份与历史内游标分别校验，迟到响应在消费处重新核对资格；不采用推测执行、依赖图或分布式运行时，也不据此声称OS通知恰好交付一次 |
| Kozar等，[Meerkat，PVLDB 19(3):306–319，2025](https://doi.org/10.14778/3778092.3778094)；[正式全文](https://www.vldb.org/pvldb/vol19/p306-kozar.pdf)，§3–6 | 按来源身份和递增序号去重，依赖存活副本对齐恢复状态；exactly-once*以前端数据来源不失败为条件。实验为边缘设备上的NEXMark，吞吐差异部分来自执行引擎；不能证明任意源回退安全 | 旧备份不满足来源持续递增假设，不能拿旧最大ID跳过新历史事件；保留同历史内大于游标的分页，不照搬连续序号或副本集群 |

Meerkat虽列入VLDB2026会议程序，正式刊年是2025，不能改写为2026论文。DSE真实容器故障恢复约10秒，立即模拟回滚是另一实验边界；这些数字不用于预测本地恢复或通知性能。

本地判断是将持久 `stream_id` 与事件ID组成通知身份。普通重启保持历史；成功restore或replace导入形成新历史，并在与数据同一次生效的边界记录基线。跨历史客户端从该基线继续，不能以首次轮询时的最新ID覆盖基线而跳过恢复后已产生的新事件。读取须在同一快照取得身份、基线和分页；UUID仅可判相等，跨标签页协调时还需核对请求、响应与当前共享状态的归属，防止迟到旧响应反向覆盖新历史。

原备份必须先完整验证并保持字节不变，暂存库仅允许明确登记的协调元数据变更；不能因更新通知身份而豁免业务表或历史产物的完整性检查。上述取舍来自本项目公开恢复链反例与工程推导，论文不替代行为回归。此协议不补回备份外已丢失事件，不建立无限事件保留，也不解决OS通知创建与本地确认之间所有崩溃不确定性。现行实现及边界见[设计文档](DESIGN.md)；上述回归验证具体行为，不构成跨操作系统事务或真实断电保证。

## 恢复与维护：2026-09-09研究判断

查阅日期2026-09-09。以下两篇均已核验2026正式发表记录并阅读关键方法、实验和限制；原始PDF与阅读定位见 `/tmp/ashare-recovery-maintenance-review-20260909/paper-review.md`。不运行作者系统，不将论文直接作为本地缺陷的证明。

| 原始论文与实际版本 | 方法及限制 | 本地工程判断 |
| --- | --- | --- |
| Mishra等，[HarborMaster，PVLDB 19(9): 2126–2139，2026](https://doi.org/10.14778/3819518.3819539)；[官方全文](https://vldb.org/pvldb/vol19/p2126-mishra.pdf)，§2–4、§6 | 因果日志与异步审计检测TEE持久状态回退；外部观察者历史不会随节点回退。要求可信TEE/PKI、收敛因果一致和最终消息可达；检测实验为16 worker/30万key的错误读取注入，非SQLite恢复验证 | 用户主动恢复是合法操作，但浏览器游标可能属于另一条历史；先验证公开恢复链，不采用TEE、向量时钟或审计集群，也不承诺恢复补回备份之外的事件 |
| Huang等，[Fractal，NSDI 2026，5月](https://www.usenix.org/conference/nsdi26/presentation/huang)；[正式全文](https://www.usenix.org/system/files/nsdi26-huang.pdf)，§2、§4、§6–7、§9 | 跟踪子图依赖、持久输出及读者偏移，恢复必要上游；评估77个脚本和4/30节点集群。机制不涵盖客户端/协调器恢复及数据库写入等外部副作用 | 停止调度循环、实际任务结束与结果提交分别验证；普通任务不能等待无依赖的扫描预检。不引入分布式执行框架，不将下游流恢复外推为端到端exactly-once |

HarborMaster的卷期、页码和DOI由正式PDF核对，并收录于[2026-08-31至09-04的VLDB会议程序](https://vldb.org/2026/program.html)；PDF生成时间不作为发表日。另完整阅读了[Bian等，DisCoGC，FAST 2026，2月](https://www.usenix.org/conference/fast26/presentation/bian)的[正式原文](https://www.usenix.org/system/files/fast26-bian.pdf)§3–7：其已发起/已完成删除记录和mock discard可提醒检验维护阶段，但底层一致性继承ByteStore，SSD顺序写负载的收益不能外推到本地SQLite；该次不采用其discard/compaction架构。

该次四项修改由真实公开入口的隔离反例决定：借用连接缺少真实事务状态，使清理预览成功而执行失败；可选压缩早于外层提交，使逻辑删除成功却无法释放空页；调度重启把仍被当前进程拥有的手动任务误判为遗留取消；自动扫描预检阻塞无依赖的普通到期任务。修改限定在真实事务能力、提交后可选压缩、实际任务所有权和单个在途自动tick，保留完整性守卫、保留阈值、终态保护及既有停止边界。151ms调度探针和17.5MB合成库的压缩结果只证明具体执行顺序，不是provider性能或真实用户收益承诺。

**该次已确认、未实施的通知问题，是后续通知历史连续性修复的依据。** 临时库经正式备份/恢复后，原备份摘要未变、数据库完整性正常；恢复出的新事件id5/6真实存在，但旧浏览器游标100的两次HTTP请求均返回200空数组。将实际API响应原样重放到公开Node轮询模块，均保持游标100且零通知；这不是活浏览器联网实验。该次仅保存问题与方案，证据见 `/tmp/ashare-recovery-maintenance-review-20260909/notification-restore-audit/`。历史身份、替换导入、备份完整性和跨标签页实施边界见上文“通知历史连续性”，不以旧反例或恢复维护验收代替通知协议的修复验证。

## 编辑与恢复工作流：2026-09-09研究判断

查阅日期2026-09-09。以下两篇均由2026官方会议目录和原文核验；阅读版本、方法定位及候选取舍见 `/tmp/ashare-workflow-frontier-review-20260908/paper-review.md`。未运行作者系统或核验完整形式证明，也不重复上一轮资源/缓存论文。

| 原始论文与实际版本 | 方法及限制 | 本地工程判断 |
| --- | --- | --- |
| Li、Cai、Lou，[Pilot Execution，NSDI 2026，5月](https://www.usenix.org/conference/nsdi26/presentation/li-zhenyu)；[USENIX全文](https://www.usenix.org/system/files/nsdi26-li-zhenyu.pdf)，§2、§4.4、§6–7 | 用影子状态与隔离I/O观察恢复传播；五系统35候选仅20例成功复现，其中检出17例。不能保证非确定性交错复现，也不覆盖未触发动作等影响 | 恢复完成不等于用户状态已正确；用隔离夹具明确检查中间状态与最终效果，不把17/20外推成本项目检出率，不引入生产影子线程框架 |
| Stonebraker、Zhou、Kraft、Li，[Consistency and Correctness in Data-Oriented Workflow Systems，CIDR 2026](https://www.vldb.org/cidrdb/2026/consistency-and-correctness-in-data-oriented-workflow-systems.html)；[会议原文](https://www.vldb.org/cidrdb/papers/2026/p9-stonebraker.pdf)，§6–9 | 同数据库事务提交业务效果与步骤日志；外部调用仍依赖幂等或处理未知结果，部分一致性机制尚属草案。实验为PostgreSQL/Python电商负载，非本地SQLite验证 | 区分服务端提交与当前编辑状态，沿用短事务和已有修订检查；不以持久执行宣称端到端exactly-once，不让事务跨越用户输入或网络等待 |

CIDR会议日期为[2026-01-18至21日](https://www.cidrdb.org/cidr2026/)。官方PDF首页仍有2025及占位DOI/ISBN模板，本文不把这些占位值作为出版标识。DoeFL另有ICST 2026官方收录页，但该次未取得并核对其独立最终全文，不将摘要或旧博士论文章节作为该次方法依据。

该次已复现复盘编辑中的两种草稿丢失：保存A等待回执时切换编辑B，或继续输入A的新内容，都会被A的迟到成功回执重置。具体方案是校验提交身份与草稿状态：保留A的真实提交结果，同时保护后来编辑的内容；未发生新编辑时仍完成原保存流程。同对象的新草稿须与已确认服务器修订协调，失败或冲突不能清空输入。这是客户端归属问题，数据库事务和论文框架不能代替其行为回归。

后端另已复现两项矛盾：Discovery同来源重试返回“已存在”，却重置后来人工设置的观察/排除状态；策略归档检查与任务写入分离，使并发请求在归档后仍能创建或重新启用任务。采用原事务内精确检查来源身份后决定是否变更观察池，首次及新来源保持原入队行为；任务最终准入与写入在同一数据库事务内绑定归档状态及固定修订，停用不依赖来源可用性。幂等应覆盖业务效果，不能只统计来源行数；准入应约束实际提交，不能只检查之前读取的值。CIDR帮助界定这两个边界，Pilot提醒检验具体交错及最终状态；这些本地反例均不是论文直接证明，也不需要分布式工作流框架。

该次通知恢复候选仅有fake GET探针；后续“恢复与维护”审计补充了真实临时备份恢复及HTTP响应证据，通知历史连续性修复据此实施。通知连接恢复、业务状态可读取与事件逐条交付是不同合同，不能默认增加永久事件日志或声称每条通知必达。该次实施证据保存在 `/tmp/ashare-workflow-frontier-review-20260908/verification.json`；现行合同见[设计文档](DESIGN.md)和[测试计划](TEST_PLAN.md)。

## 平台状态与资源：2026-09-08研究判断

查阅日期2026-09-08。该次检索了OSDI 2026、FAST 2026正式目录与作者来源，选取一篇2026年和两篇2025年正式论文的原文，避免重复项目已有研究。实际阅读版本、方法定位及限制保存在 `/tmp/ashare-platform-frontier-review-20260908/paper-review.md`；没有运行论文工具或核验全部形式证明。

| 原始论文与阅读版本 | 可迁移的方法 | 独立判断与不采用的内容 |
| --- | --- | --- |
| Lin、Chen、Wu，[ValScope，OSDI 2026，7月](https://www.usenix.org/conference/osdi26/presentation/lin-li)；[USENIX正式全文](https://www.usenix.org/system/files/osdi26-lin-li.pdf)，§3–7 | 查询变换同时检查集合与字段数值关系，不能只看行数或集合相等 | 实验覆盖6个MySQL语系数据库，未测试SQLite或应用恢复。本地借鉴完整字段组合校验和变换关系，不引入SQL fuzzer，不把其结果当作本项目事务正确性证明 |
| Lyerly等，[Skybridge，OSDI 2025，7月](https://www.usenix.org/conference/osdi25/presentation/lyerly)；[USENIX正式全文](https://www.usenix.org/system/files/osdi25-lyerly.pdf)，§3–5 | 元数据缺口应标未知并回源，缓存存在不等于已证明满足读取合同 | 摘要最佳数字来自fail-closed读取，不能套用于普通读取；跨区域协议不适合本地应用。请求条数覆盖和已读确认单调性是本地工程推导，论文未直接证明它们 |
| Voigt、Schuster、Brachthäuser，[Dynamic Wind for Effect Handlers，OOPSLA2 2025，10月](https://doi.org/10.1145/3763155)；[作者提供的正式排版全文](https://pl.cs.uni-tuebingen.de/publications/voigt2025dynamic.pdf)，§3.4.4–5 | 明确资源在离开与恢复计算时的真实状态，语法上的finally不等于已经释放 | 定理针对Effekt模型，真实打开/关闭约定并非静态强制；性能基准没有调用终结子句。不引入effect handlers，不外推Python线程可终止、无泄漏或性能改善 |

该次五项修改均由本项目反例决定：告警类型与阈值要在一个写事务内合并校验；已读确认不能因旧请求迟到而增加未读；取消后同键请求应复用仍归属runtime的任务；新鲜缓存还需满足请求覆盖；事务结束与SQLite连接关闭分别验证。这些不是论文已经证实的项目缺陷，也不是单一“缓存一致性”问题。

已读计数是派生状态，现有串行写事务足以实现确认只能减少计数、新事件仍增加计数，不必新增水位表。短响应只能证明具体来源在具体缓存内容下曾处理某个请求范围，不能证明来源永远没有更多数据；记录应有界、失效可回源，并保留既有新鲜度与降级合同。同键任务加入沿用现有最终准入，不新增第二套任务管理器。

连接实验暂时禁用GC以观察显式生命周期：旧实现连续30次导出留下30个仍打开的连接，修复后为0；这不表示正常运行必定永久泄漏30个连接，也不能换算成加载速度倍数。[Python官方SQLite文档](https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager)明确事务上下文不会关闭连接。备份恢复该次未复现新问题，不因论文讨论恢复就强行改造。

该次验收证据保存在 `/tmp/ashare-platform-frontier-review-20260908/verification.json`；现行合同见[设计文档](DESIGN.md)和[测试计划](TEST_PLAN.md)。该次不改评分或交易算法，不将工程正确性修复表述为收益提升。

## 评分证据方向与输入一致性：2026-09-08研究依据

核对日期 2026-09-08。以下两篇已核验真实期刊发表身份，关键方法来自原作者公开全文；实际取得的是作者稿，未取得最终排版 PDF，不声称逐字核对发表终稿。版本、方法定位与访问限制见 `/tmp/ashare-score-evidence-review-20260908/paper-review.md`。

| 论文及实际阅读版本 | 方法与适用条件 | 当前采用与不采用的内容 |
| --- | --- | --- |
| [Freyberger、Neuhierl、Weber，Dissecting Characteristics Nonparametrically，RFS 2020](https://doi.org/10.1093/rfs/hhz123)；[2019-01 作者全文](https://faculty.chicagobooth.edu/-/media/faculty/michael-weber/nonparametrics.pdf) | 美国月度特征的截面秩、加性样条与变量选择，按时间评估；加性结构及已研究特征库限制解释 | 检查条件信息与明确方向，不能把观察标签、曲线置信带或本项目 Alpha 充分度当成上涨概率；不直接移植模型、变量或权重 |
| [Bryzgalova、Lerner、Lettau、Pelger，Missing Financial Data，RFS 2025](https://doi.org/10.1093/rfs/hhae036)，online 2024-07-02；[Pelger 本人上传的 2024-07-10 作者全文](https://www.researchgate.net/publication/360528187_Missing_Financial_Data) | 缺失有结构，完整样本筛选会改变样本；因子插补结合截面和时间信息，local B-XS 与使用未来的 global BF-XS 时点不同 | 缺失、估计、观测和时点资格须分别表达；插补不自动获得 observed/PIT 身份，也不能以减少选择偏差为由放宽必需价格准入 |

独立判断：评分方向、证据充分度和执行资格是三种不同含义。高质量总分不能覆盖已有硬性动作限制；没有明确方向的“接近/观察/等待”不能默认成为正证据；同一完成快照的昨收引用不能在趋势与一日收益维度中互相矛盾。这三组问题由公开代码调用链的合成反例证明，论文只帮助界定检查方法，不替代程序证据。

修复保留原趋势数学、质量扣分及明确正负贡献幅度。有效“性价比一般”可按原幅度提供弱支持，但必须有可用比值；等待确认或未知评级不能通过默认分加分。动作阻断复用于建议、买点、策略卡和做T助手；规则匹配仍是另一个证据合同，其匹配状态不构成执行授权。

后续若研究插补或特征消融，须预先固定时间切分、候选族、股票池与执行成本；分别报告完整、缺失、来源降级和不可评分样本，保留分母与剔除原因。不能用未来值补历史、只展示成功子样本，或要求“缺失任一低分因子后归一化总分必须下降”。该次不新增模型或依赖，不将正确性修复宣称为样本外收益改善。该次验收证据保存在 `/tmp/ashare-score-evidence-review-20260908/verification.json`。

## 评分尺度与有效观测：2026-09-08研究依据

核对日期为 2026-09-08。该次完整阅读两篇主流发表论文的关键方法，第三篇只核对出版社元数据及作者摘要。原始版本、定位和限制保存在 `/tmp/ashare-score-scale-review-20260908/paper-review.md`；不将未取得的全文细节作为依据。

| 来源与核验范围 | 方法和适用条件 | 本项目采用的判断 |
| --- | --- | --- |
| [Gu、Kelly、Xiu，Empirical Asset Pricing via Machine Learning，RFS 2020](https://doi.org/10.1093/rfs/hhaa009)；[作者提供的发表版](https://dachxiu.chicagobooth.edu/download/ML.pdf)，online 2020-02-26 | 美国月度收益，特征逐月按截面秩归一化；缺失用截面中位数；时间顺序训练、验证和测试，另做规模子样本检查 | 预处理是模型的一部分，应明确单位、舍入和缺失含义。不能据此把本项目缺数变成有效中性证据；固定财报滞后不等于真实发布时间，也不据论文换成神经网络 |
| [Hou、Xue、Zhang，Replicating Anomalies，RFS 2020](https://doi.org/10.1093/rfs/hhy131)；[作者提供的发表版](https://theinvestmentcapm.com/uploads/1/2/2/6/122679606/houxuezhang2020rfs.pdf)，online 2018-12-10 | 发表版研究 452 个异常，用 NYSE 断点及市值权重缓解微盘支配，并比较不同估计条件；处理退市收益和特征时点 | 股票池、权重、缺失规则与数据版本会改变结论；不照搬美国微盘阈值。其机械持股容量上界不能替代本项目的成交量参与率、成本与锁板模拟 |
| [Green、Hand、Zhang，The Characteristics that Provide Independent Information about Average U.S. Monthly Stock Returns，RFS 2017](https://doi.org/10.1093/rfs/hhx019)；[作者页面](https://sites.google.com/site/jeremiahrgreenacctg/home) | 该次只取得作者摘要与发表记录，全文访问失败；摘要研究多个特征同时进入模型后的独立预测信息 | 仅作为后续组件消融的动机；不声称完整复核其缺值、费用和统计实现，也不凭摘要裁定同源指标应删除 |

该次三个实现错误分别由程序反例证明。纯趋势中，所有参与价格同时乘以正数后，价格/均线及均线比例应保持；提前把货币均值取两位破坏了这个性质。该检查只约束纯趋势，不要求整手数量、绝对价格准入、手续费或公司行动也保持等价。

ATR 的本地实现是固定窗口真实区间的简单均值：有效的 0 必须保留在分母中，无效 OHLC 仍剔除。当前项目沿用该窗口约定及两位输出，没有改成 Wilder 递归平滑；[TA-Lib 原始 ATR 实现](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_ATR.c)可核对二者差别及初始均值包含全部观测的含义。这是技术实现参照，不是证明 ATR 能预测收益的论文证据。缺失高低价则不能构成有效区间，不能经派生盘口转成卖压扣分。

后续经济假设仍须单独登记：在同一 PIT 股票池、持有期、执行与成本条件下做组件消融及流动性分层；有真实当时市值证据才做规模分层。已有三候选研究注册不追溯更改。归一化、阈值和候选族须在测试期前确定，不用当前市值回填历史，也不根据一次回测或相关性表调权。

现行实现合同见[设计文档](DESIGN.md)，验收记录与边界见[测试计划](TEST_PLAN.md)。论文支持审查研究设计，三个 bug 及修复效果以合成反例和代码回归为准，不构成收益提升证据。

## 评分语义审查：2026-09-08研究依据


以下七篇原始论文查阅于 2026-09-08，包含经典研究与 2024–2026 年已发表论文。完整方法笔记与公开原文证据位于 `/tmp/ashare-score-review-20260908/paper-review.md`。成本、短期反转和回测过拟合的正文阅读使用作者稿，并核对了正式发表记录，未将作者稿日期冒充刊期。

当前 v5 主要是趋势排序，叠加数据质量扣分与有界连续趋势修正。全市场 leader profile 本身是纯趋势，并没有再次叠加个股 profile 的资金、换手等奖励。均线、斜率、区间位置和近期收益有共同价格来源，不能称为独立证据；但相关也不自动证明组合无效。生产序数分不含收益基准或成交成本，因此不能把“分数没减手续费”本身列作缺陷；成本与成交约束应在对应持有路径的研究评估中检验。

| 原始论文与发表状态 | 方法与适用条件 | 本项目判断与取舍 |
| --- | --- | --- |
| [Jegadeesh & Titman，Returns to Buying Winners and Selling Losers，JF 1993](https://doi.org/10.1111/j.1540-6261.1993.tb04702.x)；[原文](https://www.bauer.uh.edu/rsusmel/phd/jegadeesh-titman93.pdf) | 美国股票、季度尺度形成/持有期、赢家多头与输家空头，另检验一周间隔 | 不能给 A 股 D+1/D+3 或 5/20 日正权重直接背书；采用明确观察期、持有期和端点偏移的要求 |
| [Dai 等，Reversals and the Returns to Liquidity Provision，FAJ 2024](https://www.tandfonline.com/doi/full/10.1080/0015198X.2023.2292534)；[2023-01 作者稿](https://mysimon.rochester.edu/novy-marx/research/RRLP.pdf) | 行业相对短期收益、公告窗口处理与滞后流动性条件，区分反转速度和持续性 | 高波动、高换手和短期上涨并非同一种“强”；本项目缺少其完整资料，不直接调整涨幅方向或复制公告调整策略 |
| [Novy-Marx & Velikov，A Taxonomy of Anomalies and Their Trading Costs，RFS 2016](https://academic.oup.com/rfs/article-abstract/29/1/104/1844518?login=false)；[2015-08 作者稿](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf) | 价差成本估计及不同买入/持有阈值；小额基础成本与规模冲击有不同假设 | 采用具体成交路径和账户现金约束，不移植美国价差参数；开盘是否受限不能无条件代替收盘退出证据 |
| [Harvey、Liu、Zhu，… and the Cross-Section of Expected Returns，RFS 2016 已发表原文](https://people.duke.edu/~charvey/Research/Published_Papers/P118_and_the_cross.PDF) | 区分 FWER/FDR；隐藏试验推断依赖模型，相关试验需要适当多重调整 | 保留完整候选族和预先声明的指标；不把 t>3 当通用门槛，也不把 BY 调整当作修复无效单项 p 值或错误成交模型的方法 |
| [Bailey 等，The Probability of Backtest Overfitting，JCF 2017](https://www.risk.net/journal-of-computational-finance/2471206/the-probability-of-backtest-overfitting)；[2015-02 作者稿](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) | CSCV 使用同日期轴的完整试验收益矩阵，受自相关、试验数及结构变化限制 | 当前三个候选不宜据此新增高精度 PBO 数字；先保留完整试验记录和时间依赖，模拟错误需使旧结论失效 |
| [Müller & Schmickler，Interacting Anomalies，RAPS 2025](https://academic.oup.com/raps/article/15/2/162/7979163) | 对大量双排序交互做多重校正并检验样本外表现，历史优势与换手、流动性有关 | 因子冗余和交互价值应在同一冻结样本、账户和成本下做消融检验；不能因为相关而全部删除，也不能凭历史毛收益扩大指标集合 |
| [Akey、Robertson、Simutin，Noisy factors?，Review of Finance，2026-01-28](https://academic.oup.com/rof/advance-article/doi/10.1093/rof/rfag002/8443460) | 比较因子数据版本，并用固定代码区分底层数据修订与方法变化；估计和推断会受版本影响 | 新趋势与执行标签语义必须可区分，旧产物按旧合同验证或明确不可复用；不能只改公式却继续使用同一摘要和解释 |

前次评分审查的四项缺陷由确定性程序反例证明：合法 0 分被中性默认覆盖、风险扣分反升分、同一完成快照盘前丢失量价贡献、收盘退出错误继承开盘状态。论文帮助确定审查边界，不是对这四个具体 bug 的实证证明。修复它们也不构成投资收益提升的证据。

后续经济假设须另行登记：先从受检冻结快照生成分项相关性、排名扰动、换手和成本敏感度诊断；若检验移除趋势家族或修改窗口，再于读取相应未来结果前固定候选族、主指标、样本外日期、账户政策和成本方案。项目已有三候选配对日净收益增量、6 会话块和 BY FDR 契约，不重复新增同类工具，也不追溯改写已登记试验。当前最低日期数是工程准入线，不代表检验功效充分。这是前次研究取舍；现行实现合同与验收边界见[设计文档](DESIGN.md)和[测试计划](TEST_PLAN.md)。

## 平台与问答：2026-09-08研究依据

以下原始来源查阅于 2026-09-08。研究用于发现遗漏的工程条件；采用方案还须由本项目的可复现实验支持。项目是本地 FastAPI/SQLite 服务，当前问答读取结构化研究结果，不是开放文档检索 RAG。

| 原始论文与状态 | 可借鉴的结论 | 限制及当前工程取舍 |
| --- | --- | --- |
| [Svalinn: Overload Control in Large-Scale Servers with Multiple Resource Bottlenecks，OSDI 2026](https://www.usenix.org/conference/osdi26/presentation/pardeshi) | 单一总队列不能代表不同资源的拥塞，需要定位实际受控阶段 | 论文在服务器应用/运行时评估多瓶颈控制。本项目只补工作台组合构建预算，保留已有 provider 预算；不引入自适应分布式控制器，不外推其吞吐倍数 |
| [Composable Building Blocks for Resilient Asynchronous Code，2026-08-21 预印本](https://arxiv.org/html/2608.21489v1) | 缓存、限流、重试和超时的组合次序影响语义；取消等待与底层计算结束必须区分 | 面向 JavaScript/TypeScript 的设计与案例，不是对 Python 线程的终止保证。采用已有 cleanup 机制追踪真实线程和并行子项，不加新包装库，也不把取消当重试失败 |
| [Transactional Cloud Applications Go with the (Data)Flow，CIDR 2025](https://vldb.org/cidrdb/papers/2025/p25-psarakis.pdf) | 去重标记、消息效果和接收方状态须处在同一原子边界 | Styx 的初步实验采用改写的分布式工作负载，不能外推到本项目。提醒与完成状态已在同一 SQLite，可直接事务提交，无需 Kafka 或新协调系统 |
| [Machine-Checked Dual-Write Recovery from a Committed Log，2026-08-13 v4 预印本](https://arxiv.org/html/2608.00501v4) | 来源完成标记不能单独证明接收方效果，迟到工作和恢复竞争需要隔离 | 证明依赖其模型和原子步骤，未覆盖所有复合步骤内部崩溃。借鉴完成状态与效果一致性，不宣称项目已获形式证明或进程崩溃后 exactly-once |
| [Knowing Before Answering，2026-08-27；作者标注 COLM 2026 接收](https://arxiv.org/abs/2608.27661) | 应先区分信息充分、缺失和冲突，再决定是否作答 | 隐藏层路由需要白盒模型，合成数据与自然域存在差异。当前托管 API 无此条件，采用显式支持范围/字段要求；不使用模型自评置信度替代来源校验 |
| [Correctness is not Faithfulness，ICTIR 2025 作者原文](https://staff.fnwi.uva.nl/m.derijke/wp-content/papercite-data/pdf/wallat-2025-correctness.pdf) | 引用看起来支持答案，不代表生成过程实际受证据约束 | 实验模型和规模有限。仅给自由文案附 ID 仍不足，因此改为封闭证据选择、服务端按原证据渲染；模型不能新造事实 |
| [Why RAGs Hallucinate，2026-08-26 预印本](https://arxiv.org/abs/2608.26385) | 应加入答案确实不在知识库中的问题，测量不该作答却作答的情况 | 英文单数据集、18 个缺口 canary、供应商作者背景限制产品比较的泛化。采用缺人员/报告期/公告来源的负例，不复制产品排名或固定惩罚分数 |
| [Simple Testing Can Expose Most Critical Transaction Bugs / WriteCheck，PVLDB 2025](https://www.vldb.org/pvldb/vol18/p2547-cui.pdf)；[Ananke，FAST 2025](https://www.usenix.org/system/files/fast25-liu-jing.pdf) | 事务/恢复测试同时检查返回状态和重新打开的持久化状态 | 前者针对 DBMS 事务，后者针对文件系统恢复；当前缺陷在应用提交回执边界。采用 SQLite 故障注入与独立重开数据库，不把这些测试当作断电或网络 exactly-once 证明 |

当前独立判断是先修正可观察的不一致：组合计算不能绕过资源预算，同步线程不能因协程取消而失去所有者，提醒与完成状态必须原子发布，笔记成功回执须对应已验证提交，问答须在所需证据缺失时明确停止。论文不替代这些不变量的实际测试。LLM 保留按问题选择和排序证据的职责；已有上游证据的现实真实性仍需来源治理。

备份链已具备暂存、摘要/数据库校验、原子替换及失败恢复，该次有界审查未复现新的缺陷。导入大历史的内存放大尚无量化证据，列为后续测量候选。未采用文件系统替换、实时检索、向量库、模型训练或新增分布式基础设施。上述为前次研究与实施范围；现行合同与验收见[设计文档](DESIGN.md)和[测试计划](TEST_PLAN.md)。

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

持仓审计从初始现金的整数分分配重建固定 H+1 个分账户，逐笔检查对应余额及 `sleeve_cash_after`，按声明成本配置复核费用，再对齐每日分账户余额、总现金、费用和成交额。总账户有钱不能掩盖分账户借款；重新计算摘要不能使不一致账本取得审计资格。

概率标签要求规范、严格递增且唯一的会话日期；同日重复行情必须连同成交状态与证据元数据完全一致。标签手数基于毛名义本金，净收益分母含买入费用，不能直接当作固定现金账户收益。联合执行结果的回放使用封存日历元信息，并对照当前可信日历逐项核验信号、完整后续会话、入场与各持有期退出；仅刷新日历元信息不改变旧摘要，交易日期改变、降级或缺覆盖仍拒绝。

Outcome 的 bar evidence v2 将 `session_status` 和 `open_execution_status` 纳入原始行及摘要，生成标签与深度回放均保留停牌、锁板和不可用信息。评估模块与维护工具从价格库读取这些字段，旧只读库缺失信息仍按未知处理，不能升级为正式 PIT 证据。旧 bar evidence v1 只在原始结构、摘要和旧语义验证通过后进入语义漂移目录，需由维护流程重新生成当前产物；不能只改版本字符串或补默认状态后继续复用旧标签。

当前标签为 `market-scan-upside-label-v4-execution-phase-separated`：下一会话开盘入场保留开盘限制，固定会话收盘退出不把开盘锁单外推至全天；停牌、零量、一字板及原有容量约束仍须检查。旧 v2/v3 标签使用旧语义验证全部记录、摘要与质量后报告语义漂移，需要重新生成当前产物，不能重签原文件作为训练授权。日线恢复交易的判断仍是有局限的模拟，不代表真实订单成交。

未来区间新研究版本为 `fixed-session-future-range-v3-execution-phase-separated`。其中价格区间仍按价格证据计算，执行标签区分开盘入场与收盘退出，并保留所用会话和开盘状态，后者进入产物摘要。只改变执行状态也必须产生不同的新产物身份；非法执行元数据使执行结果不可用。旧 v1/v2 产物保持原始冻结内容，不会自动成为新版本研究。

个股 compact assessment 与历史重放各有独立的代理收益标签，不能随共享估计器元数据升级就宣称取得开盘/收盘成交证据。个股旧 assessment 按精确登记的原估计器合同读取，当前构建记录真实版本；旧 v2 assessment 不能重标新估计器元数据。目标、模型、切分、额外字段与来源合同仍严格验证，概率展示继续受既有准入限制。

历史重放 artifact-v2 新增 `probability_fit.estimator_binding`，绑定私有历史标签、共享估计器身份与逐持有期配置摘要。旧 artifact-v1 缺少该绑定，先核验信封与摘要，再以 `superseded-fit-contract` 明确要求重建；不跳过拟合摘要比较，也不声称已在当前实现下完整重放旧拟合。新旧原始文件保持独立，历史收益标签本身没有因该版本变更而获得可成交证明。

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
