"""记忆层（四层记忆 + `MemoryService`）。

| 模块 | 职责 |
|---|---|
| `service.py` | `MemoryService`：`recall` / `write` / `feedback` + `migrate()` |
| `gates.py` | **五道写入闸门**（顺序不可变；拦下 → `WriteResult(skipped=True)`，不抛异常） |
| `recall.py` | 读取六步：查询改写 → 三路并行召回 → RRF → 多维加权 → 精排 → 带元数据注入 |
| `stores/` | 四层存储（Redis 工作 / PG 情景 / TopologyStore 语义 / PG 程序性）+ 内存假实现 |
| `session.py` | 会话历史 + **指代消解** + 澄清记账 |
| `tiers.py` | **三层切分**：近层逐字原文 / 中层待摘要 / 远层转索引（一条不丢） |
| `index.py` | **远层索引**：有界预览 + 稳定分页 + 按时间可查 |
| `compaction.py` | 上下文压力（计量器）/ 自动压缩（一次模型请求，替换不追加）/ 遗忘（TTL + 条数上限） |
| `prune.py` | **超长工具结果先裁**（头 + `middle pruned` + 尾；完整原文留在本地可回放） |
| `embedding.py` | `Embedder` 接口 + 当前的**离线词法哈希**实现（诚实标注：不是语义） |
| `schema.sql` | 记忆层的表（**不属于存储契约**，Agent 自行设计并迁移） |

★ 三条不变量（与工具层同源）：

1. **"没查成" ≠ "没命中"**：前者抛 `MemoryUnavailable` 并进 `degraded_paths`，
   后者是正常空结果。混起来 = 把"记忆库连不上"读成"没有相似案例"。
2. **写入永不抛异常**：闸门拦截、影子回归熔断、存储失败，全部走 `WriteResult`。
3. **注入必须带元数据**：时间 / 验证方式 / 命中次数 / 是否被取代 —— 缺了模型会把
   "三年前一条没被验证过的旧结论"当成既定事实。

★ 本包**不在这里 import 子模块**（`__all__` 只是**清单**，不是再导出）：
   工具层要用的裁剪规则长在 `prune.py`，若在包初始化时把它连同整条记忆链
   一起拉起来，`tools` ↔ `memory` 就会互相等（导入期死锁）。
   要用哪个模块就直接 `from agent.memory.xxx import ...`。
"""

__all__: list[str] = [
    "NEAR_SHARE",
    "FAR_PREVIEW_CHARS",
    "TierPlan",
    "plan_tiers",
    "IndexEntry",
    "index_entries",
    "page_entries",
    "search_entries",
    "COMPACT_AT",
    "MAX_FAR_ENTRIES",
    "FORGET_TTL_SEC",
    "CompactionResult",
    "pressure",
    "should_compact",
    "compact_history",
    "forget",
    "TOOL_RESULT_LIMIT",
    "PRUNE_HEAD",
    "PRUNE_TAIL",
    "prune_tool_result",
    "describe_pruning",
]
