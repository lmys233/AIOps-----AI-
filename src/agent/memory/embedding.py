"""向量化接口与当前阶段的**离线**实现（供"向量路召回"使用）。

★ 诚实说明：现在的向量是**词法近似**，不是语义。

三路召回里的"向量路"必须有实现，但当前环境**没有** embedding 模型可用
（`.env` 里只有 chat 模型；DeepSeek 的 `/models` 列表也没有 embedding 端点
—— 这是实测过的供应商事实）。

所以这里分两层：

- **接口** `Embedder`（`embed(text) -> tuple[float, ...]`）：换模型只改注入，调用方零改动；
- **离线实现** `LexicalHashEmbedder`：字符 n-gram 哈希到固定维度 + L2 归一化。

★ `LexicalHashEmbedder` 抓到的是**词法相似**（错别字、词序、共享短语），
  **不是语义相似**（"连接池打满"与"获取连接超时"在它眼里关系不大）。
  它存在的意义是：**让"向量路"这条链路是通的、可测的、可替换的**，
  而不是假装我们已经有语义检索了。

★ 换真模型时要做的事只有一件：把 `MemoryService(embedder=...)` 换成真实实现，
  并把 `embedding_model` 字段写进记录（表里已经留了这一列 —— 向量模型换了以后，
  老向量与新向量**不可比**，必须能按模型名过滤）。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Final, Protocol, Sequence, runtime_checkable

__all__ = [
    "Embedder",
    "LexicalHashEmbedder",
    "EMBEDDING_DIM",
    "cosine_similarity",
    "LEXICAL_EMBEDDER_NAME",
]

#: 向量维度。★ 取 256 而不是 1536：这是**词法哈希**向量，维度高了只是更稀疏
#: （哈希碰撞少了，但距离分布的区分度提升有限），而 pgvector 的存储与索引成本线性增长。
EMBEDDING_DIM: Final[int] = 256

#: 写进记忆记录的模型名（换模型后老向量不可比，必须能按它过滤）
LEXICAL_EMBEDDER_NAME: Final[str] = "lexical-hash-ngram-v1"

#: 字符 n-gram 的 n：2~4 覆盖中英文短词与短语片段
_NGRAM_RANGE: Final[tuple[int, ...]] = (2, 3, 4)


@runtime_checkable
class Embedder(Protocol):
    """向量化接口（换实现 = 换模型，调用方不动）。"""

    #: 模型标识（进记忆记录）
    name: str
    #: 维度（进建表 DDL 与校验）
    dim: int

    def embed(self, text: str) -> tuple[float, ...]:  # pragma: no cover - 协议
        ...


@dataclass(frozen=True)
class LexicalHashEmbedder:
    """字符 n-gram 哈希向量（**确定性、离线、零依赖**）。

    ★ 确定性很重要：同一个文本永远得到同一个向量 ——
      否则"写入时算一次、召回时再算一次"就会对不上，而那种错**很难发现**
      （召回率莫名偏低，却看不出哪一步错了）。
    """

    dim: int = EMBEDDING_DIM
    name: str = LEXICAL_EMBEDDER_NAME

    def _buckets(self, text: str) -> list[int]:
        """把文本拆成字符 n-gram，并哈希到 `[0, dim)`。"""
        cleaned = " ".join(str(text or "").lower().split())
        if not cleaned:
            return []
        out: list[int] = []
        for size in _NGRAM_RANGE:
            if len(cleaned) < size:
                continue
            for start in range(len(cleaned) - size + 1):
                gram = cleaned[start:start + size]
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                out.append(int.from_bytes(digest, "big") % self.dim)
        # 再加上整串的哈希：短文本（如 "order 超时"）n-gram 太少，靠它拉开区分度
        whole = hashlib.blake2b(cleaned.encode("utf-8"), digest_size=8).digest()
        out.append(int.from_bytes(whole, "big") % self.dim)
        return out

    def embed(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.dim
        for bucket in self._buckets(text):
            vector[bucket] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return tuple(vector)
        return tuple(round(value / norm, 6) for value in vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度（两侧都归一化过时就是点积）。长度不一致 → 抛，不静默截断。"""
    if len(left) != len(right):
        raise ValueError(
            f"向量维度不一致（{len(left)} vs {len(right)}）—— "
            f"换过 embedding 模型的老向量不能直接比，必须按 `embedding_model` 过滤"
        )
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))
