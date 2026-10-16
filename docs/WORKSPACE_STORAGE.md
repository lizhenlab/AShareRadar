# 工作区容量清单与备份范围预览

`tools/workspace_storage.py`按显式根清单读取文件元数据，统计主数据库、扶摇侧资料、研究目录及前瞻运行目录的容量。它不打开SQLite连接、不读取数据文件内容、不创建或恢复备份，也不修改保留策略。

## 1. 声明需要检查的根

自行保存一份JSON清单，将下例路径替换为本机实际绝对路径。只列入真正需要覆盖的根；不存在的根会明确报告缺失。支持`database`、`fuyao`、`research`、`prospective`、`control`、`runtime`和`backup`七类，每个`name`必须唯一。

```json
{
  "schema_version": "workspace-storage-scope-v1",
  "roots": [
    {"name": "primary", "category": "database", "path": "/absolute/data/ashare_radar.sqlite3"},
    {"name": "fuyao", "category": "fuyao", "path": "/absolute/data/ashare_radar.fuyao"},
    {"name": "research", "category": "research", "path": "/absolute/data/research"},
    {"name": "runtime", "category": "runtime", "path": "/absolute/frozen-runtime"}
  ]
}
```

清单上限64KiB、64个根，拒绝未知字段、重复JSON键、非有限数字与相对根路径。清单本身必须是普通文件，不能通过符号链接读取。没有自动发现数据库引用、其他磁盘或用户目录；`complete`只表示按默认排除规则完成所声明范围的遍历，不表示整个项目的所有数据都已列入。

运行时的`-wal`、`-shm`文件不因列入主数据库文件而自动包含；需要统计时显式列为独立根，或将其父目录作为更宽的根。活动文件大小可能变化，本次结果是观察值，不是一致备份或静止的文件系统快照。

## 2. 执行只读命令

```bash
.venv/bin/python tools/workspace_storage.py inventory --scope /absolute/storage-scope.json
.venv/bin/python tools/workspace_storage.py preview --scope /absolute/storage-scope.json
```

两个命令只向终端输出JSON。`inventory`输出容量清单，`preview`包含同一清单，并列明尚未建立备份一致性、尚未验证恢复及后续需要完成的步骤。

可显式限制`--max-files`、`--max-directories`、`--max-entries`、`--max-depth`、`--max-seconds`及`--max-issues`。排除项和链接也消耗条目预算，避免无界遍历。文件数、目录数、条目数或时间预算耗尽会停止剩余遍历，保留已观察结果并标为`partial`；深度超限跳过过深目录，继续同层检查。`max_issues`只限制问题样本数量，不单独触发`partial`，截断数量仍会保留。时间预算不能强制打断卡在内核中的单次元数据调用。

`started_at`和`finished_at`记录UTC观察窗口，`elapsed_seconds`使用单调时钟计算耗时。这个时间窗口不提供跨文件的原子快照。

退出码：`0`表示默认排除规则下所声明范围的遍历完成；`3`表示缺失、预算或读取问题导致部分统计；`2`表示清单、参数或执行错误。预览返回`0`也不代表已经可恢复，需同时读取`backup_created=false`、`restore_verified=false`和一致性字段。

## 3. 如何解释容量

- `logical_bytes`按纳入的普通文件路径出现次数累加；硬链接及重叠根会增加该值。稀疏文件的逻辑长度可能远大于实际分配量。
- `unique_bytes`按文件身份去重，硬链接和重叠根不会重复增加总体唯一字节。
- `allocated_bytes`按文件系统报告的分配块计量。每根内部去重，总体跨根再去重，因此不能把每根唯一/分配字节直接相加。
- 计量不包含目录inode等元数据，也不代表底层压缩、共享块或整个磁盘的精确占用。
- 缺失、符号链接、不支持的文件类型、排除项和读取错误分别报告；问题样本有数量上限，截断会明确标记。

普通生产环境中的密钥配置、日志、锁、备份套娃及虚拟环境按默认文件名规则排除；可再生缓存仅匹配`__pycache__`、`.pytest_cache`、`.mypy_cache`和`.ruff_cache`，不笼统排除任何名称含cache的业务目录。`runtime`类别保留普通依赖及缓存文件，但仍执行秘密配置等排除规则，报告会列出排除范围。工具不读取内容识别秘密，未匹配名称的秘密文件仍需操作者单独核对。符号链接不跟随，封存环境依赖共享基础Python等外部文件时，需要另行检查和恢复设计。排除项不作为已纳入容量，不能据此承诺完整副本可独立运行。

秘密配置、日志、锁和备份规则也检查显式根的全部祖先，`runtime`不豁免。仅当类别为`backup`且根本身名为`backup/backups/local-backups`之一时允许纳入该根，内部再出现同名目录仍排除。显式根被排除会返回`partial`；后代按规则排除只增加排除计数和有限样本，不单独使结果降级。

## 4. 创建离线归档与恢复

独立的`tools/workspace_backup.py`现已提供`create/verify/restore`，操作见[工作区离线归档](WORKSPACE_BACKUP.md)。备份清单另行声明根的文件/目录类型、恰好一个主库及范围内全部SQLite库；不能直接把本页的容量清单当成备份清单使用。创建前由操作者停止应用、runner和同步等全部写入者，工具取得已知租约并逐库使用SQLite backup API，最后发布完成清单；恢复只进入尚不存在的新目录。

容量预览仍然只读，也不因备份工具已存在而自动创建归档。归档验证不证明跨域一致性或恢复运行资格，`cross_domain_consistency_verified=false`和`resume_eligible=false`保持明确；封存运行目录遇到秘密文件等排除规则时会拒绝创建完整归档。后续运行绑定与真实恢复验收见[后续实施设计](RELIABILITY_NEXT_STEPS.md)，现有主数据库原位恢复规则见[运行手册](OPERATIONS.md#3-数据备份与恢复)。
