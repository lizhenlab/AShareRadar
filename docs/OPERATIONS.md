# 运行手册

## 1. 环境与启动

在项目根目录执行。应用支持 Python 3.12；开发检查支持 Node.js 22/24 与 npm 10/11。

```bash
export PROJECT_ROOT="$PWD"
export PYTHON="$PROJECT_ROOT/.venv/bin/python"
export PYTHONNOUSERSITE=1
python3.12 -m venv .venv
$PYTHON -m pip install --require-hashes -r requirements-lock.txt
$PYTHON tools/run_local.py
```

`tools/run_local.py` 是本地单进程启动入口。仅此入口发现项目 `data/fuyao-api-key` 时，为本次服务设置扶摇启用、密钥文件路径及已验证下载域名 `o.thsi.cn`；显式环境变量优先，包括显式关闭。文件只保存 Key 本身，须由当前用户拥有、无组/其他用户权限（建议 `chmod 600 data/fuyao-api-key`），所在 `data/` 已忽略提交；不要把 Key 写入命令、文档或浏览器。入口不因启用数据源而自动开始远端同步。

标准 Uvicorn 入口仍可使用，且只遵循显式环境配置，不发现或自动加载上述本地密钥文件；未配置时扶摇默认关闭：

```bash
$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --timeout-graceful-shutdown 5
```

后台本地开发可使用独立 screen 会话：

```bash
screen -dmS ashare_radar bash -lc 'cd "$PROJECT_ROOT" && exec env PYTHONNOUSERSITE=1 "$PYTHON" tools/run_local.py > /tmp/ashare_radar.log 2>&1'
tail -f /tmp/ashare_radar.log
```

保持单 worker：调度和扫描控制状态是进程内的。统一 runtime-leader 租约可阻止两个进程同时拥有正常后台任务，但不代表支持多 worker 部署。HTTP 优雅关闭预算与提供者工作停止是不同边界；取消抵抗的任务结束前租约不会释放。手动任务仍在执行时调度器暂拒重启，协调器等待其结束后自动重试，不将仍在工作的任务标为孤儿取消。自动扫描的慢预检不再占住普通任务调度循环，但预检本身仍受原超时与停止边界约束。

## 2. 停止与升级

向本项目的 Uvicorn 进程发送 Ctrl-C / SIGTERM，等待进程退出，再启动新实例。使用 screen 时：

```bash
screen -S ashare_radar -X stuff $'\003'
lsof -nP -iTCP:8010 -sTCP:LISTEN
```

监听器必须消失；同时确认没有其他使用同一数据库的旧进程。不要只关闭终端、删除锁文件或启动第二个实例掩盖卡住的旧任务。

全市场重验证读取关闭时先停止新准入，等待已拥有的读取结束后再关闭后端资源。重复取消和刚开始关闭时的取消同样遵守此顺序；已结束读取的清理不再依赖忙循环等候事件回调。

所有数据库迁移都在停机窗口进行：先备份并验证，停止全部旧服务，再运行新版本。UTC 审计时间迁移要求明确旧时区、有足够空间和未占用的租约；无效旧数据会回滚。结构迁移先建立兼容列/索引、验证封存图，再安装依赖它们的不可变触发器；迁移标记最后写入。旧来源无法核实时保持未验证状态，禁止手工补摘要或改状态伪造成功。

日线缓存执行元数据迁移 `20260908_kline_daily_execution_evidence_v1` 只保留已有信息：旧库缺失的会话、开盘执行和公司行动状态保持 `unknown`，PIT 标记为 false，复权因子与元数据版本为空。迁移不能恢复历史上已丢失的字段。已有字段须通过原始类型和取值检查，不能把字符串 PIT、字符串因子或二进制版本经 SQLite/Pydantic 强转成有效证据；检查失败时事务完整回滚。离线概率维护工具对旧价格库保持只读，采用相同的严格读取规则，不为生成新证据修改源库。

## 3. 数据、备份与恢复

`data/` 是本机运行状态，包含 SQLite/WAL/SHM、租约、交易日历、研究产物与备份，不提交到源码仓库。锁文件存在不等于锁被占用；运行中的文件不得删除或重建。

扶摇研究资料单独保存到 `Settings.cache_path.with_suffix('.fuyao')`：`research.sqlite3` 保存观察版本、同步任务及本项目请求预算，`history/` 保存原始 Parquet、规范化未复权日线/企业行动和版本 manifest。默认位置为 `data/ashare_radar.fuyao/`。这些侧文件不在主数据库备份或浏览器用户数据导出中；备份、迁移和磁盘容量检查须另行包含它们。密钥文件独立管理，不放入可分享的研究导出。

完整数据库备份使用 SQLite backup API，同时生成摘要、表计数及完整性清单：

```bash
$PYTHON tools/runtime_data.py backup
$PYTHON tools/runtime_data.py verify data/backups/ashare_radar_TIMESTAMP
```

使用命令输出的实际路径代替示例时间戳。管理目录默认保留两份备份；显式外部目标不参与自动轮转。验证要求摘要、SQLite integrity_check 与 foreign_key_check 全部通过。

Restore a backup while the service is stopped. 恢复工具先验证源、检查数据库与运行租约、创建目标回滚快照，原子替换后再次验证：

```bash
$PYTHON tools/runtime_data.py restore data/backups/ashare_radar_TIMESTAMP --confirm-service-stopped
```

SQLite 备份不包含研究侧文件。任何受管概率、来源、结果、拟合、未来区间或个股概率目录非空，或固定摘要存在时，DB-only restore 都会直接拒绝；即使文件看起来来自同一批次也不例外。工具不比较这些侧产物是否同源，也不提供成套恢复，应先独立核验并安排一致恢复方案。

成功恢复会为安装的历史生成新的预警流身份，起点为恢复库的事件高水位；即使恢复同一备份、事件ID相等或更大，也会换代。浏览器保留旧游标时可从新起点继续接收恢复后新增事件，不补发恢复库已有历史。工具保留原备份字节与清单，只在严格验证后的私有暂存库修改通知协调元数据，再验证安装结果。失败回滚按原目标的一致性回滚快照字节恢复数据及身份，不承诺还原活跃WAL主文件的原物理布局；元数据损坏须排查备份/数据库，不通过清空游标或改表掩盖。

Before deleting or replacing local data at the filesystem level，或执行 DB-only restore、数据库迁移及彻底重置，先停止全部使用该数据库的服务，并创建、验证备份。备份和恢复都针对 Settings 解析后的同一数据库路径；不要把自定义数据库的备份与默认路径的删除命令混用。本手册不提供绕过目标检查的裸文件删除命令。需要彻底重置时，先核对实际配置、数据库及全部关联侧文件，再制定明确的备份与重置范围。

“系统维护 → 数据管理”的清理与用户数据导入则在服务运行时使用：先预览，再通过受检接口提交，由维护租约、事务及各自的备份规则保护操作，无需先停服务。清理预览通过“检查可清理数据”按钮显式启动，进入页面不会自动校验全部研究文件；预览确认有可清理项后才启用执行，执行前仍重新检查。在线清理的逻辑删除先事务提交，再按原空闲空间阈值尝试压缩；界面的删除成功/计数不保证数据库文件立即变小，读写竞争导致可选压缩失败时，已删除记录仍保持提交。

不要用复制活跃 WAL 文件代替一致性备份；保留研究文件也不意味着新建空数据库会使这些产物有效。

“系统维护 → 数据管理”的用户数据导出只包含允许的自选、笔记、预警、研究历史、复盘与模拟记录，不包含凭证、缓存和完整运行来源图。导入支持 merge/replace，先 dry-run；文件、模式、时区或目标状态变化后必须重新预览。不可变历史同键异值拒绝整次导入；父子键重映射后仍验证摘要与外键。replace 前创建当前备份。

replace提交时同步切换预警流身份；merge和dry-run保留当前流。流身份属于本机协调状态，不从用户导出文件复制。普通服务重启和保留清理不会建立新通知历史。

“系统维护 → 数据管理”可填写“旧文件来源时区”，使用原导出设备的 IANA 时间基准，例如 `Asia/Shanghai`；带明确时间元数据的新备份无需填写，无时区旧文件留空不会自动推断。改变时区后先重新预览，确认影响范围后再提交。

## 4. 日常工作与诊断

```bash
curl -sS http://127.0.0.1:8010/api/health/live
curl -sS http://127.0.0.1:8010/api/health/ready
curl -sS http://127.0.0.1:8010/api/data/status
curl -sS http://127.0.0.1:8010/api/system/reliability
```

liveness 只证明进程活着；readiness 与数据源健康分别检查可服务状态和实际可用性。网络失败先查看能力级冷却、准入压力、报价/K 线时间与市场覆盖，不能反复新建扫描绕过失败原因。

- 个股普通加载不发布正式建议；盘后研究队列要求完整收盘证据。概率面板只读取数据目录里的验证产物，无产物时应显示不可用。
- 全市场页面显式新建扫描；读取旧发布、筛选和导出不抓取提供者或修改分数。股票缺失可跳过；全链路故障保持 pending 并执行有界批次恢复。
- 自动扫描默认关闭；启用前检查交易日历、市场池覆盖、提供者预算与实际数据容量。
- 复盘计划修改/归档须使用当前 revision；历史修订和评估保留。模拟只有明确请求才运行，不连接券商。
- 浏览器通知依赖页面保持打开，没有后台 service worker。点击单条通知可打开相关股票及通知捕获时的只读详情，当前行情另行加载；摘要仅打开通用提醒入口。旧通知不按可能复用的事件ID查询新历史，点击不等于已读。升级部署后刷新所有打开的旧标签页；首次从旧版游标升级保留通知开关，建立当前基线并提示不补发历史，不支持新旧页面同时协调。之后备份恢复或replace导入会自动识别新流并继续接收新事件；手动停用再启用仍仅建立当前基线。系统通知创建不代表已读，崩溃时不承诺严格一次交付。草稿失败时保留，切换股票不能让旧响应覆盖当前记录。
- AKShare/Tushare/Futu 为可选来源；未配置时应明确不可用。演示源不应参与真实研究。
- 内部模型构造、数据库和提供者错误返回脱敏不可用信息。请求结构或字段类型校验失败返回 422；格式合法但业务取值不允许通常返回 400，例如价格预警阈值为负数。完整性或修订冲突保留各自的独立错误类别，不统一改成参数错误。

离线研究输入、输出与授权边界见[研究工具](RESEARCH.md)。线上 canary 会访问真实提供者，不能把它混入离线 CI。

### 后台刷新窗口与失败退避

行情与研究后台任务按上海可信交易日历运行，窗口结束时刻不含；本地缓存清理使用独立夜间窗口，不依赖交易日历。任务间隔从上次实际执行结束后计算，再顺延到下一允许窗口；`GET /api/tasks/status`的`next_run_at`显示顺延后的时间。

任务状态中的扫描头使用现有导航读取，不为展示进度重新校验整批股票结果。该状态不能授权发布、评分或交易；正式结果读取仍执行原封存验证。健康检查只做数据健康诊断；数据保留由独立的“清理过期运行缓存”任务负责，不再使启动健康检查同步等待研究档案完整校验。

缓存状态统计将日K行数、最新抓取时间及最新交易日期合并为一次按日期聚合，使用 `(adjustment_mode, date, fetched_at)` 覆盖索引；仅对不同日期做市场时间标准化，仍区分市场时间和抓取时间。索引在正常数据库初始化时幂等建立，旧库升级前按上文备份并停止旧服务；它会增加磁盘和日K写入成本。诊断表计数使用独立只读事务取得同一已提交快照，不等待清理事务持有的应用锁，也不借用清理中的未提交连接。

研究档案读取可在同一次调用内复用已严格验证的结果；按批次选择完来源后，仍重读并比对原始压缩字节，删除或改写会拒绝返回。清理时按常用摘要口径依次验证，匹配后不再计算等价的旧口径；被摘要排除的时间字段仍须是有效的有限JSON和UTF-8。自动清理先从独立已提交快照检查是否存在全市场批次删除候选；没有候选时可只清理可再生缓存，写事务内再次检查，若候选出现则整次回滚，下次重新完整验证。有批次候选、手动清理及手动预览仍须完整验证研究档案，事务期间继续核对字节与目录身份。此预检不授予档案有效资格，也不把哈希、进程缓存或既往校验当作执行证据。保留排序仅投影主键及保护规则所需字段，不复制整行大型载荷；保留上限、分区、排序和引用保护不变。

| 任务 | 自动执行窗口 |
| --- | --- |
| 自选报价、行业背景、本地预警 | 交易日09:15–11:30、13:00–15:15 |
| 全市场股票池元数据 | 交易日08:30–09:15、15:15–20:00；每小时检查一次，新鲜完整池跳过远端刷新 |
| 关键个股日K、已保存策略任务 | 交易日09:15–11:30、13:00–20:00 |
| 主动研究队列、到期计划评估、概率标签维护 | 交易日15:15–20:00 |
| 数据健康检查 | 上述日间工作窗口至少间隔5分钟，其余时间至少15分钟；启动时保留一次检查 |
| 清理过期运行缓存 | 每日20:00–次日09:00；沿用运行维护间隔，首轮至少延迟该间隔且不短于5分钟 |

失败或降级会逐次加倍自动重试间隔，默认最多延长到1小时；用户原本配置超过1小时的间隔不会缩短。成功后恢复基础间隔；概率维护因合作式预算分段时显示“待续”，按基础间隔继续，不作为失败退避；已知证据隔离或实际失败仍报告降级。取消不增加失败次数。退避状态属于当前进程，重启后重新计算。行情与研究任务没有可信日历覆盖时暂缓自动执行，明确显示原因，6小时后只重新检查日历，不为调度判定发起供应商请求。手动“立即执行”仍可使用，但保留具体任务的数据与执行准入。

缓存清理开始前检查其他调度任务、全市场扫描、扶摇运行任务及研究索引维护；有工作时显示待续，不执行清理，也不作为失败退避。无需删除或仓储维护间隔未到时如实显示未清理。清理仍保留原备份、引用保护、事务回滚和取消收尾规则；夜间窗口与空闲检查不能保证整个执行期间没有新用户请求。

股票池任务仅在本地所有库存行仍新鲜、总量及沪深北三市场覆盖满足配置时跳过刷新；否则通过原有全量股票池解析入口更新，不逐股票取数。完整提供者结果及其本地新鲜完整缓存均通过后才报成功；部分池、旧缓存回退、写入未落地继续显示降级。保留现有异常缩水、共享请求、能力冷却和数据源顺序，独立于全市场自动扫描开关。

全市场自动扫描继续使用自身开关、发布时间及重试规则，默认关闭；Codex“策略前瞻样本采集”的工作日15:45、17:45、21:45跟进也独立管理，不随普通任务间隔改变。

行业排行的东方财富直连请求只在传输失败时尝试一个固定延迟行情入口；共享6秒协作式内部预算、每响应512KiB上限，不跟随重定向。预算不保证底层同步网络线程严格在6秒终止，worker收尾仍受原提供者生命周期管理。协议、分页或字段错误直接拒绝，HTTP 429/5xx也不触发本备用路径。延迟入口明确标记来源、保留真实事件时间，仅供排行参考，不进入实时行业评分。全部入口失败时仍保留旧缓存原时间；超过现有缓存回退期限则明确不可用。2026-09-22的真实连接检查仍遭上游间歇断开，不能把增加备用入口写成行情已恢复。总行业数超过100时允许完整的前100条排行，不因总数大于页大小误拒绝；短页、重复、失序和字段无效仍拒绝，不追取额外分页。提供者失败时立即设置能力冷却，排队请求真正进入并发槽位前再次检查，避免等待数据库记录期间继续冲击失败来源。

概率标签维护会隔离已核验机械封存身份但无法按固定会话重放的结果，维护摘要保持降级并报告档案数量。对应来源暂停重复构造，其所属比较组暂停新拟合；其他有效来源可继续维护。隔离结果也使用原有文件身份缓存，未变文件不再每轮重复重放，文件改变重新核验。进程内保留已经识别的拒绝身份，不能因文件消失而解除阻断；只有同一来源有严格验证通过且更晚的结果，才能解除。此缓存不替代完整备份或跨进程持久审计，重启后从保留的原档案重新核验。旧文件不改写、删除或重签，隔离不授予标签、拟合或策略采用资格。独立研究缓存也保留机械身份绑定的重放拒绝，阻断同组拟合和已有旧模型的概率展示；无关组仍可继续核验。坏哈希、无身份拒绝和文件变化继续按原校验失败处理，不能把旧结果修复为有效样本。

自动概率维护每次采用60秒合作式预算，在目录文件、来源和拟合组之间检查；已严格验证的文件按完整文件指纹在当前进程内复用，完整目录稳定前不生成标签或拟合。单个原子验证/成熟/拟合步骤可能超出预算，后续研究索引另有300秒子进程总预算，因此60秒不是整项任务硬超时。研究索引分块保存验证进度，完整快照核验后才发布，隔离记录在原文件消失后仍保留。重启后从原档案重新校验，不把进程缓存当成独立证据。CLI可显式传`--time-budget-seconds`，待续退出码为2，降级为1；独立CLI进程间不保留缓存，默认仍完整执行。

前台概率及历史研究投影只读取已提交索引，并核对目录身份、文件指纹、日期和来源绑定。冷启动或输入变化立即显示`source_index_verification_pending`，不在请求线程中解压重放全目录，也不先展示旧概率。请求由扫描服务生命周期内的单个后台协调器合并；失败按30秒起、最多300秒退避续跑，使用同一预热锁、来源绑定和隔离验证。服务关闭先撤销回调，等待实际验证worker结束；旧回调不能在新一代生命周期重启任务。这里的后台刷新只读取本地档案，不为打开概率面板下载供应商数据。

索引已经按当前持久化绑定完整验证后仍缺少对应source时，明确报告档案缺失或摘要冲突，不无限显示待验证或反复触发刷新。目录变化后的旧投影不能与后来刚完成的索引状态拼接成假缺档错误。后台失败写入脱敏监控事件；事件保存自身失败仍记录安全日志。索引发布、outbox恢复/审核及失败记录的正在执行的worker均须收尾，取消后不再进入下一阶段。

历史回放参考与当前来源索引分别处理可用性。安全且稳定的历史目录中，确定的旧合同或内容拒绝显示`historical_context_integrity_unavailable`，首次验证保留原拒绝原因用于记录；同一快照后续返回零个可用上下文，避免反复深验。它不授予拟合、筛选或排名资格，也不阻止通过原验证的当前来源激活归档outbox。文件或其绑定来源变化后重新验证；不可信路径、临时I/O失败及验证期间变化不保存为稳定拒绝快照，后台保留受管退避重试。当前来源本身校验失败仍阻止激活，不因历史参考可选而降级放行。

交易日计数基于当前选中的不可变日历有序索引进行区间查找，保持首尾包含及可信覆盖检查，日历刷新后重新构造索引，不改变交易日内容或旧试验冻结日历。

### 研究队列与历史计划

“我的自选”可搜索代码、名称、分组或关注原因，并组合研究状态、“到期复核”和“有新变化”。到期包含上海日期的今日与逾期；条件仅控制显示，不暂停后台关注、不清除未读变化。使用“清空筛选”恢复完整队列；筛选前后仍可继续原行的编辑草稿。

在全局复盘看板选择“查看计划”会定位那一条计划，同一股票较早的计划也可直接打开。其他历史记录仍可通过“加载更早计划”浏览；定位操作不会触发评估。行情暂不可用时，可只读查看标明股票代码的本地冻结计划；重新点击“查看计划”重试行情后才能操作。计划不存在或已归档时，应刷新看板核实状态。

选择“已到期”后按50条一页读取队列，股票、快照起始日和周期筛选作用于服务端完整到期集合；页面显示筛选总数、当前页和截止时间。翻页失败会保留成功页，可就地重试；提示队列发生变化时，重新读取第一页以采用最新记录。“刷新队列”开始新的读取，截止时间只表示到期判断时点，不表示历史数据库快照。全局统计不随这些筛选改变，“批量评估全局到期计划”每次最多处理100条，范围不限当前页或当前筛选；需要处理更多时，在本次返回后再次明确执行。批量结果中的候选数量只是本次检查窗口，不是整个队列总量。

### 策略定时任务管理

在全市场选股的策略实验室中载入已保存策略，展开“管理已保存的定时任务”。可分页查看该策略各固定版本的任务、最近执行信息，并逐项停用或恢复。归档策略仍可查看和停用任务，但不能恢复；恢复后由现有调度流程处理，不会在点击时立即执行。保存新版本不会自动改写旧任务绑定的版本。

启停成功但列表同步失败时，使用“刷新任务”核实；没有收到明确成功回执时，不自动重试写请求。查看或刷新任务列表只读取本地状态。

### 扶摇研究数据

“系统维护 → 数据管理 → 扶摇研究数据”提供接入状态、项目当日请求数和显式同步入口；“个股研究”的财报面板读取当前股票的本地记录，支持报告期选择及后台刷新。打开页面、刷新本地记录和轮询任务都不触发远端取数。任务通过 `POST /api/fuyao/jobs` 提交；`GET /api/fuyao/status`、`GET /api/fuyao/stock?symbol=600519.SH` 与 `GET /api/fuyao/market` 只访问本地状态，返回不缓存响应。首次状态读取会把上次进程遗留的运行中任务标记为中断；这不恢复采集或占用供应商请求预算。

任务列表可查看原始参数、已完成股票和阶段进度。历史任务显示签名、下载、校验、归并及写出阶段；仅显示实际取得的字节数、行数或项目数，未知总量不显示百分比。运行中可主动停止；“正在停止”表示后台仍在收尾，此时不能开启新同步，须等待实际终态。停止保留已保存观察和已发布历史版本，不撤销已发生的请求或供应商费用。输出已全部提交时，迟到的停止会返回原完成结果。进度保存失败会明确提示本地存储问题；数据已发布时保留完成结果，尚未发布时停止任务。

失败、部分完成、中断或取消后可显式补做：财报/估值只处理原任务未完成的股票；历史、板块和情绪按原参数重新执行整批校验。补做创建带父任务ID的新任务，原记录不改变；重复点击同一父任务返回既有子任务，子任务仍失败时从子任务继续补做。旧任务没有保存请求参数时显示不可直接补做，需要重新选择参数创建任务。API 对应 `GET /api/fuyao/jobs/{id}`、`POST /api/fuyao/jobs/{id}/cancel` 和 `POST /api/fuyao/jobs/{id}/retry`；未知任务返回404，不能补做的状态或缺失参数返回409。

财报任务按指定股票读取利润表、资产负债表、现金流量表及一个报告期指标；年度/季度、供应商报告日期、来源和获取时间分别保留。原始金额及没有明确单位的指标显示“单位待核实”；季度单季/累计及首次披露/修订历史仍未确认，因此不由三表推算 TTM、财务健康分或全市场选股加分。结构化财务问答只回答已缓存且能对应字段的事实，不给交易动作。同步季度后，先前已缓存的年度报告仍可选择；同一期使用最新含报表观察，只有指标的响应不会替换已有报表。页面和问答标注对应报告期的实际获取时间。全空响应按失败处理，保留旧记录，可在任务中补做未完成股票。

估值同步保存 PE TTM/MRQ、PB MRQ、PS TTM、PCF TTM 的当前观察；历史分位仅依据本地同口径观察日，不能解释为供应商完整历史估值。显式同步后，个股研究可从本地采用通过股票身份、摘要和时间校验的 PE TTM / PB MRQ：获取超过 7 日、未来记录、数值格式异常或两项均无有限非零值时，沿用原行情评分并显示未采用原因。批次时间存在时也须在七日窗口内且不晚于获取时间；重新同步不保证旧上游值重新获得评分资格。页面读取不自动补采；需要更新时由用户发起估值同步。估值观察若晚于页面所用行情时间，也不会用于该旧行情批次；刷新行情并重载研究上下文后重新判定。工作台缓存窗口内沿用原观察，财报/估值记录面板与工作台各自显示获取和评估时间。供应商时间是批次内最新有效时间，不证明当前股票每项指标同时更新。

采用扶摇后，总览原“基本面”因子与估值分析共用同一分数：基准 55，正 PE 小于 25 加 8，大于 60 或为负减 8；正 PB 小于 3 加 6，大于 8 或为负减 6；等于阈值不调整。零值和缺失项不给调整，不重新分摊到另一项；仅有一项时也不把它放大。页面说明数据来源和口径，不将行情历史、同行分位拼入这份分数。该规则反映估值压力，不能解释为完整财务体检、上涨概率或论文验证的收益预测；全市场选股权重不变。具体准入合同见[设计](DESIGN.md)，研究局限见[评分研究](SCORING_RISK_RESEARCH.md)。

扶摇记录面板同时显示价值研究摘要，API 字段为 `value_research`。估值输入覆盖显示 0/2、1/2 或 2/2，表示本次已准入的 PE TTM / PB MRQ 数量，负数也可计为有输入；覆盖数不是置信度。正倍数另展示盈利收益率和账面价值比的倒数换算，缺失、非正值或结果溢出时显示原因。盈利收益率不是股息率或预计投资回报，账面价值比不是清算回收率。摘要反映本次缓存读取时点，工作台沿用自己的行情批次与缓存时点，两者无需强制相等；摘要不会改变工作台评分或全市场排名。

切换财报期间时，价值摘要随所选年报/季报展示合并净利润、经营现金流及所有者权益的正负，并保留该期来源时间。只有同年报三表齐全、利润和现金流字段唯一可用时，才提示正利润与负经营现金流的分歧；提示用于核查营运资本和非现金损益，不等同造假判断。季度不作这一联合判断，缺数不解释为健康，负权益不等同破产。原行情估值若只有市值或 PE/PB 均为零/缺失，则显示不可用；市值不再形成估值加分。PE/PB 历史和同行样本中晚于当前行情的记录不计入分位，排除后样本不足会显示缺口。

板块同步保存行业/概念目录、当前行情和明确选定板块的当前成员；情绪同步核验涨停/跌停/炸板分页，另取同日龙虎榜和有条件的当日个股异动解释。当前成员不反推历史，供应商理由不升级为公告事实。同步失败保持已有记录，并显示失败或部分完成状态。

历史数据先做全量，再做近十交易日增量；每次同步通常需要两个签名 API 请求和两个独立文件下载，重试另计。全市场日期缺口、非法 OHLC、日线源内同证券同日的冲突重复或摘要错误会阻止发布；相同日线重复合并，增量修订计数记录在新版本中。企业行动仅去除全部规范字段完全相同的重复，同证券同日的不同记录完整保留、稳定排序，并在 manifest 注明歧义组数及事件/版本待核对，禁止直接求和、选择最后记录或自动复权；负股本变动比例保留原值并标记缩股语义，不能直接当作每类股东的持仓变动比例。下载使用独立无 Key 会话，只接受配置中的精确 HTTPS 域名，检查公网 DNS、拒绝重定向，按容量上限流式保存；不记录签名 URL。版本发布前以 8192 行批次校验和磁盘归并，优先校验较小的企业行动文件，保留原始文件及摘要；旧版本不会被原位修改，失败不会切换当前版本。

命令行同步使用与服务一致的配置和持久请求预算，不自动套用 `run_local.py` 的本地启用规则。以下无 Key 的配置示例适用于已存在且权限正确的本地密钥文件；已有显式配置可直接复用：

```bash
export ASHARE_RADAR_FUYAO_ENABLED=1
export ASHARE_RADAR_FUYAO_API_KEY_FILE="$PROJECT_ROOT/data/fuyao-api-key"
export ASHARE_RADAR_FUYAO_DOWNLOAD_HOSTS=o.thsi.cn
$PYTHON tools/sync_fuyao_history.py full
$PYTHON tools/sync_fuyao_history.py incremental
$PYTHON tools/sync_fuyao_history.py status
$PYTHON tools/sync_fuyao_history.py verify
$PYTHON tools/sync_fuyao_history.py export --output data/research/fuyao-export-20260910
```

导出目标必须是尚不存在的目录。`status/verify/export --root PATH` 可只读其他已发布档案，不读取配置或密钥；联网 `full/incremental` 固定使用项目配置目录，不接受 `--root` 绕开预算。`verify` 会遍历文件摘要，普通页面只读 manifest。Parquet 使用锁定的 `pyarrow==25.0.1`；未安装时在发请求前报错。导出只包含未复权日线 CSV、企业行动 CSV 和来源 manifest，明确 `point_in_time_verified=false`、`official_execution_admitted=false`；它不写入当前前复权行情缓存，正式回测仍需历史成员、披露版本、交易状态及独立来源准入。

2026-09-10 已用真实账号取得 30 只有效沪深京股票的三张财报、财务指标及估值观察，样本为 25 只沪深股票和 `920002.BJ`、`920001.BJ`、`920003.BJ`、`920005.BJ`、`920006.BJ`；旧代码 `430047.BJ` 单独返回业务码 `3001`，未影响有效股票完成。板块目录及行情各 710 条；同日情绪数据为涨停 34、跌停 11、炸板 22、龙虎榜 60 条，选定股票的异动结果为 0 条，不能解释为全市场无异动。

2026-09-10的真实全量历史归档已发布并通过 CLI `verify`：5,560 只股票、10,275,240 条日线、2,427 个交易日期，覆盖 2016-09-12 至 2026-09-10。企业行动由 57,184 条原始记录去除 1 条完全重复后保留 57,183 条，同时保留 2 组同日不同记录及 2 条负股本变动记录的缩股标记。本地校验、规范文件写入及版本发布耗时 165.65 秒，不含下载时间。2026-09-19已完成真实联网增量同步，新发布的未复权档案覆盖至2026-09-18、共5,565只股票；这些历史版本的计数不能混作同一版本统计。新档案仍标记`point_in_time_verified=false`，不更新生产行情缓存。

上述结果证明这批样本及归档在本次运行中通过验证，不证明全部证券类型覆盖或长期权限稳定性。费用政策、财务单位和 PIT 仍未核实，不能认定接口免费；本地请求上限是工程限制，不是供应商余额或费用估计。下一步应完成小范围行情/企业行动对账，以及披露版本、单位和费用政策核实，再评估前复权转换与正式研究准入。

三策略未来样本使用[前瞻采集操作指南](STRATEGY_PROSPECTIVE_COLLECTION.md)：`collect_strategy_prospective.py`的五个子命令均离线，只读扫描及历史档案，另写本地计划、完整输入快照和研究报告。计划绑定`app/`、`tools/`全部Python源码，发布代码变更前应检查计划状态；源码漂移会阻断继续采集，不能静默更新旧计划摘要。运行目录需单独备份。

调度与这些离线命令分开管理。先查计划状态及在途任务，仅在预定锚点按需启动一次正式全市场扫描，并在上海时间20:00截止前捕获。每交易日最多执行一次`history_incremental`，先核对持久预算；一次成功增量路径包含两个签名接口请求及文件下载。不为缺口购买新订阅，也不发送外部消息。截止后的检查只登记缺失或报告故障，不补成按时样本。默认官方执行注册表在2026-09-19检查时尚不存在，研究档案不能替代许可、交易状态和完整执行证据。

## 5. 环境变量

Use the `ASHARE_RADAR_*` namespace for new configuration. Legacy aliases are accepted where listed for local compatibility. Process environment values take precedence. For the five allowlisted `ASHARE_RADAR_LLM_*` names only, the application falls back to simple top-level assignments in `$HOME/.zshrc`; it parses that file without sourcing or executing it and ignores command substitutions, nested shell blocks, and unrelated names. When that file contains `ASHARE_RADAR_LLM_API_KEY`, it must be owned by the current user and have no group/other permissions; run `chmod 600 "$HOME/.zshrc"` before startup. It does not read `.env` files, project configuration, user-data imports, or browser storage for credentials. Settings are captured by the application container, and scheduler intervals/task registration are not hot-reloaded. Restart the single process after changing configuration.

扶摇 Key 仅来自支持的进程变量或显式配置的本地密钥文件；不从 `.zshrc` 搜索。`tools/run_local.py` 的本地文件发现是上述专用启动入口的行为，不改变 `Settings` 和标准 Uvicorn 的默认关闭合同。

| Variable | Default | Legacy alias | Notes |
| --- | --- | --- | --- |
| `ASHARE_RADAR_LLM_API_KEY` | empty | - | Secret; process environment first, then the allowlisted `$HOME/.zshrc` fallback. |
| `ASHARE_RADAR_LLM_BASE_URL` | empty | - | OpenAI-compatible absolute endpoint; HTTPS is required except for loopback development, and query/fragment/userinfo components are rejected. |
| `ASHARE_RADAR_LLM_MODEL` | empty | - | LLM evidence-selection model; required together with API key and base URL. |
| `ASHARE_RADAR_LLM_ENABLED` | `1` | - | Set `0` to force rule-only answers. |
| `ASHARE_RADAR_LLM_TIMEOUT_SECONDS` | `30` | - | Positive finite total budget shared by initial generation and the optional validation-correction request. The browser timeout is fixed at 35 seconds, leaving a margin over the default 30-second server budget. Increasing this setting does not extend the browser timeout; the browser may time out before the server finishes. |
| `ASHARE_RADAR_TUSHARE_TOKEN` | empty | `TUSHARE_TOKEN` | Secret for optional Tushare provider. |
| `ASHARE_RADAR_FUYAO_ENABLED` | `0` | - | 扶摇研究接入开关；启用不自动采集。`run_local.py` 发现本地密钥文件时仅对该次启动提供默认值，显式环境配置优先。 |
| `ASHARE_RADAR_FUYAO_API_KEY` | empty | `HITHINK_FINANCE_API_KEY` | 进程内秘密值，主名称优先；不返回浏览器或进入日志/模型序列化；有值时优先于密钥文件。 |
| `ASHARE_RADAR_FUYAO_API_KEY_FILE` | empty | - | 当前用户拥有的普通密钥文件，不接受符号链接、组/其他用户权限或超过4096字节的文件；建议0600。相对路径按项目根解析，只有取数时才读取。 |
| `ASHARE_RADAR_FUYAO_REQUEST_INTERVAL_SECONDS` | `1.0` | - | 同账号跨能力最小请求间隔，范围0–60秒；配合提供者并发准入、有限重试和冷却。 |
| `ASHARE_RADAR_FUYAO_DAILY_REQUEST_LIMIT` | `1000` | - | 当前项目数据目录每日持久请求上限，范围1–100000；按上海日期，含失败尝试和重试。不是账号付费额度，也不是全部设备共享的供应商限额。 |
| `ASHARE_RADAR_FUYAO_TIMEOUT_SECONDS` | `20.0` | - | 单次 REST 尝试超时，范围1–120秒；Parquet 文件下载使用独立有界下载流程。 |
| `ASHARE_RADAR_FUYAO_DOWNLOAD_HOSTS` | empty | - | 逗号分隔的精确公网对象存储域名，禁止URL、路径、端口、IP及通配符；空值拒绝历史下载。专用本地启动入口已验证的默认域名为 `o.thsi.cn`。 |
| `ASHARE_RADAR_FUTU_ENABLED` | `0` | `FUTU_ENABLED` | Requires local Futu OpenD. |
| `ASHARE_RADAR_FUTU_HOST` | `127.0.0.1` | `FUTU_HOST` | Futu OpenD host. |
| `ASHARE_RADAR_FUTU_PORT` | `11111` | `FUTU_PORT` | Futu OpenD port. |
| `ASHARE_RADAR_DEMO_PROVIDER_ENABLED` | `0` | `DEMO_PROVIDER_ENABLED` | Demo data must stay disabled for real research. |
| `ASHARE_RADAR_CORS_ALLOW_ORIGINS` | local 8010 origins | `CORS_ALLOW_ORIGINS` | Comma-separated origin allowlist. Every API request, including reads, must have an allowed Host-derived origin. A supplied Origin must be allowed; otherwise a supplied Referer is checked. With only Sec-Fetch-Site, cross-site requests are rejected. Requests without browser-origin headers still require an allowed Host. |
| `ASHARE_RADAR_CACHE_PATH` | project `data/ashare_radar.sqlite3` | `CACHE_PATH` | Absolute path or project-root-relative SQLite path. |
| `ASHARE_RADAR_LEGACY_AUDIT_TIMEZONE` | `Asia/Shanghai` | - | IANA timezone used only to interpret legacy naive audit timestamps during the first UTC migration and user-data import. Set it before first startup when an old database was written in another host timezone; new audit timestamps are fixed-width UTC `Z`. |
| `ASHARE_RADAR_MINUTE_KLINE_CACHE_SECONDS` | `60` | `MINUTE_KLINE_CACHE_SECONDS` | Minute K-line cache TTL. |
| `ASHARE_RADAR_STOCK_POOL_AUTHORITATIVE_MIN_COUNT` | `1000` | `STOCK_POOL_AUTHORITATIVE_MIN_COUNT` | Fresh cache count needed to confirm an empty stock search. |
| `ASHARE_RADAR_STOCK_POOL_PROVIDER_TIMEOUT_SECONDS` | `60` | - | Timeout for one full stock-pool provider call; range 1-300 seconds. Kept separate from short quote/K-line calls because exchange-list fallbacks may require several pages. |
| `ASHARE_RADAR_TENCENT_KLINE_MAX_IN_FLIGHT` | `5` | - | Tencent daily-K async request capacity; range 1-16. This is isolated from blocking backup providers, which retain the generic two-call admission limit. Keep it no higher than the scan K-line concurrency unless a measured provider test justifies otherwise. |
| `ASHARE_RADAR_STOCK_CONCEPT_CACHE_SECONDS` | `21600` | `STOCK_CONCEPT_CACHE_SECONDS` | Stock concept cache TTL. |
| `ASHARE_RADAR_PROVIDER_FAILURE_COOLDOWN_SECONDS` | `90` | `PROVIDER_FAILURE_COOLDOWN_SECONDS` | Provider retry cooldown after failures. |
| `ASHARE_RADAR_QUOTE_PROVIDER_PRIORITY` | `tencent,futu,akshare` | - | 报价来源优先级；仅使用可用且支持该能力的来源。 |
| `ASHARE_RADAR_KLINE_PROVIDER_PRIORITY` | `tencent,akshare,tushare,baostock` | - | 完整日线来源优先级。 |
| `ASHARE_RADAR_MINUTE_PROVIDER_PRIORITY` | `futu,akshare` | - | 分钟线来源优先级。 |
| `ASHARE_RADAR_STOCK_PROVIDER_PRIORITY` | `akshare,tushare,baostock,local` | - | 股票池来源优先级。 |
| `ASHARE_RADAR_PLATE_PROVIDER_PRIORITY` | `akshare,local` | - | 板块来源优先级。 |
| `ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_ENABLED` | `1` | - | 扫描前验证提供者能力与准备状态。 |
| `ASHARE_RADAR_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS` | `30` | - | preflight 总等待预算，范围 0.1–300 秒。 |
| `ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS` | `600,1800,3600` | - | 自动重试延迟序列，每项 1–86400 秒。 |
| `ASHARE_RADAR_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS` | `3` | - | 自动重试上限，范围 0–10。 |
| `ASHARE_RADAR_MARKET_SCAN_AUTO_ENABLED` | `0` | - | Enable the after-close full-market scan. |
| `ASHARE_RADAR_MARKET_SCAN_SCHEDULE_HOUR` | `16` | - | Automatic-scan local hour; the 15:15 daily publication floor still applies. |
| `ASHARE_RADAR_MARKET_SCAN_SCHEDULE_MINUTE` | `30` | - | Automatic-scan local minute. |
| `ASHARE_RADAR_MARKET_SCAN_BATCH_SIZE` | `50` | - | Symbols per persisted scan batch; range 1-500. |
| `ASHARE_RADAR_MARKET_SCAN_CONCURRENCY` | `5` | - | Maximum concurrent per-symbol K-line jobs; range 1-32. |
| `ASHARE_RADAR_MARKET_SCAN_KLINE_LIMIT` | `260` | - | Requested completed `qfq` daily rows per symbol; range 61-1000. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_HISTORY_ROWS` | `61` | - | Minimum complete daily rows required for ranking; range 61-260 and no greater than the K-line limit. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_DATA_QUALITY_SCORE` | `50` | - | Results below this 0-100 quality floor are skipped. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_UNIVERSE_COUNT` | `4000` | - | Reject a purported full-market pool below this total count. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_SH_COUNT` | `1800` | - | Reject a scan pool with fewer Shanghai A shares. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_SZ_COUNT` | `2500` | - | Reject a scan pool with fewer Shenzhen A shares. |
| `ASHARE_RADAR_MARKET_SCAN_MIN_BJ_COUNT` | `200` | - | Reject a scan pool with fewer Beijing A shares. |
| `ASHARE_RADAR_MARKET_SCAN_SYMBOL_TIMEOUT_SECONDS` | `30` | - | Timeout for one symbol's K-line attempt; range 0.1-300 seconds. |
| `ASHARE_RADAR_MARKET_SCAN_QUOTE_BATCH_TIMEOUT_SECONDS` | `60` | - | Outer timeout for one quote batch; range 0.1-600 seconds. |
| `ASHARE_RADAR_MARKET_SCAN_RETRY_ATTEMPTS` | `2` | - | K-line attempts per symbol; range 1-5. |
| `ASHARE_RADAR_MARKET_SCAN_RETRY_BACKOFF_SECONDS` | `1` | - | Linear delay multiplier between K-line attempts; range 0-30 seconds. |
| `ASHARE_RADAR_MARKET_SCAN_BATCH_RETRY_ATTEMPTS` | `3` | - | Attempts for the pending subset of a batch after a system-wide quote/daily-K chain outage; range 1-5 and independent of per-symbol K-line retries. |
| `ASHARE_RADAR_MARKET_SCAN_PROVIDER_WAIT_BUDGET_SECONDS` | `120` | - | Cumulative actual provider-recovery sleep budget across one scan's pending work; range 0-600 seconds. Exhaustion fails the run while affected rows remain pending; `0` disables recovery sleeps. |
| `ASHARE_RADAR_MARKET_SCAN_NEW_STOCK_DAYS` | `120` | - | Calendar-day window used only for the new-stock tag; range 1-730. |
| `ASHARE_RADAR_MARKET_SCAN_OFFICIAL_EXECUTION_REGISTRY_PATH` | `data/research/market_scan_official_execution/source-registry.json` | - | Operator-reviewed licensed official-source registry. Its contents alone never grant authority. |
| `ASHARE_RADAR_MARKET_SCAN_OFFICIAL_EXECUTION_REGISTRY_DIGEST` | unset | - | Out-of-band pinned lowercase SHA-256 of the official-source registry. When unset, formal execution evidence stays explicitly unconfigured and public vendor data is never promoted. |
| `ASHARE_RADAR_MARKET_SCAN_OFFICIAL_EXECUTION_RAW_ROOT` | `data/research/market_scan_official_execution_raw` | - | Root containing immutable raw licensed delivery files referenced by official session receipts. Every file is read without symlink traversal and checked for exact size and SHA-256. |
| `ASHARE_RADAR_MARKET_SCAN_OFFICIAL_EXECUTION_SESSION_DIRECTORY` | `data/research/market_scan_official_execution/sessions` | - | Content-addressed normalized official execution-session artifacts. Loading also replays the pinned registration, license interval, source URI, raw-file bytes, unadjusted OHLCV/amount, effective trading rules, corporate actions, and complete decision coverage. |
| `ASHARE_RADAR_MARKET_SCAN_JOINT_EXECUTION_AUTHORIZATION_PATH` | `data/research/market_scan_joint_execution/authorization/active.json` | - | Operator-installed exact probability-filter authorization artifact. The file is non-authorizing until its digest is independently pinned and strict replay against the selected H5 study succeeds. |
| `ASHARE_RADAR_MARKET_SCAN_JOINT_EXECUTION_AUTHORIZATION_DIGEST` | unset | - | Out-of-band lowercase SHA-256 pin for the formal H5 filter authorization. Unset, stale, mismatched or superseded evidence keeps probability filtering disabled. |
| `ASHARE_RADAR_MARKET_SCAN_PROBABILITY_RANKING_CONTROL_PATH` | `data/research/market_scan_joint_execution/ranking-control/active.json` | - | Human-authored exact `promote` or `rollback` control for `full-market-score-v6`; automation must not create or silently replace it. |
| `ASHARE_RADAR_MARKET_SCAN_PROBABILITY_RANKING_CONTROL_DIGEST` | unset | - | Out-of-band lowercase SHA-256 pin for the ranking control. The current audit-only inference contract blocks new v6 promotions even with a pinned control; verified rollback is persisted independently of Shadow qualification. Old publications remain auditable, but an old audit token cannot recreate a missing SQLite mirror or authorize a new publication. |
| `ASHARE_RADAR_SCHEDULER_ENABLED` | `1` | `SCHEDULER_ENABLED` | Local refresh scheduler switch. |
| `ASHARE_RADAR_SCHEDULER_QUOTE_INTERVAL_SECONDS` | `30` | `SCHEDULER_QUOTE_INTERVAL_SECONDS` | Quote refresh interval. |
| `ASHARE_RADAR_SCHEDULER_KLINE_INTERVAL_SECONDS` | `900` | `SCHEDULER_KLINE_INTERVAL_SECONDS` | K-line refresh interval. |
| `ASHARE_RADAR_SCHEDULER_PLATE_INTERVAL_SECONDS` | `300` | `SCHEDULER_PLATE_INTERVAL_SECONDS` | Plate refresh interval. |
| `ASHARE_RADAR_SCHEDULER_HEALTH_INTERVAL_SECONDS` | `45` | `SCHEDULER_HEALTH_INTERVAL_SECONDS` | Requested data-health interval; automatic scheduling enforces at least 300 seconds in the working window and 900 seconds otherwise. |
| `ASHARE_RADAR_SCHEDULER_KLINE_SYMBOLS_LIMIT` | `5` | `SCHEDULER_KLINE_SYMBOLS_LIMIT` | Per-cycle K-line symbol cap. |
| `ASHARE_RADAR_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | `5` | `SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS` | Bounded scheduler stop wait; unified runtime leadership is not released while unfinished service work is still shutting down. |
| `ASHARE_RADAR_MAX_QUOTE_HISTORY_ROWS` | `120` | `MAX_QUOTE_HISTORY_ROWS` | Per-symbol daily quote-history cap; minimum `120`, matching the analysis window. |
| `ASHARE_RADAR_MAX_DAILY_KLINE_ROWS` | `260` | `MAX_DAILY_KLINE_ROWS` | Per-symbol and adjustment-mode daily K-line cap; must cover `ASHARE_RADAR_MARKET_SCAN_KLINE_LIMIT`. |
| `ASHARE_RADAR_MAX_MINUTE_KLINE_ROWS` | `20000` | `MAX_MINUTE_KLINE_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_STOCK_CONCEPT_ROWS` | `20000` | `MAX_STOCK_CONCEPT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_TASK_RUN_ROWS` | `2000` | `MAX_TASK_RUN_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_RELIABILITY_BUCKET_ROWS` | `10000` | - | Global retention cap for low-cardinality UTC-hour reliability aggregates; minimum `1`. |
| `ASHARE_RADAR_MAX_MARKET_SCAN_RUNS` | `30` | - | Newest-run keep-window target. Every unreferenced sealed graph outside the window is eligible and deleted as a verified result/run graph; active work and genuine database/file references may keep the physical count above target, and every noncandidate/directly protected retry root pins all reachable candidate ancestors. A run's outward `task_run_id` is not a pin. |
| `ASHARE_RADAR_MAX_MONITOR_EVENT_ROWS` | `3000` | `MAX_MONITOR_EVENT_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_CACHE_EVENT_ROWS` | `5000` | `MAX_CACHE_EVENT_ROWS` | Runtime retention cap for cache/provider events. |
| `ASHARE_RADAR_MAX_ALERT_EVENT_ROWS` | `5000` | `MAX_ALERT_EVENT_ROWS` | Runtime retention cap for alert events. |
| `ASHARE_RADAR_MAX_ADVICE_HISTORY_ROWS` | `20000` | `MAX_ADVICE_HISTORY_ROWS` | Runtime retention cap. |
| `ASHARE_RADAR_MAX_DATABASE_SIZE_MB` | `2048` | - | Local SQLite and managed-backup capacity budget in MiB; sized for one full-market daily cache plus two backups, minimum `16`. Diagnostics warn at 80%. |
| `ASHARE_RADAR_RUNTIME_MAINTENANCE_INTERVAL_SECONDS` | `3600` | - | Minimum interval between automatic regenerable-data maintenance passes; range 60-604800 seconds. |
| `ASHARE_RADAR_MAX_RUNTIME_BACKUPS` | `2` | - | Managed runtime backup bundles retained per database; range 2-100. The default preserves two recovery points without multiplying the full-market cache footprint. API/CLI backup and restore operations pass this limit explicitly. |
| `ASHARE_RADAR_ADVICE_HISTORY_DEDUPE_SECONDS` | `180` | `ADVICE_HISTORY_DEDUPE_SECONDS` | Advice-history de-duplication window. |
| `ASHARE_RADAR_QUOTE_STALE_WARNING_SECONDS` | `900` | `QUOTE_STALE_WARNING_SECONDS` | Quote freshness warning threshold. |
| `ASHARE_RADAR_QUOTE_CONSISTENCY_WARNING_PCT` | `1.0` | `QUOTE_CONSISTENCY_WARNING_PCT` | Multi-source price-difference warning threshold. |
| `ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH` | `0` | `TRADE_CALENDAR_AUTO_FETCH` | Non-blocking single-flight background refresh when runtime is missing, invalid, stale for the current date, or cannot cover a target. The triggering call uses the current bundle/closed decision; later calls see a successful atomic runtime update. |

Missing optional values use documented defaults. Present but malformed boolean, numeric, path, or LLM endpoint values fail configuration at startup instead of silently changing behavior. Restart after changing process variables or the allowlisted LLM assignments in `$HOME/.zshrc`.

### Provider Canary Variables

`tools/provider_canary.py` owns three tool-only environment variables. They are intentionally not `Settings` fields and are outside the application's configuration-document coverage contract:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASHARE_RADAR_CANARY_SH_SYMBOL` | `600519.SH` | Representative Shanghai symbol. |
| `ASHARE_RADAR_CANARY_SZ_SYMBOL` | `000001.SZ` | Representative Shenzhen symbol. |
| `ASHARE_RADAR_CANARY_BJ_SYMBOL` | `920066.BJ` | Representative Beijing symbol. |

Each value is normalized and must belong to the named market. Equivalent `--sh-symbol`, `--sz-symbol`, and `--bj-symbol` flags override the defaults. `--request-timeout` controls each representative quote/K-line probe and defaults to `provider_call_timeout_seconds`; `--stock-pool-timeout` independently controls the larger stock-pool refresh and defaults to `stock_pool_provider_timeout_seconds`. `--overall-timeout` remains the final deadline for the concurrent set.

The CLI creates and removes a temporary SQLite database, disables the scheduler for that isolated DataHub, and concurrently checks:

- one direct non-cached quote for each SH/SZ/BJ representative;
- one direct five-row completed daily-K request per representative; the response must contain exactly five ordered usable rows and pass finite OHLCV, cache/fallback, future-date, and staleness validation;
- a refreshed stock pool with valid unique identity rows and at least one SH, SZ, and BJ member.

Output is one sanitized JSON object. Exit `0` means every market and the stock-pool contract were available, `2` means at least one market remained available but the full contract was partial, and `1` means no market was available or provider cleanup failed. This is a live-provider diagnostic, not a required CI test:

```bash
$PYTHON tools/provider_canary.py
```

### LLM Remote Data Boundary

When LLM enhancement is enabled, only supported research questions with the required evidence may send an OpenAI-compatible chat-completion request to the configured remote endpoint. Chat messages contain the current question and topic; symbol, stock name, and quote time; the deterministic rule answer; authoritative conclusion, confidence, support, resistance, actions, and invalidations; and bounded evidence units with their IDs, text, source, and observation time. Units contain at most six rule-evidence items, usable current price/change facts, the answer-reliability score with its meaning, and at most four data-quality notes. The request also binds the protocol version and a digest of the current answer context. Local watchlists, stock notes, alert rules/events, advice history, provider credentials, full workbench payloads, and other local collections are not sent. Unknown questions and missing-evidence answers remain local.

The transport also sends the configured model name, generation parameters, and `ASHARE_RADAR_LLM_API_KEY` as authentication to that endpoint. The API key is not inserted into chat messages, but the remote service necessarily receives it as a request credential. Use only an endpoint whose data-handling policy is acceptable, or set `ASHARE_RADAR_LLM_ENABLED=0` to keep Q&A rule-only.

The `stock-qa-evidence-selection.v1` response must copy all authoritative fields, the protocol version, and the context digest exactly. The model may only select and order one to four unique IDs from the supplied evidence units. Free-form explanations, extra fields, unknown IDs, and mismatched contexts are rejected. The server renders selected evidence verbatim and retains the deterministic answer, actions, invalidations, and data limitations; this prevents new model-authored facts from entering the answer but does not establish that upstream evidence is true.

If and only if the first output fails local validation, the app may send one format-correction request with the same bounded context, asking the model to copy the bound fields and choose existing IDs; it does not resend the previous raw model output. One outer timeout covers the first request, local validation, and correction together, so correction receives only the remaining `ASHARE_RADAR_LLM_TIMEOUT_SECONDS` budget rather than a new full timeout. The SDK's automatic retries are disabled. A request error, total-budget expiry, or second validation failure returns the deterministic rule answer without another remote attempt.


## 6. 验证与依赖维护

完整本地交付命令见[测试计划](TEST_PLAN.md)。以下安全与供应链检查需要相应工具/网络，应明确记录实际执行结果。

The Security workflow is an additional required gate. Its local-equivalent dependency and reproducible-SBOM checks are:

```bash
$PYTHON -m pip install --only-binary=:all: --require-hashes -r requirements-security-lock.txt
$PYTHON -m pip check
npm ci --ignore-scripts
$PYTHON -m pip_audit --require-hashes --disable-pip --strict --progress-spinner off --requirement requirements-lock.txt
$PYTHON -m pip_audit --require-hashes --disable-pip --strict --progress-spinner off --requirement requirements-dev-lock.txt
$PYTHON -m pip_audit --require-hashes --disable-pip --strict --progress-spinner off --requirement requirements-security-lock.txt
npm audit --audit-level=high
first_dir="$(mktemp -d)"
second_dir="$(mktemp -d)"
$PYTHON tools/generate_sbom.py --output-dir "$first_dir"
$PYTHON tools/generate_sbom.py --output-dir "$second_dir"
diff -ru "$first_dir" "$second_dir"
rm -rf "$first_dir" "$second_dir"
```

CI additionally installs a checksum-verified Gitleaks binary, scans the current source and complete Git history with `--redact=100`, and uploads the normalized CycloneDX artifacts. Do not replace that history scan with a latest-tree grep. `tests/test_supply_chain.py` guards SHA-pinned actions, disabled checkout credential persistence, both Python lock audits, npm audit, redacted current/history scans, Dependabot ecosystems, and two-run SBOM comparison.

### 更新依赖与生成文档

Keep direct dependencies in the appropriate input file. Runtime libraries belong in `requirements.txt`; test, lint, type, and lock-compilation tools belong in `requirements-dev.txt`; only vulnerability-audit and SBOM generators belong in `requirements-security.txt`. These inputs are for lock generation: reproducible installs use `--require-hashes` and the generated locks. A runtime-input change requires rebuilding the runtime and development locks because `requirements-dev.txt` includes `requirements.txt`; a development-only or security-tool change requires rebuilding only its matching lock. Do not edit generated locks by hand. Verify the locks in clean Python 3.12 environments:

```bash
$PYTHON -m piptools compile --generate-hashes \
  --output-file=requirements-lock.txt requirements.txt
$PYTHON -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements-dev-lock.txt requirements-dev.txt
$PYTHON -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements-security-lock.txt requirements-security.txt
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
$PYTHON -m pip install --only-binary=:all: --require-hashes -r requirements-security-lock.txt
$PYTHON -m pip check
```

After any dependency lock changes, audit all three Python locks, run `npm audit`, and regenerate both SBOMs. `tools/generate_sbom.py` consumes `requirements-lock.txt` and `package-lock.json`, validates CycloneDX JSON, removes volatile serial/timestamp fields, and imposes deterministic ordering. Each output (`python.cdx.json` and `npm.cdx.json`) is replaced atomically; the pair is not one transaction, so regenerate both after a failed run. Private staging files are cleaned on write failure or interruption. Each external generator has a 120-second timeout, after which its directly owned subprocess is terminated and reaped; filesystem errors produce a redacted message and nonzero exit status. The Security workflow generates twice and compares bytes before artifact upload. Its separate tool lock prevents an audit-only Linux runner from building optional provider source distributions. A reproducible SBOM is an inventory aid; it is not a signed release or provenance attestation.

Dependabot runs weekly for pip, npm, and GitHub Actions. Review generated changes through the same tests instead of merging solely because a version is newer. Keep every `uses:` reference pinned to a reviewed 40-character commit SHA and preserve `persist-credentials: false` for checkout.

Regenerate inventory files only when accepting their source changes. CI and review should use the non-mutating checks:

```bash
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
```

诊断刷新失败时，页面保留上次成功结果并明确标记其历史性质；下一次成功刷新会移除警告。笔记或策略若提示“已保存，但刷新失败／同步未完成”，使用只读刷新恢复视图，不重复创建。没有收到有效写入回执的网络超时仍需核实结果，不能视为未写入。

## 工作区与操作路径

主导航分为“个股研究、全市场选股、复盘模拟、自选监控、系统维护”。个股笔记和预警在“个股研究 → 笔记预警”，当前股票标识始终可见；数据源、后台任务在“系统维护 → 运行状态”，导入导出和清理在“系统维护 → 数据管理”。来回切换或重载会恢复各区上次选择的子页；已有浏览器偏好自动迁移。

自选行的普通查看进入图表，保持未读；“查看 N 条新变化”进入该股建议时间线。成功显示后才同步已读水位；行情、记录或同步失败时保留未读并提示重试。队列筛选不会代替查看动作。

自选写入的内部回读或返回模型构建失败时，本次修改和计数调整整体回滚；网络断开或响应丢失仍须先核实实际状态。新增提醒和笔记等待保存时可以更改下一份草稿的类型，旧成功回执会保留这份同值新类型草稿；再次明确提交后，未再改动的内容才会清空。

策略实验室修改输入后先完成编译；当前草案与已保存版本不一致时，须确认保存后才能执行、回放或创建新定时任务。若改回已保存定义，重新编译一致后可继续使用该版本，无需再次保存；状态行明确显示已保存版本及草案状态。保存请求超时或回执不匹配时，当前页面会标记“保存结果待核对”，先刷新列表并显式载入具体记录；列表为空不能证明首次写入未提交。该标记仅在当前页面会话有效，整页刷新后仍应先核对服务端记录，不能把刷新当作未提交证明。已保存定时任务仍固定原版本，修改编辑器不会改写已有任务。生成的纸面委托草案可查看股票、股数、金额、成本和退出约束，不会自动加入“复盘模拟”的账户。

模拟账户加入策略或生成运行后初始资金冻结，仍可用“保存默认成本”调整后续默认配置；并发运行先保存成功时，资金修改会拒绝并要求按当前账户状态操作。历史运行保持不可变。已进入运行的策略不能删除，无持仓并不代表可以删除其历史身份。

## 筛选方案和变化记录

在全市场选股中，选择已保存方案后可应用原定义。“另存方案”把当前编辑条件创建为新方案，名称须与已有方案区分；“更多管理 → 更新所选方案”显式更新当前选择。不能完整表达的兼容方案可按原定义应用，也可导出保留原定义；要另存当前表单条件，须先取消兼容方案的选择，避免误把不完整条件当作副本。方案列表可按需翻页，跨页选择的方案身份单独显示；列表失败时先刷新核对，保存回执与列表回读状态分别看待。提交结果未知时，当前页面会阻止继续保存并要求选择具体已保存方案核对；此标记仅保护当前页面会话，整页刷新不能证明之前的提交未生效。

“更多管理 → 记录变化提醒”比较所选方案的同模式、同股票池、同规则相邻发布批次，显示新进入、退出和当前不可排名的证券。展开“查看已记录变化”可翻页、打开单条明细及切换类别。查看历史是只读操作，来源批次和方案修订保持记录时的值；旧记录没有保存完整条件正文，不用当前方案名称或条件补造历史。“当前不可排名”表示本次证据不足，不能视为退出。

公开执行事实采集见[来源与操作说明](PUBLIC_EXECUTION_SOURCES.md)。`tools/collect_public_execution_facts.py collect`是独立联网CLI，不使用本地付费密钥，默认最近完成交易日且最早17:45开始。每日最多12次逻辑请求（失败与中断计入），同来源/交易日当天失败不重试；每路交易所逻辑请求最多20次HTTP请求。`inspect/compare`和前瞻`evidence --public-root`只读归档。数据保存在忽略目录`data/research/public_execution`，与策略计划一起备份；不能清除失败记录以绕过预算。
