# 问答证据与笔记时间：前沿研究及工程方案

核对截止：2026-09-09。研究范围是全市场评分之外的个股问答、用户笔记和交互恢复。本文比较八篇原始论文，包括 2026 年正式会议论文及 9 月 2 日的最新预印本；“最新”不代表结论更可靠。阅读范围为方法、实验设置、关键结果与限制，没有运行作者模型或复现论文成绩。

## 结论与优先级

当前应优先完善证据准入和操作恢复。项目已有结构化规则问答与严格的 LLM 证据选择合同，但这些保护不能自动识别上游错误的事件集合，也不能恢复被取消的浏览器控件。三项已复现缺陷适合本轮直接修正；复杂记忆系统应等到实际检索需求与评估集具备后再考虑。

| 优先级 | 已确认问题 | 具体修改 | 完成判据 |
| --- | --- | --- | --- |
| P1 | 空事件提示经过两层生产者进入事件证据，实际问答被判可回答 | 事件记录显式区分 `event` 与 `availability_notice`；摘要及事件图表只消费实际事件，摘要不再追加默认观察句 | 空事件拒答、无动作、零 LLM 调用；真实观察事件仍可回答，提示仍能显示 |
| P1 | 刷新失败取消问答后，原 DOM 中按钮永久忙碌 | 分开回答回写与控件清理的所有权条件 | 原请求取消后恢复；再次提交实际获得回答；旧请求不清除新请求/表单状态 |
| P2 | 笔记清空日期后，UTC 审计时间无法形成图表日期 | 缺省时按审计时点转换上海日期；显式交易日期沿原市场日语义 | 真实新增→清空→读取标注成功；跨日、偏移、旧格式、隐藏和非法值均有回归 |

公开工作流的修改前反例分别是：空事件问答可靠度 82 且调用一次 LLM；失败刷新后第二次提交没有新增请求；笔记 PATCH 成功且 `visible=true`，标注数量从 1 变为 0。这些是受控案例，不能外推为线上发生率。可复用目标见[维护 Goal](MAINTENANCE_GOAL.md)，现行合同见[设计文档](DESIGN.md)，实际验收见[测试计划](TEST_PLAN.md)。

## 原始论文、适用条件与限制

以下每项分别列论文事实和本项目判断。会议终稿与会后作者修订分开标识；预印本不称为已录用成果。

### 1. Verify Before You Commit: Towards Faithful Reasoning in LLM Agents via Self-Auditing

Wenhao Yuan、Chenchen Lin、Jian Chen、Jinfeng Xu、Xuehe Wang、Edith Cheuk-Han Ngai。2026-07，ACL 2026 主会长文；正式会议论文。[原文](https://aclanthology.org/2026.acl-long.1440.pdf)；[身份与版本](https://aclanthology.org/2026.acl-long.1440/)。

阅读版本：ACL Anthology final, pp. 31201–31225。定位：§3.1–3.5: pp. 31203–31205；§4.1–4.4 / Tables 1–3: pp. 31205–31208；Limitations: p. 31209。

论文事实与限制：SAVeR 由同一模型生成不同视角候选，选择部分推理做结构化审计，只修失败步骤再核验。六个问答/事实核查集分别评估答案与推理。作者承认多候选、反复审计成本高，简单问题可能无益；未证明长期部署安全。 [原文](https://aclanthology.org/2026.acl-long.1440.pdf)

本项目判断：借鉴“提交前检查具体证据条件”，本项目应让占位事件在规则层失去回答资格。无需引入多模型辩论或最多十轮自审。审计器判为无违反不等于形式证明，原文的 faithfulness 指标也依赖审计协议。

### 2. Sufficient Context: A New Lens on Retrieval Augmented Generation Systems

Hailey Joren、Jianyi Zhang、Chun-Sung Ferng、Da-Cheng Juan、Ankur Taly、Cyrus Rashtchian。2025-04，ICLR 2025；正式会议论文（初稿发表于 2024，不能写成 2025 首发）。[原文](https://arxiv.org/html/2411.06037v3)；[身份与版本](https://arxiv.org/abs/2411.06037)；[正式会议来源](https://proceedings.iclr.cc/paper_files/paper/2025/file/33dffa2e3d2ab74a783d1a8c292f66d9-Paper-Conference.pdf)。

阅读版本：arXiv v3 阅读；正式 ICLR PDF 核对题名、作者和会议身份。定位：§3.1 定义与 §3.2 AutoRater / Table 1；§4 错误分层 / Table 2；§5.1 selective generation、§5.2 与 Limitations。

论文事实与限制：把相关上下文与足以作答的上下文分开，结合充分性及模型自评做选择性回答。充分性分类验证仅含 115 个人工样本；定义允许上下文本身错误。缺证仍可能猜对，所以论文追求准确率与回答覆盖率折中，并非要求所有缺证一律拒答。 [原文](https://arxiv.org/html/2411.06037v3)

本项目判断：项目研究问答要求当下可核验依据，比开放域猜答目标更严格。占位提示不满足真实事件前提，应在调用模型前拒答；不能用模型自信、答案碰巧正确替代源证据，也不照搬额外 LLM 充分性裁判。

### 3. Evaluating Memory Capability in Continuous Lifelog Scenario

Jianjie Zheng、Zhichen Liu、Zhanyu Shen、Jingxiang Qu、Guanhua Chen、Yile Wang、Yang Xu、Yang Liu、Sijie Cheng。2026-07，Findings of ACL 2026；正式 Findings 论文，非主会长文。[原文](https://aclanthology.org/2026.findings-acl.351.pdf)；[身份与版本](https://aclanthology.org/2026.findings-acl.351/)。

阅读版本：ACL Anthology final, pp. 7063–7089。定位：§3.1–3.6: pp. 7065–7069；§4 / Table 3 / §5: pp. 7069–7071；Limitations: p. 7072；Appendix I.2: p. 7088。

论文事实与限制：LifeDialBench 在每次新增后冻结记忆、再回答当时问题，限制未来信息。保留原文的方法整体强于压缩方法，但表 3 中 A-Mem 在部分开放问答胜过 RAG，不能照抄摘要说 RAG 全胜。两组对话都含合成，且仅覆盖文本和有限模型。 [原文](https://aclanthology.org/2026.findings-acl.351.pdf)

本项目判断：保留原始笔记和明确日期语义更有价值；读当前可变记录不等于恢复历史快照。旧日期格式兼容应通过既有审计时间解析完成，而非让模型猜日期。本轮标注修复是本地证据驱动，论文没有验证 AShareRadar。

### 4. Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions

Yuanzhe Hu、Yu Wang、Julian McAuley。2026-04，ICLR 2026 Poster；会议身份由官方日程核实。[原文](https://arxiv.org/html/2507.05257v4)；[身份与版本](https://arxiv.org/abs/2507.05257)；[正式会议来源](https://iclr.cc/virtual/2026/poster/10010781)。

阅读版本：正文采用 arXiv v4；这是会后修订，不把其中新增实验冒称会议原版本。定位：§3.1–3.3 / Table 2；§4 / Tables 3,5 / §5 limitation；Appendix E.5、I–K 成本、预算与覆写策略。

论文事实与限制：MemoryAgentBench 分开检索、测试时学习、长程理解、选择性遗忘。逐块写入后才统一提问，并非严格在线逐时评估。冲突任务规定新序号优先；预算匹配只覆盖两类子任务，不能把一个总分或平均成本外推所有记忆架构。 [原文](https://arxiv.org/html/2507.05257v4)

本项目判断：借鉴分层失败测试：证据缺失、旧值与新值冲突、日期与实体错配应分别断言。事实序号覆写不等于真实笔记编辑历史、删除隐私或知识可信度，不能据此自动覆盖用户原始研究记录。

### 5. A-MEM: Agentic Memory for LLM Agents

Wujiang Xu、Zujie Liang、Kai Mei、Hang Gao、Juntao Tan、Yongfeng Zhang。2025-12，NeurIPS 2025；正式会议论文。[原文](https://papers.neurips.cc/paper_files/paper/2025/file/19909c36f51abc4856b4560aff3d36d6-Paper-Conference.pdf)；[身份与版本](https://arxiv.org/abs/2502.12110)。

阅读版本：NeurIPS proceedings final；arXiv 元数据补充日期。定位：§3.1–3.4: pp. 3–5；§4.1–4.7 / Tables 1–4: pp. 5–9；§6 Limitations: p. 9。

论文事实与限制：A-MEM 保留内容与时间，生成关键词、标签和关联，新记录可更新旧记忆的派生属性。在长对话基准上有收益，但组织质量依赖基础模型，部分拒答类别并非最优；检索子路径耗时也不能直接等同端到端写入与问答成本。 [原文](https://papers.neurips.cc/paper_files/paper/2025/file/19909c36f51abc4856b4560aff3d36d6-Paper-Conference.pdf)

本项目判断：原文与派生索引分离值得保留；关联图仅应在实际检索需求和成本验证后考虑。当前没有理由自动改写笔记或给数据库加图结构；LifeDialBench 的结果限制了“更复杂就更好”的外推。

### 6. Memory Injection Attacks on LLM Agents via Query-Only Interaction

Shen Dong、Shaochen Xu、Pengfei He、Yige Li、Jiliang Tang、Tianming Liu、Hui Liu、Zhen Xiang。2025-12，NeurIPS 2025；正式会议论文；另读作者会后 v5。[原文](https://arxiv.org/html/2503.03704v5)；[身份与版本](https://arxiv.org/abs/2503.03704)；[正式会议来源](https://papers.nips.cc/paper_files/paper/2025/file/42a97bbd9844d2bf68596730af80bcdf-Paper-Conference.pdf)。

阅读版本：正式 PDF 核对；方法、实验与防御同时阅读 v5，未混报两版本数字。定位：§3 threat model；§4 injection method；§5.1–5.4 / Tables 1–5；Impact Statements。

论文事实与限制：MINJA 通过普通查询污染供后续示范检索的记忆，实验依赖共享记忆及特定写入规则；EHR/QA 设置会保存全部记录。提示检测存在漏检与误报权衡。论文没有证明每种隔离、身份校验都失效，攻击可达性必须先核对应用架构。 [原文](https://arxiv.org/html/2503.03704v5)

本项目判断：可借鉴低信任内容不得因被存入或检索就升级成规则。当前 QA 不读取用户笔记、不回存模型推理，所以不能宣称现有长期记忆投毒漏洞，亦不能把本轮占位证据缺陷称为投毒攻击。

### 7. LongMemEval-V2: Evaluating Long-Term Agent Memory Toward Experienced Colleagues

Di Wu、Zixiang Ji、Asmi Kawatkar、Bryan Kwan、Jia-Chen Gu、Nanyun Peng、Kai-Wei Chang。2026-05-12，arXiv 预印本，作者注明 Work in Progress；未核实正式会议录用。[原文](https://arxiv.org/html/2605.12493v1)；[身份与版本](https://arxiv.org/abs/2605.12493)。

阅读版本：arXiv v1。定位：§3.1–3.4 / Figure 4；§4.1–4.2 / §5 / Table 2；Appendix E.1 Limitations。

论文事实与限制：以 451 个定制网页问题评估状态、流程、陷阱及错误前提，记忆只向固定读者返回证据；保留原始状态并辅助文件检索有收益。它用预收集轨迹，测证据问答而非实时任务完成，模型与思考预算不同，且延迟成本明显。 [原文](https://arxiv.org/html/2605.12493v1)

本项目判断：“错误前提”适合作为本地拒答测试：没有事件时不能接受“这次利好是什么”的预设。但浏览器取消后表单能否恢复，仍需真实 UI/受控请求测试，不能以这篇记忆问答成绩当作工程正确性证据。

### 8. CAPTURE: Disentangling Preference Drift from Memory Poisoning in Personalized LLM Agents

S M Asif Hossain、Ruksat Khan Shayoni、Md Kishor Morol。2026-09-02，arXiv v1 预印本；作者标注 ICLR 2027 under review，不是已录用。阅读 §3、§5 Q4/Q5、§8、附录 D 的 replay/clarification。[版本记录](https://arxiv.org/abs/2609.02265)

论文事实与限制：系统结合时序跟踪、分层记忆及澄清。相同监督的 Transformer 解释了大部分增益；合成数据和 40 人历史样本限制外推。用户历史实验采用冻结回放，澄清依赖预存人工标签，攻击在回放时注入，不能等同真实在线交互防御。[原文](https://arxiv.org/html/2609.02265v1)

本项目判断：保留来源、时间和适用条件的明确含义，先用确定性规则处理已知的空状态。当前没有可训练的偏好变化标签，也没有用户笔记进入模型记忆的路径，引入神经微分方程会增加新的维护和评估任务。论文不能成为系统自行改写研究记录或用“最近更新”覆盖事实核验的依据。

## 对照后形成的独立判断

**有引用、相关、充分、真实，是不同条件。** Sufficient Context 的充分性定义允许上下文本身错误，SAVeR 的审计也不是外部事实证明。因此，本项目先检查对象身份与来源类别，再按问题判断字段是否足够，最后保持既有证据 ID 和上下文绑定。此次错误发生在第二步之前：系统自己的空状态句被塞进事实列表。增加自审模型不能替代修正这个确定的生产者错误。

**论文摘要也需要与表格对照。** LifeDialBench 表 3 中，A-Mem 在部分开放问答胜过 RAG，不能把摘要概括读成“RAG 在每个任务都更好”。合理结论是复杂组织没有稳定的普遍优势。当前问答使用有限结构化内容，先修正来源语义有直接收益；是否增加检索图，需在本项目问题集上与简单检索同预算比较。

**时间正确性须从产品含义出发。** 笔记交易日期是用户指定的市场日，创建时间是记录进入系统的审计时点，两者不能互换。转换 UTC 审计时点须先转上海再取日期，不能截字符串前十位。这个修复不是实现历史快照：可变笔记的旧内容不会因筛选 `created_at` 自动恢复，`trade_date` 也不能证明该内容当时已知。

**取消后的结果无效，不代表清理动作也无效。** 浏览器必须拒绝旧回答，同时恢复仍属于旧请求的原控件。状态所有权由请求、表单和当前股票共同定义；这一修改来自可复现的 JavaScript 生命周期问题，上述论文均没有证明浏览器取消处理的正确性。

**新机制需比较全部成本。** SAVeR 需要额外模型审计；CAPTURE 的主要收益部分来自监督信息；若只比较最终准确率或检索子路径耗时，会遗漏写入、索引、澄清和错误更新代价。当前无事件可在模型调用前判定，正确的优化是不发无依据请求；本轮不报告未经测量的提速比例。

## 实施边界与后续方案

本轮沿用现有模块和接口，不新增向量数据库、后台记忆改写或模型调用。实际事件可以是行情推断或本地复盘，分类为 `event` 并不升级为公告事实；缺失源继续披露。用户笔记目前不进入问答上下文，模型也不把推理写回共享记忆，所以 MINJA/CAPTURE 的攻击设置不能直接证明这里存在同类漏洞。

后续可以按以下顺序推进，每项独立验收：

1. **笔记编辑冲突保护。** 已复现两份旧表单先后提交会覆盖先成功的新内容。为记录提供明确修订身份；更新/删除在同一事务核对期望版本，冲突返回 409，页面保留草稿并支持重新读取比较。验收包括同字段冲突、隐藏与编辑交错、删除、事务回滚和导入恢复。单纯串行写事务无法解决基于旧读的覆盖。
2. **离线创建本地记录。** 已复现显式给出价格和日期的笔记、已停用预警仍因报价失败返回 503。先从可验证本地股票身份解析名称、代码和市场；明确缺省价格/日期的输入规则，再解除不必要的行情依赖。不能伪造 Quote 或把缓存历史价格当现价。
3. **完整历史与检索。** 当前页面展示数量有限，应明确完整数量、排序和翻页失败恢复。若再接入笔记问答，先确定查询“当前笔记”还是“当时已知内容”，保存可核验版本和来源；用缺证、冲突、删除、股票错配、时间推进和注入样例分别衡量正确性及拒答，之后才评估向量检索或派生关联。

上述候选是该次研究的后续建议；笔记条件写入和显式字段的本地创建现已进入[笔记写入专项](NOTE_WRITE_RESEARCH.md)，完整历史分页仍待设计。该次三项修复保持有效；真实行情有效性、模型收益与投资收益均不属于这些工程回归的证明范围。
