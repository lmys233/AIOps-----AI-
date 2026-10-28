"""意图识别层：生产插件为规则 → few-shot 向量 → 主力大模型。

| 模块 | 职责 |
|---|---|
| `rules.py` / `rules.yaml` | ① 规则前置：确定性、高危意图 100% 召回（**独立自洽**） |
| `slots.py` / `slots.yaml` | 槽位抽取：词表/别名/正则外置为数据，抽取逻辑在代码里 |
| `classifier.py` | **置信度校准** + **澄清判据**（猜错代价 × 概率 > 问一句成本） |
| `plugin.py` / `plugin_contract.py` | 可注入决策插件；不取遥测、不改权限 |
| `catalog*.py` / `management.py` | PG 意图目录、本地旁路缓存、CRUD 与版本校验 |
| `vector_index.py` / `bge*.py` | 示例 hash 增量预计算与独立 BGE CPU 进程 |
| `router.py` | 决策提议转已校验结论；槽位/澄清保持统一，无插件注入兼容旧链路 |

★ 本包**不导出**具体符号（`from agent.intent import ...` 请显式写到子模块）：
  意图层有几个同名概念（`probe`、`classify`），包级再导出会让
  `import agent.intent.router as R` 这类写法踩到"拿到的是函数还是模块"的坑
  （踩过一次：包级导出会让上面这种写法拿到函数而不是模块）。
"""

__all__: list[str] = []
