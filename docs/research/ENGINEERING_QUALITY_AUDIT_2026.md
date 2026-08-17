# AShareRadar 工程质量审计（2026）

> 核验日期：2026-07-24
>
> 核验对象：核验日当前工作树，包括未提交修改；实现证据以源码、测试、锁文件和工作流为准，不以 README 自证。
> 结论边界：本文是面向本地、单用户 A 股研究工具的工程审计，不是 ISO 认证、正式 ATAM 评估、渗透测试、外部 SLA、投资建议或供应链等级认证。

## 1. 审计方法

本次审计用三类证据交叉核验：

1. **可执行证据**：运行时检查、架构边界测试、时钟/迁移测试、异常安全测试、可靠性测试、canary 测试、供应链测试、类型范围防回退测试，以及 CI/Security 工作流。
2. **实现证据**：实际模块依赖、配置解析、健康探针、SQLite 表和迁移、可靠性聚合、provider canary、依赖锁、SBOM 工具和 GitHub Actions 配置。
3. **权威参照**：ISO/IEC 25010:2023、SEI ATAM、NIST SSDF、Google SRE、OWASP ASVS、CycloneDX 与 SLSA。参照用于发现质量属性、场景和证据缺口，不把“参考了标准”写成“符合/通过标准”。

截至核验日采用的权威版本或页面：

- [ISO/IEC 25010:2023 产品质量模型](https://www.iso.org/standard/78176.html)：官方页面说明该模型包含九类产品质量特征，可用于需求、设计、测试目标和验收标准。
- [SEI Architecture Tradeoff Analysis Method](https://www.sei.cmu.edu/library/the-architecture-tradeoff-analysis-method/)：通过业务驱动、质量属性场景、敏感点和权衡点分析架构风险。
- [NIST SP 800-218 SSDF 1.1](https://csrc.nist.gov/pubs/sp/800/218/final)：把安全实践集成进既有 SDLC，以减少漏洞、降低潜在影响并处理根因。NIST 已发布 1.2 草案；本文不把草案当作现行最终版。
- [Google SRE SLO 文档示例](https://sre.google/workbook/slo-document/)与[误差预算策略](https://sre.google/workbook/error-budget-policy/)：SLI/SLO 应明确测量点、目标、窗口、排除条件与误差预算后果。
- [OWASP ASVS](https://owasp.org/www-project-application-security-verification-standard/)：为 Web 应用技术安全控制提供可验证的需求和测试基线。
- [CycloneDX SBOM 能力](https://cyclonedx.org/capabilities/sbom/)与[规范概览](https://cyclonedx.org/specification/overview/)：用机器可读方式表达组件、依赖及其关系，服务于漏洞、许可证和供应链风险管理。
- [SLSA 1.2 规范](https://slsa.dev/spec/v1.2/)与[Provenance 定义](https://slsa.dev/spec/v1.2/provenance)：用分级控制和可验证 provenance 提高源码与构建产物可信度。当前项目没有据此声明等级。

## 2. 结论摘要

当前工作树已从“功能密集的本地应用”向“具备可执行工程合同的本地应用”迈进，最明显的改进是：

- 运行环境、时间语义、模块依赖和类型检查范围不再仅靠约定，而有自动化守卫。
- liveness、readiness、provider 可用性和用户工作流可靠性被拆成不同信号，减少错误联动。
- 可靠性数据按 UTC 小时低基数聚合，SLO 明确窗口、目标与样本下限，不用小样本制造“达标”。
- 供应链已覆盖双 Python 锁与 npm 审计、当前/历史密钥扫描、可复现 SBOM、Dependabot、Action SHA 固定和 checkout 凭据关闭。
- live provider 检查使用临时 SQLite 且有总超时、清理和稳定退出码，不污染正常运行数据，也不绑架 PR CI。

主要剩余风险不是“再加功能”，而是治理成熟度：SLO 尚无代表性长期样本和误差预算动作；ASVS/威胁模型尚未形成可追踪清单；SBOM 尚未连接签名发布或 SLSA provenance；canary 只验证代表代码和有限合同，不能证明全市场或持续可用。

## 3. ISO/IEC 25010 视角的实现证据

下表是项目对质量模型的工程映射，不是 ISO 合规声明。

| 质量关注点 | 当前证据 | 判定 | 保留风险 |
| --- | --- | --- | --- |
| 功能适合性 | 确定性扫描/排序、数据质量门、结构化降级、版本化规则、现有大规模领域测试 | 已有较强证据 | 外部 provider 变化仍可能让结果不可用；不能从算法覆盖推导投资有效性 |
| 性能效率 | provider 有界并发与超时、SQLite 异步卸载、monotonic deadline、扫描 duration SLI | 部分落地 | 无持续负载基线、SSE 压测或 provider 超时级联性能测试 |
| 兼容性 | Python 3.12、Node 22/24、npm 10/11 可执行合同；旧 SQLite 时间迁移；Node 24 smoke | 已落地 | 浏览器矩阵集中在 Chromium；历史数据库没有覆盖每个已发布版本的固定回放包 |
| 交互/可用性 | 明确降级、ARIA 状态、浏览器回归、健康与诊断分层 | 已有证据 | 尚无完整视觉回归和可用性研究样本 |
| 可靠性 | liveness/readiness、UTC 小时桶、7/30 天 SLI/SLO、样本下限、canary | 已落地基础 | SLO 目标是初始假设，无 burn-rate 告警和误差预算动作 |
| 安全性 | 同源写边界、凭据环境化、双向错误脱敏、双锁审计、历史密钥扫描、固定 Action SHA | 已有基础 | 未形成 ASVS 控制追踪、正式威胁模型、发布签名或 provenance 验证 |
| 可维护性 | 单向依赖、无环导入、模型直引、配置拆分、mypy 范围 ratchet、函数复杂度守卫 | 已落地基础 | mypy 仍是显式增量范围，不是全仓严格类型检查 |
| 灵活性/可移植性 | provider registry、兼容 facade、项目相对路径、临时 canary DB、无文档本机路径 | 已有证据 | 产品运行和 CI 仍有 macOS 优先假设，外部 SDK 跨平台行为未全面验证 |
| 安全失效 | 市场日历越界关闭、缺失/脏数据不生成行动结论、provider fallback 显式化 | 已有证据 | 研究结论仍依赖第三方数据正确性，不能把数据质量分数当作真实性保证 |

## 4. 架构与时间合同

### 4.1 运行时合同

`tools/runtime_contract.py` 同时检查实际版本与声明漂移：

- Python 3.12.x；
- Node.js 22.x 或 24.x；
- npm 10.x 或 11.x；
- `.python-version=3.12`、`.node-version=22`；
- `package.json` 的 Node/npm engines 与工具内合同一致。

CI 的 macOS quality job 使用 Node 22，独立 macOS smoke 使用 Node 24；浏览器回归使用 Ubuntu Chromium。由此能证明两个 Node 主版本的工程脚本兼容性，但不能证明应用支持任意 Node 版本或任意浏览器。

### 4.2 时间合同

`app/utils/clock.py` 是唯一直接读取当前墙上时间的生产边界：

- 市场/交易语义：aware `Asia/Shanghai`；
- 审计/持久化语义：aware UTC，序列化为 ISO 8601 `Z`；
- TTL、deadline、throttle：monotonic；
- latency、duration：performance clock。

一次性迁移 `20260724_audit_timestamps_utc_v2` 只处理显式列清单。旧的 naive `YYYY-MM-DD HH:MM:SS[.fraction]` 被解释为上海时间再转换为 UTC，已带时区的 ISO 值不改写，市场事件字段不批量迁移。该边界兼顾历史 SQLite 可读性与新写入一致性，但仍应为未来新增审计列同步补迁移测试。

### 4.3 依赖方向与配置拆分

自动化架构合同要求：

```text
API -> workflows -> services -> repositories/DB
                   -> domain models/utils
```

`db`、`repositories`、`models`、`utils` 不得反向导入 API、services 或 workflows；生产代码直接导入领域模型，不依赖 `app.models.schemas` 聚合 facade；应用内部导入图必须无环。provider 错误合同、建议变更和规则版本等共享类型已移到低层所有者。

配置拆成三个实现模块和一个稳定 facade：

- `config_settings.py`：`Settings`、环境变量、默认值、路径和惰性 shell fallback；
- `config_shell.py`：不执行 shell 的赋值解析与密钥文件权限检查；
- `config_validation.py`：LLM endpoint 等安全校验；
- `config.py`：兼容 re-export。

这降低了配置、安全解析和业务 settings 的共同变化半径。仍需保持 provider adapter 不直接读取环境变量，避免重新形成隐藏配置依赖。

## 5. ATAM 场景与权衡

这不是完整九步 ATAM；它使用 SEI 的场景/敏感点/权衡思路做轻量审计。

| 场景 | 架构响应 | 敏感点/权衡 | 判定 |
| --- | --- | --- | --- |
| 主机时区改为 UTC 或其他地区 | 市场时间固定上海、审计固定 UTC、持续时间 monotonic | 旧 naive SQLite 的解释规则 | 已有迁移和时区测试 |
| provider 全部离线，但本地服务和数据库正常 | readiness 保持 ready；数据路径显式降级，provider SLI/canary 失败 | 可用性与数据真实性分离 | 合理；不能让进程编排把外部故障变成重启风暴 |
| SQLite 忙或不可读 | readiness 在 250 ms SQLite/1 s 总预算内返回通用 503 | 误报 unready 与请求堆积之间权衡 | 已有有界实现；阈值是本地运行假设 |
| 第二进程共享数据库 | 两者可 ready，只有一个 runtime leader | 读可用性与后台写唯一性分离 | 与单 worker、本地工具边界一致 |
| 一个 provider SDK 永不返回 | daemon executor 不阻塞解释器退出；正在运行线程不能强杀 | 清理完整性与有界退出权衡 | 行为已明确，不能声称调用被取消 |
| 新模块需要共享合同 | 合同下移到 models/utils，facade 只向上兼容 | 修改便利与依赖纯度权衡 | 架构测试可阻止反向依赖 |
| 依赖或 Action 更新 | 锁审计、SBOM、Dependabot、SHA 固定、CI 回归 | 更新速度与可复现/审查成本权衡 | 已有检测；无签名发布链 |
| live provider 行为漂移 | 可选 canary 验证三市场代表报价、5-row 日 K 请求、股票池结构 | 真实环境证据与 CI 稳定性权衡 | canary 不进入必需 PR CI 是合理选择 |

## 6. 可靠性模型

Google SRE 强调从用户可见结果定义 SLI，并明确目标、窗口、测量位置和排除条件。当前实现采用固定窗口与显式样本下限：

| SLI | 好事件 | 窗口 / 下限 | 目标 |
| --- | --- | --- | ---: |
| `workbench_usable` | 工作台完成并形成响应 | 7 天 / 20 | 99% |
| `workbench_quality` | 成功工作台的数据质量分至少 50 | 7 天 / 20 | 95% |
| `workbench_fresh` | quote event 与日 K 新鲜度通过 | 7 天 / 20 | 95% |
| `workbench_non_fallback` | quote/K-line 未使用 cache/provider fallback | 7 天 / 20 | 80% |
| `provider_attempt` | provider capability attempt 成功 | 7 天 / 20 | 95% |
| `task_success` | 非全市场普通任务为 `success` 或 `degraded` | 7 天 / 20 | 95% |
| `market_scan_success` | 全市场终态为 `success` 或 `degraded` | 30 天 / 3 runs | 90% |
| `market_scan_coverage` | 成功排序 symbol / 总 symbol | 30 天 / 3 runs | 95% |
| `market_scan_duration` | 非 retry 扫描 duration 的 nearest-rank p95 | 30 天 / 3 durations | <= 90 分钟 |

正面设计：

- 低于下限统一返回 `insufficient_data`，避免零样本或小样本“达标”。
- cancelled 不计失败；interrupted 计入扫描终态失败；retry duration 不污染扫描时长分位数。
- workbench/provider 以 UTC 小时聚合，symbol、request ID、URL 和异常文本不成为 label。
- provider 可靠性与 capability 状态在同一 SQLite 事务写入，避免状态成功而 SLI 丢失或相反。
- `ASHARE_RADAR_MAX_RELIABILITY_BUCKET_ROWS` 对可再生聚合数据做全局保留上限。

限制：

- 当前目标是工程假设，不是依据长期用户数据校准的 SLA。
- 尚无多窗口 burn-rate 告警、误差预算消耗或超预算后的发布策略。
- provider attempt 是内部尝试，不等于一次用户请求；fallback 可能让用户成功但 provider SLI 下降。
- scan coverage 是 symbol 加权，极大批次会比小批次贡献更多样本；响应同时提供 run 数用于判断样本下限。

## 7. 健康探针与 Canary

### 7.1 健康探针

- `/api/health/live` 只读取 app state，不构造容器、不访问 SQLite/provider。
- `/api/health/ready` 要求 lifespan 已完成启动且仍接受请求，并执行只读 SQLite `SELECT 1`。
- provider 网络不参与 readiness；runtime standby 可以 ready 并报告角色。
- 健康响应 `no-store`，失败只返回组件状态，不回显异常或本地路径。

这符合“liveness 判断是否重启进程、readiness 判断是否接收请求”的职责分离。当前项目是本地单进程工具，未声明 Kubernetes 或其他编排平台配置。

### 7.2 可选 Provider Canary

`tools/provider_canary.py` 使用默认代表代码 `600519.SH`、`000001.SZ`、`920066.BJ`，也接受 tool-only 的 `ASHARE_RADAR_CANARY_SH_SYMBOL`、`ASHARE_RADAR_CANARY_SZ_SYMBOL`、`ASHARE_RADAR_CANARY_BJ_SYMBOL` 或对应 CLI 参数。它们不属于 app `Settings`，因此不应被写进 app 配置变量合同。

CLI 用临时 SQLite 创建 DataHub，关闭 scheduler，并在有界时间内并发验证：

- 每个市场一条直接、非缓存 quote；
- 每个市场发起一个直接的 5-row 完成日 K 请求，要求返回 3-5 条可用记录，并检查日期顺序、有限 OHLCV、未来/陈旧值、cache/fallback；
- 股票池身份、去重和 SH/SZ/BJ 至少各一条；
- 异常输出脱敏和 DataHub 有界关闭。

退出码 `0/2/1` 分别表示完整、部分、无可用市场或最终清理失败。它能发现真实 provider/解析合同漂移，但不能证明全部股票、全部能力、全交易时段或长期可用性。

## 8. NIST SSDF、OWASP 与供应链证据

| 安全实践 | 当前实现 | 差距 |
| --- | --- | --- |
| 保护源码和 CI 凭据 | Action 全 SHA 固定；checkout `persist-credentials: false`；工作流只读权限 | 未记录分支保护、两人评审或组织级权限证据，不声明 SLSA Source 等级 |
| 管理依赖漏洞 | 独立、provider-free、全哈希 security-tool lock 在 Linux wheel-only 安装；`pip-audit` 扫描 runtime/dev/security 三份 hashed locks；`npm audit` high gate；Dependabot 覆盖 pip/npm/Actions | 没有独立 dependency-review PR gate 或漏洞修复 SLA |
| 发现硬编码凭据 | checksum-verified Gitleaks 扫当前树和完整 Git 历史，输出 `--redact=100` | 检测不等于 push protection；真实泄露仍必须撤销/轮换 |
| 软件成分透明度 | 从 Python/npm 锁生成 CycloneDX JSON，去除波动字段、稳定排序、两次生成做字节比较并上传 | SBOM 未签名，未和发布物/commit 做可验证绑定 |
| 应用安全控制 | 同源 mutation 边界、输入验证、错误脱敏、凭据不入项目文件、异常安全静态守卫 | 尚无逐项 ASVS profile、威胁模型、DAST/渗透测试证据 |
| 构建来源与完整性 | 可复现依赖安装、Action 固定、SBOM 可复现 | 无 signed artifact、SLSA build provenance、attestation verification；不得声称 SLSA level |

CycloneDX 提供的是机器可读成分和依赖关系；SLSA provenance 解决“产物在哪里、何时、如何生成”的可验证来源。当前项目完成了前者的一部分，但没有完成后者，二者不能混称。

## 9. 异常、取消与信息泄露边界

`tests/test_exception_safety.py` 把宽异常处理变成审查清单：

- `BaseException` 只允许在进程、任务、事务和资源所有权边界出现，并要求传播、聚合、Future 交接或已完成观察者等明确策略。
- `asyncio.CancelledError` 只能在 SSE 断连或 task done callback 等终态观察者消费；其他 async 路径必须传播。
- provider sanitizer 只有一个低层实现，service facade 仅兼容导出；API、SSE、repository 写入和 mapper 读取都必须经过脱敏。
- 静态检查拒绝把敏感标识符/字面量直接传给日志、stdout/stderr 或响应构造器。

这能防止已知模式回退，但不是通用污点分析。代码审查仍需关注字符串间接拼接、第三方库日志和新输出边界。

## 10. 历史研究文档状态

以下文档继续保留，但只代表其标注日期的产品/代码快照，不是 2026-07-24 当前实现合同：

- `COMPETITOR_CHINA.md`、`COMPETITOR_GLOBAL_CHARTING.md`、`COMPETITOR_RESEARCH_WORKFLOWS.md`、`COMPETITOR_CORE_FEATURES_2026.md`：2026-07-15 的竞品资料截面。外部产品、价格、覆盖和帮助页面可能已变化。
- `LOGIC_AND_ARCHITECTURE_GAPS.md`：2026-07-15 工作树缺口清单。不能仅凭该文档判断问题仍存在或已经解决。
- `CURRENT_CAPABILITY_AUDIT.md`、`PRODUCT_GAP_AND_ROADMAP.md`：2026-07-16 能力与路线图快照。后续代码重构、全市场扫描、健康/可靠性和供应链治理不应倒推写回旧结论。

引用这些文档时必须同时带日期；判断当前行为应重新读取源码、测试和当前设计/运维文档。竞品文档证明的是当时观察到的产品模式，不证明数据授权、实现存在、效果或当前仍可用。

## 11. 当前验收合同

必需的 hermetic 验收：

```bash
$PYTHON -m pip install --require-hashes -r requirements-dev-lock.txt
$PYTHON -m pip check
npm ci
$PYTHON tools/runtime_contract.py
$PYTHON -m ruff check app tests tools
$PYTHON -m mypy
npm run check:js
$PYTHON tools/api_inventory.py --check
$PYTHON tools/architecture_inventory.py --check
$PYTHON -m pytest -q -p no:cacheprovider \
  --cov=app --cov=tools --cov-report=term-missing
npx --no-install playwright install chromium firefox webkit
npm run test:e2e
```

安全验收还必须覆盖：双 Python lock audit、`npm audit --audit-level=high`、Gitleaks 当前树与完整历史、两次 CycloneDX SBOM 生成与字节比较。完整命令和 CI 对照见 `docs/OPERATIONS.md`。

可选 live 证据：

```bash
$PYTHON tools/provider_canary.py
```

验收规则：

- branch coverage 不低于 90%；
- mypy 显式范围只能扩大或经审查修改，不能静默缩小；
- 架构、时间、异常、可靠性、canary、供应链和 typing contract 测试必须存在并通过；
- 文档不得包含本机绝对路径、凭据或用户信息；
- live canary 不替代 hermetic 测试，也不因外部网络波动阻塞 PR；
- 未生成并验证签名 provenance 前，不得声称 SLSA 等级或可验证发布链。

本次文档核验实际执行并通过：工程合同定向测试 53 项；文档路径/索引/配置与供应链定向测试 65 项；runtime contract（Python 3.12、Node 24.14.1、npm 11.11.0）；六份变更文档的 `git diff --check`、本机路径和常见凭据形态扫描。该记录不冒充当前工作树的完整 coverage、Playwright、双 lock 实际漏洞数据库查询或 Gitleaks 完整历史扫描；发布前仍须执行上面的完整验收和 GitHub Security workflow。

## 12. 建议的后续工程顺序

1. 收集足够的本地代表性可靠性样本，再校准 SLO；在此之前保持 `insufficient_data` 语义。
2. 为现有安全边界建立轻量威胁模型和适用的 OWASP ASVS 控制追踪，不新增产品功能。
3. 定义漏洞修复时限、密钥泄露响应和依赖审计例外流程，让 Security workflow 的发现有闭环。
4. 若开始发布二进制/安装包，再引入签名、构建 provenance 和验证策略；在此之前不提前宣称供应链等级。
5. 为高风险布局补视觉回归、为 provider timeout cascade/SSE 补性能基线、为历史 SQLite 版本补固定回放样本。
