# 维护指南

## 1. 修改入口

| 改动 | 首先查看 |
| --- | --- |
| 路由、状态码、响应缓存 | `app/api/routes/`、`app/api/errors.py`、`app/api/security.py` |
| 应用生命周期与实例共享 | `app/main.py`、`app/api/container.py`、`runtime_coordinator.py` |
| 配置 | `config_settings.py`、`config_validation.py`、`config_shell.py`及运行手册变量表 |
| 行情与缓存 | `datahub_*.py`、`provider_registry.py`、`repositories/market_*.py` |
| 调度 | `scheduler_service.py`、`scheduler_lifecycle.py`、`scheduler_execution.py`及任务模块 |
| 个股分析 | `analysis_*.py`、`financial_*.py`、`valuation_analysis.py`、`stock_event_*.py` |
| 全市场流程 | `market_scan_manager.py`及 lifecycle/execution/completion/query 领域模块 |
| 扫描规则 | `market_scan_scoring.py`、`market_scan_score_*`和 replay；修改前明确是否改变规格身份 |
| 研究产物 | `app/artifacts/`、领域 research/store/validation 模块及 `tools/`真实 CLI |
| 数据库 | `app/db/`、`app/repositories/`；迁移与触发器必须完整事务验证 |
| 页面装配 | `static/app.js`、`static/js/`中的控制器、事件绑定和请求模块 |
| 样式 | HTML 引用的 `static/css/`，共用规则放已有域文件，不新增空转发样式表 |

完整模块与函数位置由[函数索引](FUNCTION_INVENTORY.md)生成，不在指南中重复维护几百个签名。

## 2. 实施流程

1. 从实际用户入口确认触发、输入和错误表现；先读相关领域契约及行为测试。
2. 明确改变的是产品语义、数据格式还是内部结构。结构调整不得改变评分摘要、来源资格、事务及取消语义。
3. 修改唯一所属实现，并迁移所有消费者。测试使用同一公开入口或领域实现，不新增只为保住旧测试的代理函数。
4. 执行相关回归；检查动态 import、字符串路径、HTML 引用、CLI 和持久化数据。单纯 `rg` 零命中不能证明装饰器/回调无用。
5. 删除确认失效的实现与文档，同步模型导入、类型清单、测试目录和自动索引。
6. 冻结源码后执行[交付检查](TEST_PLAN.md)，记录当前验收结果；不把每轮计划或终端日志永久堆进工程文档。

删除用户数据是另一类操作。清理源码不代表可以清空数据库、研究产物、账户配置或本机记忆。变更前保留可恢复工作区快照或版本控制记录；仓库里不保留 `old`/`backup` 代码副本。

## 3. 代码约束

- API 薄层、领域服务单一职责、仓储拥有事务；下层不得依赖上层。跨域明确导入模型，避免聚合 schemas 或私有名称转发。
- Python 生产函数不超过 60 行、12 个 AST 分支点。拆分应产生清楚的阶段或领域边界，不能只增加一层包装。
- 前端生产 JavaScript 函数须严格少于 60 行、20 个分支点；入口和 E2E 文件另受逐文件增长预算约束，不能套用 Python 的包含等号门槛。
- 显式 Mypy 文件列表与测试共同约束类型覆盖。删除模块时删除对应清单项；不通过忽略错误或放宽类型门槛掩盖失败。
- Python 的 `app/` 与 `tools/` 纳入分支和行的综合覆盖率统计，门槛为 90%，配置见 `pyproject.toml`。JavaScript 使用语法、Node 行为及 Playwright 回归验证，未纳入这项 Python 覆盖率数字。静态结构测试不能替代失败、竞态和数据边界测试。
- 公共异常必须脱敏；取消应传播或由生命周期明确处理。禁止宽泛捕获后返回假成功。
- 异步请求、线程池和后台任务必须有所有者、准入预算和终止路径。已发出的用户写操作独立于页面读请求。
- 关闭测试须覆盖连续取消及子任务首次调度前取消；等待已完成任务时不能依赖尚未运行的回调退出无让出点循环。事务测试覆盖内部回读、模型构造和真实提交失败，不能只检查写 SQL 本身成功。新增异常/取消处理时先运行 `tests/test_exception_safety.py`，仅为已验证的所有权边界登记准确的传播方式和理由。
- 列表中的启用状态不能替代取得执行权时的事务内准入。前端取消须同时检查结果失效与在途读取释放；表单可用性按当前领域状态恢复，不能由通用按钮包装器无条件覆盖。包含输入法的搜索控件应覆盖组合输入和正常键盘选择。
- 文件写入有界、原子且校验摘要。SQLite 外键、不可变触发器和修订号检查是当前数据契约，不是可随意删除的旧实现。

## 4. 版本与兼容边界

内部函数名、纯重导出模块、只由测试调用的快捷方法没有持续兼容义务；消费者统一后直接删除。仍有真实消费者的旧数据验证则必须保留或设计明确迁移。例如旧扫描规格摘要、数据库审计时间和复盘记录来源检查，决定现存数据能否安全读取；移除它们会改变结果含义。

只允许从受检运行目录读取研究产物。示例放文档，夹具放 `tests/fixtures/`，运行数据放 `data/`；三者不得互相充当隐含回退。离线实验不会自动取得正式发布或排名权限。

## 5. 文档与工具

README 负责上手导航，REQUIREMENTS 描述行为，DESIGN 描述架构，OPERATIONS 描述操作，RESEARCH 描述离线研究，TEST_PLAN 描述验证。特定 Choice 与个人实验说明保留独立入口。当前 Goal 完成后更新结果，不继续保存过期阶段计划作为现行契约。

文档与代码冲突时，先核对产品约束、公开入口和真实消费者：正确行为被旧说明误述则改文档；实现违反数据完整性或用户状态约束则修代码并回归，不能把缺陷写成正常行为。稳定的设计说明和研究依据应指向所属模块或回归测试，轮换的当前Goal只记录正在执行的工作。临时目录中的审计材料属于对应本机会话，不是仓库运行或新检出环境复验的前置条件。

新增/删除测试时更新 TEST_PLAN 的测试模块索引；API 或函数变化后再生成参考文档：

```bash
.venv/bin/python tools/api_inventory.py
.venv/bin/python tools/architecture_inventory.py
```

CI 使用 `--check`，不自动覆写待审查内容。Markdown 本地链接与内部模块导入由仓库一致性检查验证。

## 6. 依赖与供应链

直接依赖分别维护在 `requirements.txt`、`requirements-dev.txt`、`requirements-security.txt`。运行依赖改变时重编 runtime 和 dev 两份锁；安全工具锁独立。使用 Python 3.12 的 pip-tools 生成带 hashes 的锁，禁止手改生成文件。安装使用 `--require-hashes`；更新后运行 `pip check`、锁审计、npm audit、完整测试和可复现 SBOM 检查。

CI action 固定到审查过的完整 commit SHA；checkout 不持久化凭证。秘密扫描覆盖当前文件与完整 Git 历史，输出必须脱敏。在线漏洞库和真实 provider canary 属于明确的外部检查，不伪称离线单元测试已覆盖。

## 7. 有意保留的边界

DataHub 与 SQLiteCache 的当前组合关系、少数领域内部私有依赖仍受架构基线约束。后续调整应一次迁移真实调用并验证共享身份，不能只增包装或为了数字降低覆盖。外部 SDK 不可强制终止的线程、样本外经济有效性和外部正式源授权仍各有独立限制；工程重构不改变这些事实。
