# 个人实验概率

“历史模型概率筛选与实验排序”是用户主动开启的独立研究视图，刷新后默认关闭。它不修改生产 v5 排序，也不放宽正式概率过滤门槛；返回 `formal_filter_qualified=false`、`production_ranking_effect=none`。正式研究入口见 [研究指南](RESEARCH.md)。

## 目标与有效输入

| `prediction_kind` | 固定目标 |
| --- | --- |
| `close_d1` / `close_d2` / `close_d5` | D 日已完成的 qfq 收盘至固定 D+1／D+2／D+5 收盘是否严格上涨；平盘为否，不含费用 |
| `net_h5` | D+1 开盘至 D+6 收盘，扣除声明成本后的绝对净收益是否为正 |

这些目标不能互换：D+5 方向不是 H5 执行净收益，绝对盈利也不是跑赢市场。方向预测不是可成交收益承诺。

模型使用 11 个 OHLCV 特征。当前输入必须是截至 D 的同一 qfq 合同、连续固定会话上的 21 根已完成日 K；盘中使用上一完整交易日。日期缺失时不能拿更早一根替代，混合价格合同、零信号成交量或超出训练特征 8 个标准差的输入不生成概率，并保留原因。

当前信号必须晚于所有训练／校准标签，模型最后标签最多滞后 90 个自然日；仅修改生成时间不能延长有效期。缺失、过期或损坏模型不会退回其他目标，也不补 0 或 50%。模型与输入经过原文／摘要校验，但本地摘要不是外部 PIT 认证。

方向模型至少使用 120 个训练日期及独立的 40 日 Platt 校准段，各目标分别 purge 1／2／5 日；H5 purge 6 日。拟合了校准器不代表独立测试有效。在线方向模型保留 `not_evaluated` 的样本外状态，不能借用 H5 或另行重拟合的 Choice 模型评估结果。

## 使用与构建

页面显式选择目标后才请求实验接口。API 必须确认实验性质，未确认返回 422：

```text
GET /api/market-scans/<RUN_ID>/experimental-probability?acknowledge_experimental=true&prediction_kind=close_d1&min_probability=0.4&sort=probability&page=1&page_size=50
```

UI 明确发送默认 D+1 目标；API／CLI 省略 `prediction_kind` 仍采用兼容的 `net_h5`，调用方应显式指定。此接口与正式 `/results?min_upside_probability=...` 不同，实验阈值不授予正式筛选资格。

从已核验静态研究输入构建，输出使用独立新目录；`--help` 可查看完整参数：

```bash
.venv/bin/python tools/build_experimental_probability.py \
  --prediction-kind net_h5 --source '<已核验历史重放.json>' \
  --output-dir '<新模型目录>'

ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0 TRADE_CALENDAR_AUTO_FETCH=0 \
.venv/bin/python tools/build_experimental_probability.py \
  --prediction-kind close_d1 --history-manifest '<Tencent历史manifest.json>' \
  --database '<匹配的静态qfq.sqlite3>' --output-dir '<新模型目录>'
```

方向目标可替换为 `close_d2` 或 `close_d5`。默认在线模型目录是 `data/research/personal_experimental_probability`，内容寻址文件分别使用 `experimental-h5`、`experimental-close-d1/d2/d5` 前缀；向默认目录构建会发布研究模型文件，应明确选择输出位置。

Choice 数据使用专用 `--choice-history-manifest`，必须显式提供与在线目录隔离的 `--output-dir`；转换、验证与回放步骤见 [Choice 研究](CHOICE_RESEARCH.md)。Choice 与 Tencent 的复权来源、股票池和采集时点不同，不得混用原始历史、校准器或评估结论。

服务在一致的只读快照中取得批次与特征，模型计算使用有界子进程。`min_probability`、分页和排序只处理已合格产生的实验预测；未知原因应一并查看，不能把有值股票子集当成全市场覆盖或事前收益证据。
