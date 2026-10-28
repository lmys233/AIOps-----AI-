"""工具层 —— **Agent 能碰到外部世界的唯一通道**。

| 模块 | 职责 |
|---|---|
| `registry.py` | 9 个工具的**唯一声明处**（`ToolSpec` + 实现 + 裁剪器），导入即校验 |
| `executor.py` | `ToolExecutor.call()`：五道保护顺序不可变 + 审计 + 摘要化 |
| `schema_check.py` | 第 1 道保护：零依赖的最小 JSON Schema 校验器 |
| `semantics.py` | 第 3 道保护：防全量扫描 / 时间窗 ≤24h / 返回上限 |
| `rate_limit.py` | 第 4 道保护：按 `(角色, 工具)` 的令牌桶 |
| `summarize.py` | 第 5 步：结果摘要化（**硬上限 50 点**） |
| `sources.py` | 数据源适配器（Prometheus / Loki / Tempo / Postgres） |
| `handlers.py` | 9 个工具的实现（只取数，不管权限/审计/裁剪） |
| `errors.py` | 三类"结局"的类型化：没查成 / 查询被拒 / 主动拒绝 |

★ 使用姿势（**只有这一个入口**）::

    ctx = ToolContext(prometheus=PrometheusSource("http://127.0.0.1:9090"))
    executor = ToolExecutor(context=ctx, audit=InMemoryAuditSink())
    result = await executor.call("metrics_agent", "promql_read",
                                 {"query": 'db_pool_waiting_connections{service="order"}',
                                  "window": "30m"})

★ 权限矩阵在 `common/enums.py`（`AGENT_TOOL_MATRIX`）—— 不在这里再抄一份：
  "谁能调什么"是**安全口径**，必须只有一处可改。
"""

__all__: list[str] = []
