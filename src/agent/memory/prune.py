"""超长工具结果的**裁剪**（压缩之前的固定动作）。

★ 同一件事只留一份实现

这条规则（超过上限 → 头 + `middle pruned` 标记 + 尾）原先只长在
"回取原生数据"那一个摘要器里。可它管的是**所有**超长块：一旦上下文里出现
第二个超长来源（会话历史、证据拼接、回放原文），各写一遍就会分叉 ——
两处的阈值、两处的标记文本，模型看到的"被剪掉多少"就对不上了。
所以规则搬到这里，回取那一侧改为**调用它**。

★ 为什么是"先裁"：压缩（把旧历史压成摘要）要先把待压缩的内容读进来。
  如果超长块不进这一道，压缩的输入本身就可能是几十万字符 ——
  那一次模型调用的成本与截断风险都不受控。裁剪是**压缩前的固定动作**，
  不是"需要时才做"的优化。

★★ 完整原文**留在本地副本**：裁掉的是**进上下文的那一份**，
  原始载荷按 `raw_ref` 落在本地（可精确回放）。裁剪不许变成数据丢失 ——
  所以留痕里必须写明"完整原文在本地副本、可按 raw_ref 回放"。
"""

from __future__ import annotations

from typing import Any, Final, Optional

__all__ = ["TOOL_RESULT_LIMIT", "PRUNE_HEAD", "PRUNE_TAIL", "PRUNE_MARK",
           "prune_tool_result", "describe_pruning"]

#: 工具结果进上下文的字符上限
TOOL_RESULT_LIMIT: Final[int] = 8192
#: 裁掉中间时保留的头部长度（看开头：结构、字段名、第一段）
PRUNE_HEAD: Final[int] = 4096
#: 裁掉中间时保留的尾部长度（看结尾：最后几行错误、收尾的括号）
PRUNE_TAIL: Final[int] = 1024
#: 中间那句标记里**必须逐字出现**的片段（模型据此知道自己看到的不是全文）
PRUNE_MARK: Final[str] = "middle pruned"


def prune_tool_result(text: str, *, limit: int = TOOL_RESULT_LIMIT,
                      head: int = PRUNE_HEAD, tail: int = PRUNE_TAIL) -> str:
    """超长就裁成 `头 + 标记行 + 尾`；没超长就**原样返回**（逐字，一个字符都不动）。

    ★ 按**字符**计数（不是字节）：中文一个字在 UTF-8 里占 3 字节，
      按字节算会把中文结果裁得只剩三分之一。
    ★ 没超长时**不许**加"已裁剪"之类的字样：模型会据此以为还有别的内容没看到。
    """
    clean = "" if text is None else str(text)
    cap = max(0, int(limit or 0))
    if len(clean) <= cap:
        return clean
    keep_head = max(0, int(head or 0))
    keep_tail = max(0, int(tail or 0))
    pruned = len(clean) - keep_head - keep_tail
    marker = f"\n... [{PRUNE_MARK} {pruned} chars] ...\n"
    return f"{clean[:keep_head]}{marker}{clean[-keep_tail:] if keep_tail else ''}"


def describe_pruning(original: Any, pruned: bool, *, raw_ref: str = "") -> str:
    """裁剪留痕（写明被裁多少、完整原文在本地副本、可按 `raw_ref` 回放）。"""
    total = max(0, int(original or 0))
    if not pruned:
        return f"全文进上下文（{total} 字符，未超过 {TOOL_RESULT_LIMIT} 字符上限，未裁剪）"
    reference = f"raw_ref={raw_ref}" if str(raw_ref or "").strip() else "（未给 raw_ref）"
    return (f"已裁：原始 {total} 字符 > 上限 {TOOL_RESULT_LIMIT}，只把"
            f"头 {PRUNE_HEAD} + 标记 + 尾 {PRUNE_TAIL} 放进上下文；"
            f"**完整原文在本地副本、可按 {reference} 精确回放**")
