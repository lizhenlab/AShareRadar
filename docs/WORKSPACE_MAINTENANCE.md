# 工作区维护面板

“系统维护 → 运行状态 → 工作区维护”集中展示显式配置的容量范围、备份覆盖及验证记录、前瞻采集巡检。进入页面只读取有界元数据；容量检查通过按钮单独触发，不递归扫描整个项目或磁盘。

## 服务端配置

配置固定在当前主数据库同目录，以 `.maintenance.json` 替换数据库后缀。例如默认数据库 `data/ashare_radar.sqlite3` 对应 `data/ashare_radar.maintenance.json`。配置不随用户数据导出，不接受浏览器传入任意文件路径。文件不存在时页面显示“尚未配置”；内容无效或不可读时显示不可用，不猜测其他路径。

```json
{
  "schema_version": "workspace-maintenance-scope-v1",
  "storage_roots": [
    {"name": "主库", "category": "database", "path": "/absolute/data/ashare_radar.sqlite3"},
    {"name": "研究资料", "category": "research", "path": "/absolute/data/research"}
  ],
  "backups": [
    {
      "name": "维护前归档",
      "archive": "/absolute/archive-store/snapshot",
      "expected_digest": "替换为独立保存的64位小写十六进制摘要",
      "verification_receipt": "/absolute/archive-store/verify-result.json"
    }
  ],
  "runners": []
}
```

示例路径与摘要必须替换。容量根遵循[工作区清单](WORKSPACE_STORAGE.md)的分类、排除及符号链接规则；最多64项。备份最多4项，巡检最多8项，各节名称唯一。配置文件上限64 KiB，路径必须指向服务进程可读取的本机对象；建议用临时文件加原子替换更新配置。配置摘要发生变化后，旧页面的容量请求会拒绝，刷新维护状态后重试。

## 容量检查

`GET /api/system/workspace-maintenance`返回配置摘要与状态，不启动容量遍历。点击“检查容量”发送`POST /api/system/workspace-maintenance/storage?scope_digest=...`，只检查配置中的根，响应不包含源绝对路径。单进程同一时刻只执行一次容量检查，竞争返回503及`Retry-After: 2`；浏览器停止等待时，服务端仍会持有锁直到当前扫描退出。

页面检查限制为2秒、10,000个文件、20,000个目录项、2,000个目录；保留现有深度、问题数量等预算。超限、缺失、排除或无法读取时按原报告呈现`partial`和已观察容量，不能据此推断整个工作区总量。更大范围使用独立CLI及显式预算。

## 备份记录

读取并核对归档清单格式、自摘要及配置中的外部期望摘要，再显示根名称、文件数、字节数和排除项。此页面不重读归档负载、不打开归档SQLite，也不运行封存代码；清单超过2 MiB时显示不可用，可使用CLI验证。

`verification_receipt`可省略；配置时应指向[工作区备份CLI](WORKSPACE_BACKUP.md)成功运行`create/verify/restore`后保存的标准输出JSON。面板核对操作类型、归档路径、摘要及`archive_verified=true`；包含合法`verified_at`时显示记录时间。旧回执没有此字段时明确显示“旧格式未记录验证时间”，不使用文件修改时间冒充。路径、摘要或时间不匹配时显示“验证回执无法核对”。回执上限256 KiB。

历史回执不是当前文件的重新验证。归档之后被损坏时，清单和历史记录仍可能读取成功，完整性必须另行运行`verify --expected-digest`确认。API始终返回`current_payload_verified=false`、`resume_eligible=false`，页面也提示恢复后实际可运行性需独立验收。

## 前瞻采集巡检

每项runner配置包含`name`、`root`、`plan_id`、`control_root`、`plan_digest`、`runtime_directory`、`runtime_digest`；应使用已确认的真实绝对路径和摘要，不为消除告警修改旧计划或运行目录。面板以当前主库和这些字段构造完整巡检身份，读取独立健康记录并探测会话锁，显示阶段、最近完成、下次检查和故障原因。

身份不符、心跳超时、进程已离开、健康记录不可读都会保守提示。页面不创建或启动runner、不回填研究收据、不验证整个运行目录；`runtime_execution_verified=false`、`sample_admission_verified=false`始终保留。新计划准入与实际运行环境验证继续使用[前瞻采集流程](STRATEGY_PROSPECTIVE_COLLECTION.md)。
