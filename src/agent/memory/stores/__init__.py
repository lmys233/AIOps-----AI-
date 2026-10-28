"""四层记忆的存储层。

| 层 | 内容 | 落点 | 本文件里的类 |
|---|---|---|---|
| **工作记忆** | 本次排查的黑板/时间轴/澄清记账 | **Redis**，会话级 TTL 24h | `WorkingMemory` / `InMemoryWorkingMemory` |
| **情景记忆** | 历史故障：现象→证据→根因→处置→复盘 | Postgres + pgvector，**带时间衰减** | `SqlEpisodicStore` / `InMemoryEpisodicStore` |
| **语义记忆** | 服务拓扑、Owner、SLO | **`TopologyStore` 接口** | `SemanticMemory` |
| **程序性记忆** | 排查套路 / SOP，按成功率自我进化 | Postgres + pgvector | `SqlProcedureStore` / `InMemoryProcedureStore` |

★ 三条设计约束：

1. **每一层都能注入**（真实现 / 内存假实现同签名）：离线单测不需要 Redis/Postgres，
   而真链路仍然跑真存储 —— 这是本项目一贯的"可注入即可测"。
2. **语义记忆不另建表**：拓扑的唯一来源是 `TopologyStore`（"同一事实只留一份"）。
   在记忆层再存一份服务目录，就是第二个副本，必然过期。
3. **"没查成"与"查了没命中"必须分开**：前者抛 `MemoryUnavailable`，
   后者返回空列表。混起来的后果与工具层完全一样 ——
   把"记忆库连不上"读成"没有相似案例"，于是**放弃一条本该有用的线索**。"""


# ----------------------------------------------------------------------
#  本包由原先的 `stores.py`（903 行）拆分而来。
#  ★ 下面的再导出**就是对外契约**：`from agent.memory.stores import SqlEpisodicStore`
#    之类的写法一个字都不用改。
# ----------------------------------------------------------------------
from .base import (PROCEDURE_ID_PREFIX, SEMANTIC_ID_PREFIX, SESSION_KEY_PREFIX,
                   WORKING_TTL_SEC, MemoryUnavailable, bare_procedure, hit_layer)
from .episodic import (EpisodicStore, InMemoryEpisodicStore, SqlEpisodicStore)
from .procedure import InMemoryProcedureStore, ProcedureStore, SqlProcedureStore
from .records import (ConversationTurn, EpisodicRecord, ProcedureRecord, SemanticFact)
from .semantic import SemanticMemory, TopologySemanticMemory
from .working import InMemoryWorkingMemory, RedisWorkingMemory, WorkingMemory

__all__ = [
    "MemoryUnavailable",
    "EpisodicRecord",
    "ProcedureRecord",
    "SemanticFact",
    "ConversationTurn",
    "WorkingMemory",
    "InMemoryWorkingMemory",
    "RedisWorkingMemory",
    "EpisodicStore",
    "InMemoryEpisodicStore",
    "SqlEpisodicStore",
    "ProcedureStore",
    "InMemoryProcedureStore",
    "SqlProcedureStore",
    "SemanticMemory",
    "TopologySemanticMemory",
    "WORKING_TTL_SEC",
    "SESSION_KEY_PREFIX",
    "PROCEDURE_ID_PREFIX",
    "SEMANTIC_ID_PREFIX",
    "hit_layer",
    "bare_procedure",
]
