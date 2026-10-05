# AShareRadar

本地运行的 A 股研究工作台：聚合行情和日线，解释规则评分，保存研究笔记与复盘证据，并提供全市场扫描、条件筛选和离线概率研究。后端为 FastAPI + SQLite，前端为原生 JavaScript 模块。模拟交易只用于研究，项目没有券商下单入口。

## 快速开始

在项目根目录执行，要求 Python 3.12：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-lock.txt
PYTHONNOUSERSITE=1 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --timeout-graceful-shutdown 5
```

浏览器打开 http://127.0.0.1:8010 。开发检查另需 Node.js 22 或 24、npm 10 或 11。数据默认写入 `data/ashare_radar.sqlite3`，首次启动可能需要建立缓存。数据源不可用、行情过期或证据不足时，页面应明确显示不可用或降级原因。

已在本机配置扶摇密钥文件时，使用 `PYTHONNOUSERSITE=1 .venv/bin/python tools/run_local.py` 启动。该入口读取被 Git 忽略的本地配置，提供财报、估值、板块、情绪和批量历史研究功能，同步任务支持进度查询、主动停止和未完成项补做；配置与调用上限见[运行手册](docs/OPERATIONS.md#扶摇研究数据)。

## 当前功能

| 入口 / 模块 | 用途 |
| --- | --- |
| 个股研究 | 行情、技术与基本面解释、日线/分钟图、研究问答、笔记预警与通知原记录导航 |
| 自选监控 | 自选分组、队列检索与到期／未读筛选、显式查看新变化、报价观察 |
| 全市场选股 | 冻结扫描、规则评分、条件筛选、单条件影响与空榜解释、覆盖率与缺失解释、方案另存与分页、冻结候选对比、变化记录明细、Excel 导出 |
| 策略实验室 | 草案与版本校验、策略分页、冻结执行回看、定时任务查看与启停、纸面委托与证据评估 |
| 复盘模拟 | 版本化计划、全局计划精确定位、到期队列筛选与分页、不可变评估记录、组合模拟 |
| 系统维护 | 运行状态与后台任务、数据管理、导入导出和备份 |
| 离线研究 | 历史样本、概率评估、未来区间、执行成本、资金约束和敏感性分析 |

[评分跟踪报告](docs/SCORE_TRACKING.md)可使用本地冻结扫描检查高低分股票的后续表现，分别展示同日配对差、待到期、缺失与样本不足；生成可直接打开的 HTML，不请求外部行情。

[量化因子与策略](docs/QUANT_FACTORS_AND_STRATEGIES.md)列出当前因子、8个可载入研究模板及待补数据方向，包括跳过近期动量和低波趋势；说明每项证据限制和后续迭代顺序。

[策略模板对照报告](docs/STRATEGY_TEMPLATE_TRACKING.md)将有界中期趋势、跳过近期动量和低波趋势应用于同一冻结扫描。v2分别展示历史毛收益、次日入场的独立批次净收益，以及同评分合同内的诊断领先和统计优势；当前回溯报告不授予采用资格，不覆盖用户策略。它只读本地数据，不执行交易：

```bash
.venv/bin/python tools/track_strategy_templates.py \
  --database data/ashare_radar.sqlite3 \
  --output-directory data/research/strategy_template_tracking \
  --non-overlapping-signals
```

上例按每个合同首日固定每`H+1`个交易日的锚点，默认`H=10`。净收益比较另需文档中的官方执行证据四件套；没有这些材料仍能生成缺口报告。至少20个完整锚点及多重检验通过也只是回溯证据，不能证明未来胜出。2026-09-19本地检查仍缺合格官方执行证据，部分历史批次封印失效或含已保存但状态为`missing`的输入，暂不能证明哪套策略收益更好，保留当前策略。

[三策略前瞻采集](docs/STRATEGY_PROSPECTIVE_COLLECTION.md)提供`init/status/capture/evidence/evaluate`离线流程：先冻结未来日期、模板和源码，按时保留完整扫描，再抽取已有扶摇研究资料并追加独立批次结果。缺失与迟到不补成按时样本；当前仍需正式执行证据、成熟样本及后续账户验证，不自动晋级策略。

[免费公开执行事实](docs/PUBLIC_EXECUTION_SOURCES.md)接入BaoStock按日批量行情及沪深交易所停复牌记录，保存原始响应、实际采集时间和摘要。日内更新后采集、按日复用、限制预算与超时，供前瞻证据报告逐只核验；公开资料仍不等于可成交或正式收益证据。

正式评分、展示百分位和研究概率是不同指标。研究产物不会自动取得排序、筛选或发布权限；正式来源、完整性、样本外评估及独立授权必须分别通过校验。

浏览器重新打开时会从研究历史恢复上次成功加载的股票；清空历史后恢复默认股票。系统维护和自选监控按当前页面读取数据，进入个股研究或复盘时再加载股票分析。全市场的高级概率研究默认收起，展开后仍保留原有研究功能。

## 开发

```bash
.venv/bin/python -m pip install --require-hashes -r requirements-dev-lock.txt
npm ci
npm run check
.venv/bin/python -m ruff check app tests tools
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q -p no:cacheprovider --cov=app --cov=tools
```

完整交付还需生成文档漂移检查、覆盖率门槛和浏览器回归，见[测试计划](docs/TEST_PLAN.md)。单元测试使用临时数据库和替代数据源，不依赖真实账户或在线行情。

`npm run test:e2e` 会核对完整测试清单，并把 WebKit 分为有界的独立进程批次；所有项目和断言保持不变，批次报告合并后交付。此方式规避当前测试环境中连续创建浏览器上下文导致的导航故障，不自动重试失败或延长超时。

## 文档入口

- [功能要求](docs/REQUIREMENTS.md)：当前行为与验收标准。
- [架构设计](docs/DESIGN.md)：依赖方向、生命周期、数据和前端边界。
- [维护指南](docs/MAINTENANCE.md)：修改入口、删除规则、工程检查和依赖更新。
- [运行手册](docs/OPERATIONS.md)：启动、停止、备份、恢复、配置和故障定位。
- [研究工具](docs/RESEARCH.md)：离线 CLI、输入输出与证据边界。
- [Choice 研究](docs/CHOICE_RESEARCH.md)和[个人实验概率](docs/PERSONAL_EXPERIMENTAL_PROBABILITY.md)：特定数据流程。
- [测试计划与当前验收](docs/TEST_PLAN.md)、[当前维护 Goal](docs/MAINTENANCE_GOAL.md)。
- [笔记写入研究](docs/NOTE_WRITE_RESEARCH.md)：并发条件写入、草稿恢复与离线创建的研究取舍。
- [问答证据研究](docs/QA_EVIDENCE_RESEARCH.md)：2025–2026 原始论文、限制与工程取舍。
- [同类产品调研](docs/PRODUCT_RESEARCH.md)：官方资料、公开实现边界及注明日期的实施取舍。
- [全市场选股迭代计划](docs/MARKET_SCAN_PRODUCT_PLAN.md)：候选对比、自选范围和条件影响的产品对照与分阶段验收。
- 自动生成的 [API 参考](docs/API_REFERENCE.md)和[函数索引](docs/FUNCTION_INVENTORY.md)。

操作与接口说明描述当前系统；研究依据和历史验收注明各自日期与范围，历史实现的恢复交给版本控制。运行数据、研究产物、凭证、测试输出和本机记忆不属于源码文档。
