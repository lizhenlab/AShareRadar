# 工作区离线归档、校验与新目录恢复

`tools/workspace_backup.py`提供显式范围的`create/verify/restore`命令，覆盖声明的SQLite数据库及相关普通文件。它不会自动发现所有业务目录、停止服务、下载资料、执行归档中的Python或启动恢复后的应用。先用[容量与范围预览](WORKSPACE_STORAGE.md)核对需要覆盖的根，再准备本页的独立备份清单；两种清单的schema不同。

成功输出的`archive_verified=true`表示本次归档检查通过。`cross_domain_consistency_verified=false`和`resume_eligible=false`始终保留：归档检查不证明全部外部写入者已停止，也不授予研究采用或恢复运行资格。现有`runtime_data.py`主数据库备份、保留轮转及原位恢复合同保持独立。

## 1. 确认停写并声明范围

创建前，先停止应用、独立前瞻runner、扶摇同步、研究写入和可能清理这些目录的其他进程，等待实际退出。不要以删除锁文件代替停写。清单中的`offline_writers_stopped: true`是操作者声明，不是工具完成了自动停机或证明外部进程不存在。

将下例保存为本机JSON文件，替换为实际绝对路径。示例展示不同类别的分立根，并不代表本机默认目录已全部列齐；不存在或不需要的根应在确认范围后移除。目录根不能互相包含，也不能另列其内部文件为第二个根。

```json
{
  "schema_version": "workspace-backup-scope-v1",
  "offline_writers_stopped": true,
  "roots": [
    {"name": "primary", "category": "database", "path": "/srv/ashare/data/ashare_radar.sqlite3", "kind": "file"},
    {"name": "fuyao", "category": "fuyao", "path": "/srv/ashare/data/ashare_radar.fuyao", "kind": "directory"},
    {"name": "ranking", "category": "research", "path": "/srv/ashare/data/research/ranking_artifacts", "kind": "directory"},
    {"name": "prospective", "category": "prospective", "path": "/srv/ashare/data/research/strategy_prospective", "kind": "directory"},
    {"name": "control", "category": "control", "path": "/srv/ashare/data/research/strategy_prospective_control", "kind": "directory"},
    {"name": "runtime", "category": "runtime", "path": "/srv/ashare/frozen-runtime", "kind": "directory"}
  ],
  "databases": [
    {"root": "primary", "relative_path": ".", "role": "primary"},
    {"root": "fuyao", "relative_path": "research.sqlite3", "role": "auxiliary"}
  ]
}
```

清单必须显式声明**恰好一个主库**，以及所选范围内全部其他SQLite库。上例仅适用于其他根没有SQLite的情况；如研究目录内还有数据库，须补充其`root`、相对路径和`role: auxiliary`。文件根用`relative_path: "."`；目录根用根内的规范相对文件路径。未声明的SQLite文件会被拒绝，识别依据包括SQLite文件头及`.db/.sqlite/.sqlite3`后缀。

根名称须匹配`[a-z][a-z0-9_-]{0,63}`且唯一；类别支持`database/fuyao/research/prospective/control/runtime/backup`。类别决定领域检查，不能将前瞻或运行目录随意改标为普通研究目录来绕过检查。根和数据库声明各最多64项，清单最多64KiB；拒绝未知字段、重复JSON键、非有限数字、路径穿越、任意符号链接与特殊文件。仅接受共享规则验证过的macOS`/tmp`和`/var`系统别名。数据库还拒绝硬链接别名。

创建期间取得已知应用/产物租约、所有声明数据库的SQLite写保留及发现的已知领域锁，逐库使用SQLite backup API。声明数据库的`-wal/-shm/-journal`作为快照侧文件排除，不把活动WAL复制成快照。普通文件按稳定身份复制，并复查源清单；这些检查仍不能证明任意外部写入者都遵守同一锁协议。

复制完成并通过源目录签名和数据库身份复查后，工具释放源租约，再继续对目标归档做完整校验。启用下述进度输出时，`source_leases_released`阶段表示这一边界已经完成，操作者可恢复原服务及其他源写入者；工具不会自动启动它们。该信号不表示备份成功：目标仍可能校验失败，须等待最终成功JSON及完成清单。未启用进度时，应等待命令完成后再恢复服务。

## 2. 创建、验证与恢复命令

在项目根目录使用已安装依赖的Python运行。源根、归档目录和恢复目录须为绝对路径；目的目录的父目录应已存在，目的目录自身必须**尚不存在**，即使已有空目录也会拒绝。创建目标须与所有源根分离；恢复目标须与输入归档和清单记载的原源根分离。

```bash
.venv/bin/python tools/workspace_backup.py create \
  --scope /absolute/workspace-backup-scope.json \
  --destination /absolute/archive-store/snapshot-20261015
```

保存成功输出中的`digest`到归档之外的可信记录。下面的变量需要填入该实际64位小写SHA-256，不要使用示例占位文字：

```bash
ARCHIVE_DIGEST='填写独立保存的64位小写SHA-256'
.venv/bin/python tools/workspace_backup.py verify \
  --archive /absolute/archive-store/snapshot-20261015 \
  --expected-digest "$ARCHIVE_DIGEST"
.venv/bin/python tools/workspace_backup.py restore \
  --archive /absolute/archive-store/snapshot-20261015 \
  --destination /absolute/restore-area/restored-20261015 \
  --expected-digest "$ARCHIVE_DIGEST"
```

`--expected-digest`在`verify/restore`中可选；需要确认“就是之前独立记录的那一份”时应提供。清单自摘要及成员SHA-256用于完整性检查，**不是数字签名**；能够改写归档的人也能重新计算自摘要。把期望摘要只保存在同一归档里不构成外部身份固定。

`verify`只检查归档本身，无需原源目录仍然存在，也不会打开原源数据库或执行封存运行目录中的解释器。它核对清单身份、文件集合、大小、普通权限位和摘要，拒绝额外或缺失成员，再检查SQLite及已知领域绑定，并复查全树身份和元数据以拒绝检查期间的晚变更；返回的是本次重新计算的领域检查结果。它不联网补缺文件。

`restore`先验证源归档，再复制到新目录并再次验证。只有主库的恢复副本旋转预警通知流身份，避免复用原通知游标；旁库保留其原字节，不增加主库通知协调状态。输入备份不变。恢复结果生成新的清单和摘要，`operation=restore`及`source_backup_digest`记录来源；再次验证恢复目录时使用**恢复结果的新digest**，不能继续使用原备份digest。

旁库在复制后执行一次数据库完整校验，并继续参加最终归档校验；主库在通知流旋转前后分别校验。复制时的身份、侧文件、字节预算和变化检测保持启用。

成功退出码为`0`，结构化操作失败为`2`。失败输出包含`archive_verified=false`、`resume_eligible=false`和错误类型；未知命令、缺少参数及非法数字等参数解析错误也返回脱敏JSON，此时`operation=null`，不会回显原参数或执行归档。工具不自动轮转、删除旧备份或覆盖生产数据。

成功结果增加`verified_at`，记录本次实际校验成功后的UTC时间。它属于输出回执，不改写既有归档清单；可将标准输出保存到归档之外，与`archive_path`和`digest`一起记录最近一次验证。历史回执不证明文件此后始终未变。

三个命令均可添加`--progress`，向标准错误输出逐行JSON进度；标准输出仍只包含最终结果JSON。例如：

```bash
.venv/bin/python tools/workspace_backup.py create \
  --scope /absolute/workspace-backup-scope.json \
  --destination /absolute/archive-store/snapshot-20261015 \
  --progress --progress-interval 2 \
  > /absolute/archive-receipts/create-result.json \
  2> /absolute/archive-receipts/create-progress.jsonl
```

进度记录包含`schema_version=workspace-backup-progress-v1`、操作、阶段、已耗时和源租约释放状态。复制/哈希报告当前文件实际字节，SQLite快照报告实际页数，行数检查报告已完成表数；这些不是整次操作的统一百分比。完整性和领域检查等未知总量阶段只显示阶段及节流心跳，不推算剩余时间。默认间隔为2秒，`--progress-interval`允许0.1至60秒；阶段切换立即输出，同阶段进度和心跳按间隔节流。

心跳由遍历、读写、SQLite执行和领域校验检查点驱动；阻塞中的内核调用不会被后台线程强制打断，也不保证这段时间持续输出。进度不包含文件路径、表名或异常原文。启用进度后，失败JSON是标准错误的最后一行，另含`stage`、`error_code`和`source_leases_released`，便于定位失败阶段；解析器应逐行读取，不能将整段标准错误当作单个JSON。未启用进度时保留原来的单条失败JSON及成功时空标准错误。进度输出失败会传播，操作者仍应独立核对产物，不能仅凭曾收到的某一阶段认定成功。

## 3. 目录布局与完成标记

文件根保留原文件名；目录根的内容放在对应逻辑根下。以示例清单为例，备份及恢复目录都采用以下布局：

```text
snapshot-20261015/
  workspace-manifest.json
  payload/
    primary/ashare_radar.sqlite3
    fuyao/research.sqlite3
    fuyao/history/...
    ranking/...
    prospective/...
    control/...
    runtime/...
```

恢复不会将文件自动装回旧绝对路径。清单、数据库及研究文件中保存的原绝对路径不会改写；结果中的`roots[].restored_path`给出本次归档内位置。控制绑定、研究引用和运行环境可能需要按各自合同重新绑定与独立验证，不能移动目录后直接启动，也不能改写冻结计划/收据来伪造旧身份匹配。

目标目录先独占创建，初始权限为`0700`；文件写入及验证期间可见，但没有完成清单时属于未完成归档。创建和恢复均先验证目标文件、数据库及领域绑定，并复查文件身份，成功后才通过独占原子发布`workspace-manifest.json`形成完成标记。发布后重新核对清单身份；清单存在仍须通过`verify`，不能只凭目录或文件名判断可用。

发生异常、取消、磁盘写满或身份变化时，工具尝试清理本次拥有且身份仍匹配的文件和目录。并发替换后的其他对象不会被盲目删除。强制终止、断电、清理权限不足或无法证明对象身份可能留下目录；失败退出后不要自动把残留目录当作成功备份。核对身份后另行处理，重试使用新的目的目录。

## 4. 排除规则及领域检查的范围

默认按名称排除秘密配置、日志、运行锁、嵌套备份、普通生产虚拟环境及四类工具缓存。秘密规则包括`.env/.env.*`、`.aws/.ssh`、`credentials/secrets`名称、API key名称及`.key/.pem/.p12/.pfx`后缀；它不读取内容识别所有秘密。规则也检查显式根的祖先，具体规则见[容量说明](WORKSPACE_STORAGE.md#3-如何解释容量)。归档中的`exclusions`记录实际排除项，`coverage=explicit_roots_with_reported_exclusions`只承诺声明范围与这些排除。

`runtime`类别保留普通依赖和缓存，但不豁免秘密、日志、锁和备份规则。封存运行目录只要命中排除项，整次创建会失败；不会悄悄缺件后声称运行目录完整。例如依赖包里的`.pem`证书也可能命中名称规则。符号链接同样拒绝，不能为了打包而删除原环境文件或更改旧runtime清单。

领域验证限于归档内已知合同：主库已知排名引用、前瞻计划与收据链及其输入快照、扶摇当前版本文件，以及runtime清单与代码/环境文件摘要。一般研究文件仍可能只有文件完整性检查。它不重放全部业务合同、不核验所有数据库外部引用，也不证明研究结果成熟或取得执行资格。

封存运行目录的共享基础Python、标准库等外部依赖不随归档自动补齐；结果保留`runtime_execution_verified=false`及外部依赖未验证状态。检查这些归档不会执行冻结Python，恢复运行需另行使用相应受控流程验收。

## 5. 预算与磁盘空间

三个命令都支持以下参数，字节参数使用整数：

| 参数 | 默认值 | 可配置上限 |
| --- | --- | --- |
| `--max-files` | 100,000 | 1,000,000 |
| `--max-bytes` | 50 GiB（53,687,091,200字节） | 1 TiB |
| `--max-file-bytes` | 16 GiB（17,179,869,184字节） | 1 TiB |
| `--max-depth` | 32 | 128 |
| `--max-seconds` | 900秒 | 86,400秒 |

枚举条目和流式处理文件分别累计并受`max_files`限制；目录及排除项也占枚举预算。根、`payload`层级及清单文件会增加归档检查的条目数，因此不要把该值设为仅业务文件数。总字节和单文件限制检查实际负载，SQLite快照也有写入前及复制进度的字节准入。深度限制适用于实际遍历层级，恢复布局的`payload/<name>`层也需预留。

时间限制使用单调时钟，在遍历、流复制及数据库检查的检查点生效，不能保证中断任意阻塞中的内核调用。预算耗尽直接失败，不把部分备份报告为完成。清单另有16MiB、100,000个文件及100,000个目录等格式限制；领域验证还有独立的文件、解压后字节及深度上限，提高CLI预算不会取消这些保护。

字节预算不是磁盘空间预留。SQLite临时快照、恢复副本及校验过程可能需要额外空间，稀疏文件复制也可能增加实际分配量。创建前结合容量报告和可用磁盘空间安排目的位置；完成后独立保存摘要，并实际进行一次新目录恢复验证。
