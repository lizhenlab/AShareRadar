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

## 当前功能

| 入口 / 模块 | 用途 |
| --- | --- |
| 个股研究 | 行情、技术与基本面解释、日线/分钟图、研究问答、笔记预警与通知原记录导航 |
| 自选监控 | 自选分组、队列检索与到期／未读筛选、显式查看新变化、报价观察 |
| 全市场选股 | 冻结扫描、规则评分、条件筛选、覆盖率与缺失解释、方案另存与分页、变化记录明细、Excel 导出 |
| 策略实验室 | 草案与版本校验、策略分页、冻结执行回看、定时任务查看与启停、纸面委托与证据评估 |
| 复盘模拟 | 版本化计划、全局计划精确定位、到期队列筛选与分页、不可变评估记录、组合模拟 |
| 系统维护 | 运行状态与后台任务、数据管理、导入导出和备份 |
| 离线研究 | 历史样本、概率评估、未来区间、执行成本、资金约束和敏感性分析 |

正式评分、展示百分位和研究概率是不同指标。研究产物不会自动取得排序、筛选或发布权限；正式来源、完整性、样本外评估及独立授权必须分别通过校验。

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
- 自动生成的 [API 参考](docs/API_REFERENCE.md)和[函数索引](docs/FUNCTION_INVENTORY.md)。

操作与接口说明描述当前系统；研究依据和历史验收注明各自日期与范围，历史实现的恢复交给版本控制。运行数据、研究产物、凭证、测试输出和本机记忆不属于源码文档。
