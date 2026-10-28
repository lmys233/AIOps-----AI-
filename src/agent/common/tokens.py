"""token 的**粗估**（只用来判断"内容装不装得下"）。

★ 它是估算，不是分词器：真正的分词要跟着模型走（不同模型的切分各不相同），
  而这里要做的是**装箱**（近层切到哪儿、预览留多长）——
  那件事只需要一个**有界、单调、可复现**的量，不需要与供应商的账一致。

★ 口径：CJK 字符 ≈ 1 token/字，其余字符 ≈ 1 token / 4 字符（`CHARS_PER_TOKEN_OTHER`）。
  于是"中文一句话"不会被低估成四分之一个字，"一段 JSON"也不会被高估。

单次上下文压力也使用此估算（含schema与输出预留），不使用累计花费。
供应商真实用量单独计量；该启发式不等同于供应商分词器或硬上下文保证。
"""

from __future__ import annotations

from typing import Final

__all__ = ["CHARS_PER_TOKEN_OTHER", "estimate_tokens"]

#: 非 CJK 字符每多少个算 1 个 token（英文约 4 字符/token）
CHARS_PER_TOKEN_OTHER: Final[int] = 4


def estimate_tokens(text: str) -> int:
    """估算一段文本占用多少 token（空串 → 0；有内容 → **至少 1**）。

    ★ 返回值只用于"装不装得下"：它必须**单调**（更长或更多的文本不会算出更小），
      否则按它切分会切出"越切越长"的结果。
    """
    clean = str(text or "")
    if not clean:
        return 0
    cjk = sum(1 for char in clean if _is_cjk(char))
    other = len(clean) - cjk
    packed = other // CHARS_PER_TOKEN_OTHER + (1 if other % CHARS_PER_TOKEN_OTHER else 0)
    return max(1, cjk + packed)


def _is_cjk(char: str) -> bool:
    """这个字符算不算"一字一 token"的 CJK 字符（汉字 + 全角标点 + 假名 + 谚文）。"""
    point = ord(char)
    return (0x3040 <= point <= 0x30FF or 0x3400 <= point <= 0x4DBF
            or 0x4E00 <= point <= 0x9FFF or 0xAC00 <= point <= 0xD7AF
            or 0xF900 <= point <= 0xFAFF or 0xFF00 <= point <= 0xFFEF)
