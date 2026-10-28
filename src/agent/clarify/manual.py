"""运维手册的**直出**。

★ 为什么不走模型：手册是**静态事实**（现象 / 确认命令 / 处理办法 / 错误码）。
  让模型"总结一下"会引入幻觉，而且**不可审计**（用户拿到的建议与文件里的不一样）。
  手册要求"原样返回该小节的文本，不经模型改写"。

★ 对账：手册里出现的错误码必须都是 `ErrorCode` 的真值 —— 由测试强制。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

__all__ = ["MANUAL_PATH", "OBSERVED_CODES", "load_manual", "sections",
           "advice_for", "section_titles"]

#: **被观测侧**（业务/SDK）的错误类型 —— 它们**不是** Agent 的 `ErrorCode`。
#: ★ 手册要对账的是"Agent 侧的错误码"，把业务侧类型混进来会让对账变成假的；
#:   所以这里**显式声明**，测试要求：手册里的每个大写 token 都得在
#:   `ErrorCode ∪ OBSERVED_CODES` 里（编一个不存在的码照样红）。
OBSERVED_CODES: tuple[str, ...] = ("POOL_INIT_FAILED",)

#: 手册位置（可用 `AIOPS_SDK_MANUAL` 覆盖，测试与部署都能指到别处）
_DEFAULT = Path(__file__).resolve().parents[3] / "docs" / "sdk-troubleshooting.md"


def MANUAL_PATH() -> Path:  # noqa: N802 —— 与"路径函数"的既有命名一致
    override = (os.environ.get("AIOPS_SDK_MANUAL") or "").strip()
    return Path(override) if override else _DEFAULT


def load_manual() -> str:
    """读手册原文；读不到 → 空串（调用方据此**如实降级**，不许编）。"""
    try:
        return MANUAL_PATH().read_text(encoding="utf-8")
    except OSError:
        return ""


def sections() -> dict[str, str]:
    """按 `## ` 切分成"小节标题 → 原文（含标题行）"。"""
    out: dict[str, str] = {}
    title: Optional[str] = None
    body: list[str] = []
    for line in load_manual().splitlines():
        if line.startswith("## "):
            if title is not None:
                out[title] = "\n".join(body).strip()
            title, body = line[3:].strip(), [line]
        elif title is not None:
            body.append(line)
    if title is not None:
        out[title] = "\n".join(body).strip()
    return out


def section_titles() -> tuple[str, ...]:
    return tuple(sections())


def advice_for(error_code: str) -> Optional[tuple[str, str]]:
    """按**错误码**找最合适的一节 → `(标题, 原文)`；找不到 → `None`（不猜）。"""
    code = str(error_code or "").strip()
    if not code:
        return None
    hits = [(title, body) for title, body in sections().items() if f"`{code}`" in body]
    if not hits:
        return None
    # 端口被占 / 没接 SDK 都是 TOOL_UNAVAILABLE：优先给"端口"那节（最常见的形态）
    for title, body in hits:
        if "端口" in title:
            return title, body
    return hits[0]
