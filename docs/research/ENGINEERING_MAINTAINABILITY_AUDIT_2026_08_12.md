# AShareRadar 可维护性审计与渐进重构方案（2026-08-12）

> 审计对象：2026-08-12 当前共享工作树，包括尚未提交的产品改动。
>
> 结论边界：本文评估代码结构、依赖、类型、生命周期和测试可维护性，不评价选股有效性，也不把文件行数直接等同于代码质量。

## 1. 结论

项目需要继续重构，但不需要整体重写。当前应用内部导入图无环，分层、时钟、异常安全、函数复杂度、生成式 inventory 和浏览器矩阵已有较强门禁；主要风险是“无环但高度中心化”：少数兼容 façade、管理器和前端入口承担了过多组合、生命周期和转发职责。

本轮没有发现必须停服处理的 P0。已直接落地九个低风险切片：

1. API route 从 `AppContainer.domain_services` 获取领域服务，不再经 `datahub.cache.*_service` 反向定位服务；原 route-local dependency 函数保留，FastAPI override 兼容。
2. Scheduler 显式注入与容器相同的 `StrategyAutomationService`；任务和 scheduler 构造都不再从 cache 恢复领域服务。冲突实例会在绑定时拒绝；为兼容直接构造，缺失实例允许保留到对应任务执行入口，再在任何策略副作用前明确失败。
3. mypy 显式应用覆盖从 208 个文件提高到 214 个，当前检查共 223 个源文件；用泛型、只读 Protocol、`Mapping` 和运行时窄化修复真实边界不兼容，没有使用 `cast`/`ignore` 掩盖问题。
4. 浏览器 `online`、`visibilitychange`、`pagehide` 生命周期从 `static/app.js` 抽到可销毁、显式依赖注入的 `app-lifecycle.js`；入口从 3,092 行降到 3,040 行，旧代理和测试钩子保持兼容。
5. `ScreenSpecV2` 的 JavaScript 校验与 Pydantic 对齐 extra-forbid、默认值、SH/SZ/BJ 枚举、九类数值边界、唯一性、排序和 Unicode 字符限制；Python↔Node parity 覆盖完整、partial 与非法载荷，并在请求发出前失败。
6. `market_scan_evaluation.py` 的 ordinal calibration 纯指标簇迁到 122 行的 `market_scan_evaluation_metrics.py`；原私有入口继续 alias，固定 normalized report 与 calibration digest 前后相同，主模块降到 3,561 行。
7. `utils/symbols.py`、`datahub_runtime.py`、`utils/market_data.py` 进入不可回退的显式类型集合；新增检查实际发现并修复 provider 元素类型、只读映射、股票代码及可选时间字符串的旧合同问题。
8. evaluation façade 与指标模块新增 3,600/160 行下降式预算，防止职责重新回流或新模块继续无界增长。
9. 将全市场/Discovery 自然流程从临界的 `frontend-flow.spec.js` 原样迁到 `market-scan-flow.spec.js`；两个 spec 从单文件 2,747 行变为 1,499/1,229 行，合计 2,728 行，没有放宽总预算，四个 Playwright 项目的测试名、route 和 skip 语义保持不变。

跨模块私有依赖门禁也已增强：解析完整 attribute chain，并以总量、来源模块和目标模块三层基线阻止债务从一个模块转移到另一个模块。

## 2. 当前结构基线

| 指标 | 当前值 | 含义 |
| --- | ---: | --- |
| `app/**/*.py` | 368 文件 / 108,814 行 | 领域覆盖广，不能用一次性目录搬迁治理 |
| `app/services/**/*.py` | 230 文件 / 78,567 行 | 占应用代码约 72.2%，仍是主要复杂度集中区 |
| `app/repositories/**/*.py` | 44 文件 | 持久化已按域拆分，但兼容 façade 仍较宽 |
| 超过 500 / 1,000 / 1,500 行的应用模块 | 54 / 19 / 7 | 函数预算有效，但缺少职责级模块增长治理 |
| 最大模块 | `market_scan_evaluation.py` 3,561 行 | 已提取首个纯指标簇，后续继续按稳定职责渐进迁移 |
| `SQLiteCache` | 1,021 行 / 约 149 个方法 | 同时承担存储组合、兼容 façade 和历史 service locator |
| `MarketScanManager` | 953 行 / 约 53 个方法 | 命令、查询、研究、恢复和后台任务仍集中 |
| 应用内部 import edge | 约 1,697，当前无环 | 问题是中心度与接口宽度，不是循环依赖 |
| 跨模块私有依赖 | 298 | 已到原总量上限，必须逐域偿还而非互换位置 |
| mypy 显式 app 覆盖 | 214 / 368（58.2%） | 仍有 154 个应用模块未被直接列入检查 |
| `static/app.js` | 3,040 / 3,100 行预算 | 生命周期已抽离，业务事件 wiring 仍约 670 行 |
| `frontend-flow.spec.js` / `market-scan-flow.spec.js` | 1,499 / 1,229 行；合计 2,728 ≤ 拆前 2,747 | 已按产品职责拆分，并以单文件与合计预算同时防回流 |

其他大 Python 模块包括 `market_scan_shadow_scoring.py`（2,128）、`market_scan_probability.py`（1,920）、`market_scan_probability_outcomes.py`（1,711）、`market_scan_probability_replay.py`（1,666）、`market_scan_future_range.py`（1,626）和 `market_scan_probability_research.py`（1,553）。这些模块包含完整研究契约与回放语义，拆分必须由固定 artifact/report digest 证明行为等价。

## 3. 本轮迁移边界

### 3.1 组合根而不是存储 façade 拥有领域服务

生产 API 和 scheduler 现在使用 `DomainServiceBundle` 中的同一实例。`SQLiteCache` 的服务属性暂时保留给旧调用方，不在本轮删除；新增架构测试禁止 route 和 scheduler 再经 cache 获取 `*_service`。下一步应迁移剩余非生产兼容调用，再给 cache 公共 façade 增加“不再增长”的基线。

### 3.2 类型端口保持只读语义

`path`、repository 等只读依赖在 Protocol 中改为 `@property`，避免可写 Protocol 成员产生不必要的不变性；K 线调整方式复用领域 `KlineAdjustmentMode`。这属于静态合同收紧，不改变对象构造、数据库或运行逻辑。

### 3.3 前端生命周期成为可销毁 controller

`app-lifecycle.js` 只通过注入的 state、DOM target 和 effects 工作，在线恢复有 Promise 去重，`dispose()` 会移除监听。`app.js` 保留兼容代理。该模式可用于后续 market-scan、strategy、discovery controller 的统一生命周期，但不能在单例页面尚未需要重挂载时一次性改造全部 controller。

### 3.4 浏览器筛选合同与后端同源判定

`market-scan-screening-contracts.js` 现在对 `ScreenSpecV2` 做完整的请求前校验。测试把同一组完整、缺省、partial 和非法载荷分别交给 Pydantic 与 Node，并要求 acceptance 完全一致；校验返回原始对象，不在客户端伪造后端默认字段。后续增加筛选字段时必须同时扩展双方合同和 parity matrix。

### 3.5 evaluation 只提取可证明等价的纯簇

首个迁移单元只包含 ordinal calibration 的 metrics/record/bucket。新模块只依赖标准库及两个只读 Protocol，不反向导入 façade；原 `_calibration_*` 名称保留兼容 alias。固定报告 SHA-256 `5c30f5a2c4006d9110375fcf34b1b7120649729c161ce036acc460cc6eca102a` 与 calibration SHA-256 `8f36b6d7a1035c20d245135b234cc2896e9b20d8750c7787b829e1cdf9519bb2` 锁定行为等价。

### 3.6 E2E 按产品职责拆分而不扩张预算

全市场扫描、模式隔离、恢复与 Discovery 流程归入 `market-scan-flow.spec.js`，其余个股工作台、图表、模拟交易与本地研究流继续由 `frontend-flow.spec.js` 拥有。共享导航 helper 只在 fixture 中保留一份；静态门禁同时约束两个文件、fixture 和两个 spec 的合计行数。迁移块的规范化源码摘要与四项目 Playwright 回归证明这是职责迁移，不是删除覆盖或改变 skip 语义。

## 4. 后续优先级

### P1：下一轮应做

1. **窄化存储和扫描端口**：停止扩展 36-member `MarketScanCacheProtocol`；分别定义 read、command、publication、probability-capture port。`MarketScanManager` 继续作为兼容 façade，但依赖由容器显式传入。
2. **拆除 cache service locator 的剩余兼容入口**：迁移消费者后，先锁定 `SQLiteCache` 公共方法/服务属性总量不得增加，再逐域移除转发。不要一次搬走约 149 个方法。
3. **继续按职责拆 evaluation**：calibration 已迁移；下一次只选择 return/decile/Rank IC 或 stability/hysteresis 中一个依赖闭合簇。继续保留 re-export，并用固定 report、ranking digest 和 promotion blockers 证明等价。
4. **继续风险加权类型覆盖**：以 fan-in、数据写入和异常边界选择下一批显式文件；显式覆盖接近全量前，不把 `follow_imports` 从 `skip` 一次切为 `normal`。

### P2：在 P1 稳定后处理

- 将构造期的 schema/provider 状态写入迁到显式 `StorageRuntime.open()` / `DataHub.start()`，让“实例化”和“产生持久化副作用”分离。
- 用正式 `MaintenanceTransaction` 或 borrowed-connection port 替换 maintenance 期间对 repository 私有 `_connections` 的临时换线。
- 从 `app/artifacts/io.py` 抽一个只负责机械目录扫描和缓存失效的只读 index，消除 probability/future/source store 重复；各领域 schema 和 digest 校验必须继续独立。
- 当前证据 CSS 稳定后，把 `.strategy-*` 从 `market-scan.css` 字节级迁到紧邻加载的独立文件，并用 computed-style 桌面/移动回归锁定 cascade。
- 新的前端 contract 模块可逐步启用 `// @ts-check` 或 `tsc --checkJs`；不要先对全部旧 JavaScript 制造格式或类型洪水。

## 5. 明确不做

- 不按行数把大模块切成互相导入私有 helper 的碎片。
- 不一次性移动 229 个 service 文件或更改所有 import path。
- 不删除兼容 façade，除非所有生产消费者已经迁移且有调用基线证明。
- 不用 `cast`、`Any`、`type: ignore` 或调高预算制造“检查通过”。
- 不更改 SQLite schema、用户数据、生产评分、研究 artifact 或概率语义来完成结构重构。

## 6. 完成定义

每个后续重构切片都必须满足：

1. 先保存 API/schema/digest/排序或浏览器行为的 characterization evidence。
2. 新依赖通过组合根或窄 Protocol 注入；旧入口仅作为明确的兼容 façade。
3. 新旧行为等价；缺失、损坏、并发和取消路径仍 fail closed。
4. Ruff、mypy、pytest、JavaScript、关键 Playwright、两个 inventory 与 `git diff --check` 全绿。
5. 所有真实运行数据只读；不得自动清理 artifact、SQLite 行或用户文件。

## 7. 本轮验收

- 完整 Python：3,087 passed，另 64 subtests passed；行+分支精确覆盖率 90.0083%，90% 门槛未调整。
- 静态质量：Ruff 全树通过；mypy 223 个显式源文件通过；JavaScript syntax、API/FUNCTION inventories 与 `git diff --check` 通过。
- 聚焦回归：架构、类型、evaluation、ScreenSpec 和静态资源共 70 passed。
- 浏览器：迁移后的全市场/Discovery spec 在 desktop/mobile Chromium、desktop Firefox、desktop WebKit 共 22 passed、18 个按既有项目条件 skipped；共享导航另 4 passed。
