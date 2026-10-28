"""Shared lexical continuation signals; not an API dependency."""
from typing import Final
_NEW_ASK_WORDS: Final[tuple[str, ...]] = (
    "为什么", "怎么", "如何", "变慢", "报错", "延迟", "错误率", "超时", "失败",
    "异常", "慢", "涨", "掉", "不可用", "多少", "排查", "根因", "帮我查", "看一下",
    "修复", "解决", "给出方案", "提供方案")
