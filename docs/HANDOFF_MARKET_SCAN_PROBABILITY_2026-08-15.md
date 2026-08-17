# 全市场上涨概率修复暂停交接（2026-08-15）

## 部署后补丁：重型读取 busy 可恢复体验

- 运行 `#89` 发布后的访问日志确认：批次详情、榜单和 latest 共享容量为 1 的进程内快照校验槽；重叠读取按合同返回 `503`、`Retry-After: 2` 和“全市场冻结快照正在校验，请稍后重试”。这是 admission busy，不是 `409` 快照完整性失败。
- 通用 `fetchJson` 现保留数字秒或 HTTP-date 形式的 `Retry-After` 为 `error.retryAfterMs`；市场扫描轮询按该最短等待时间重试，busy 不增加 `consecutiveFailures`，也不触发连续失败的 latest 降级恢复。
- 结果读取 busy 时保留并重新展示 last-good 已验证榜单/概率证据，等待提示明确“已保留上次已验证结果”，不再清空可信缓存或显示“证据读取失败”；无 last-good 时显示“快照校验中·等待重试”，筛选继续 fail closed。
- latest / latest-published / results trusted chain 的 busy 仍保持可重试，不写入确定性失败 fingerprint；`409`、合同错误、网络失败和超时继续走原 fail-closed 分支，语义未放宽。
- 静态资源统一 cache token 更新为 `20260815-market-scan-busy-local-1`。此前 ordered-12 manifest 仍是 horizon 部署证据，但已被本补丁后的工作树内容取代，不能再作为当前静态文件身份清单。
- 定向验证：请求/市场扫描前端 `49 passed`；静态资源/API 错误/市场扫描 API `128 passed`；`npm run check:js` 通过；busy 恢复 Playwright 在桌面 Chromium、移动 Chromium、Firefox、WebKit 为 `4 passed`。

## 恢复、部署与线上验收完成结论

- 用户于 `2026-08-15` 明确授权从暂停工作树继续；horizon UI hotfix 已完成正式门禁、受控部署和一次性线上验收。
- Same-context 409/合同失败恢复现在保持概率面板 `aria-busy=true`，直至唯一可信恢复提交；cross-context 409 反例证明旧 run/mode/history 失败不会清除新上下文缓存。
- single-owner promise tail、last-intent、queued A→B、poll/query/terminal/history/visibility/reactivate 与 publication rebase 竞态均由完整 frontend 回归覆盖。
- 全量浏览器门禁另外发现并修复默认导出路径解构 `window.fetch` 导致 Chromium `Illegal invocation`；默认 fetch 现绑定 `globalThis`，四浏览器导出回归和全量矩阵均通过。
- Ordered-12 manifest 位于 `/tmp/ashare-radar-horizon-final.zHfqAx/ordered-12.sha256`，manifest SHA-256 为 `96a4fcd631de385a6fe82f119a36809315718991013949b1db4b86453bb99662`，12/12 文件复核通过。
- 支持式备份 `data/backups/ashare_radar_20260815T043606_269186Z` 已创建并独立验证；数据库 2,965,782,528 bytes，SHA-256 `3c39c6acb1a4861b06516a6d337744acc077ca21a3031e7f67ff7524ac4eab9a`，integrity `ok`。
- 旧 PID `90176` 优雅退出；离线 5,382 行 SLA 3/3 通过；新服务 PID `54301` 以批准的单 worker 命令启动，`live=ok`、`ready=leader`。
- 一次性线上页面正确绑定 #87：5,389 条榜单、`研究已生成·样本不足`、5,389 条点时样本、筛选关闭；快速 H1→H5→H20 只触发轻量 identity，results/latest 重读为 0，console 为空。
- Excel 导出仅请求一次并返回 HTTP 200；5,389 行生成耗时超过验收端 60 秒下载监听窗口，但页面无错误且按钮恢复，未重复请求。
- 页面关闭后 PID CPU 0.2%，所有任务终态；锁感知 `mode=ro + query_only + BEGIN` 复核 `quick_check=ok`、FK 0、`total_changes=0`，#86/#87 与 source/outbox digest 不变，#88 不存在。

## 原暂停结论（历史）

- 当时按用户要求暂停；不得继续编辑、测试、重启、人工重试 outbox 或触发新扫描。
- 当前生产运行的是已通过完整门禁的 weekend source temporal 修复版，不是下述未完成的 horizon UI hotfix。
- 当时工作树包含尚未冻结的 horizon UI 半迁移，明确不可部署、不可标记完成。

## 已完成并已上线

- 生产进程最后确认：PID `90176`、screen `90174`，单 worker Uvicorn；`live=ok`、`ready=leader`、SQLite schema `233`、`quick_check=ok`、FK `0`。
- 已部署 temporal ordered-13 manifest：`7667c5153e9d4e0eb94e23a974e7d5395cc7da06d25ee7a7deac024482de52d1`。
- #86 保持不可变失败：`5389 success / 154 missing / 0 skipped`，无 seal/outbox。
- #87 已发布为 degraded：`5389 success / 8 missing / 146 justified skip`，snapshot digest `c3388fa74e66b7eba747e99ec09698d3f3cea3b8047f23e3632c215727d30a83`。
- #87 outbox 未人工重试；automatic attempt 7 于 `2026-08-14T17:56:09Z` 成功。
- #87 source/archive 四方绑定 digest：`fb6af8b7aa9a5835fe08f7a9663caab923ce378e7d1afad6ec2d12bfd1264a6d`。
- Source artifact：`data/research/market_scan_probability_source/market-scan-probability-source-run-87-fb6af8b7aa9a5835fe08f7a9663caab923ce378e7d1afad6ec2d12bfd1264a6d.json.gz`；1,886,682 bytes；gzip SHA-256 `305c23453934c083950a60ba96a5118fd800d15fb336c80ca18159741959a6cc`。
- API 已独立核验为 `source_archived / insufficient_data`：source/session=`1/1`，observations/PIT=`5389/5389`；H1/H5/H20 两类 target 的 probability 均为 `null`，filter/selection 均为 `false`，production ranking effect 为 `none`。这是只有一个独立会话时的真实状态，不得生成伪概率或降低校准门槛。
- #88 不存在，未触发过。

## 已上线版本的门禁证据

- Full：`4288 passed + 68 subtests`，唯一 5382 SLA 分片单独验证，0 product failures。
- 显式 current-source coverage：419/419 files，combined line+branch `90.1552393273%`。
- 离线 5382 SLA 连续 3/3 通过；每轮 exact one hash、exact one replay、CPU `<5s`、query wall `<12s`。
- Final artifacts：`/tmp/ashare-radar-temporal-final.3FRPi3`。
- Rollout/SLA artifacts：`/tmp/ashare-radar-temporal-rollout.uKUkON`。
- 上线前支持式备份：`data/backups/ashare_radar_20260814T174040_960753Z`；DB SHA-256 `49ae8189ccad132736a094ae50d321728c1478fc583fd6737c340f09a250ad91`，integrity check 通过。

## 已完成并已部署：horizon UI hotfix

线上一次性页面验收发现：快速切换 H1→H5→H20 会让旧 results 请求在服务端继续持有 heavy-read admission，而客户端立即发起新 results/latest；出现 `results ×3 = 503`、`latest ×1 = 503`，页面至少 6 秒停在“证据读取失败·等待重试”。初始页面本身正确显示 #87、“研究已生成·样本不足”、5389 条已归档点时样本、概率 `--`、筛选禁用，console 为空。

本轮半迁移涉及：

- `static/js/market-scan-controller.js`
- `static/js/market-scan-latest-loader.js`
- `static/js/market-scan-latest-sync.js`
- `static/js/market-scan-history.js`
- `static/js/market-scan-surface.js`
- `static/js/market-scan-view.js`
- `static/js/market-scan-probability-horizon-controller.js`（新）
- `static/js/market-scan-read-transition.js`（新）
- `static/js/market-scan-export-action.js`（新）
- `static/index.html`
- `tests/test_market_scan_frontend.py`
- `tests/e2e/market-scan-probability.spec.js`

已实现/短节点曾通过的方向：

- 四类重读使用 single-owner promise tail、last-intent generation，峰值 owner 为 1，tail rejection 后可恢复。
- 无 probability min 时 horizon 切换只做已验证 payload 的本地重投影，heavy request 为 0。
- 有 probability min 时清旧阈值，基于 exact applied URL 重建 page 1，仅删除 `probability_horizon` 与 `min_upside_probability`，保留已经提交的其他筛选；最多补一次查询。
- direct/latest stale response 不提交旧 DOM；selector stage、history、surface、user query、active pollRun 与 failure recovery 的关键 deferred 短节点曾分别通过。
- hidden→visible 且 identity 未变时可复用绑定的 last-good validated payload，不重复 heavy selector。

## P1 / 验证缺口闭合记录

1. Same-context stale trusted 409/合同错误恢复在途保持 `aria-busy=true`；完成后恢复真实证据状态。
2. 新增 cross-context stale 409 反例，旧 run/mode/history 失败不清除或污染新上下文缓存。
3. queued-before-start A→B、promise settle、零旧 HTTP、零 unhandled rejection 回归通过。
4. pending poll→exact query、terminal reconcile、history light-prelude、visibility/deactivate/reactivate 与 publication rebase 回归通过。
5. 完整 `tests/test_market_scan_frontend.py` 为 `38 passed`。
6. Horizon 四项目 Playwright 为 `12 passed`；最终全量矩阵为 `147 passed / 49 intentional skips`，无失败。
7. `check:js`、Ruff、mypy（255 source files）、static/function/tool/typing 39 项、双 inventory、`git diff --check` 全绿；FUNCTION inventory 已机械更新。
8. 全量 Python 为 4,294 项在受限沙箱通过、2 项仅因 loopback bind 被拒，授权回环后 Uvicorn smoke `2 passed`；68 subtests 通过，combined line+branch coverage `90.14407527197883%`。
9. 独占 5,382 行门禁连续 3/3 通过；每轮 exact one hash、exact one replay、CPU `<5s`、query wall `<12s`。

## 已执行的部署闭环

1. Ordered manifest 在停服前、离线 SLA 后和上线后均复核 12/12 一致。
2. 当次支持式备份、优雅停服、离线 SLA 3/3、单 worker 重启与 live/ready/数据库检查均完成。
3. 一次性线上快速周期切换、筛选 fail-closed、单次 Excel 导出和关闭页面后的资源排空均完成；未触发扫描或人工重试 outbox。
