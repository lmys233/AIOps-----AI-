"""写入闸门（五道，**顺序不可变**）。

```
① 验证方式分级（<0.7 直接拒写）
② 根因置信度 ≥ 0.70
③ 脱敏
④ 去重合并（相似度 ≥ 0.95）
⑤ 影子回归（Top-1 下降 > 0.03 → 熔断）
```

★ 为什么顺序不能变（每一对交换都会出事）：

- ①↔②：先看置信度再看验证方式，一条"根因分 0.9 但从没被验证过"的记忆会先过
  闸门②，白白算一次相似度与影子回归；
- ②↔③：脱敏前就把内容拼进日志/异常信息 → **敏感信息先泄露了才被拦**；
- ③↔④：拿**未脱敏**的文本算相似度，同一条记忆会因里面的手机号不同被判成"两条"，
  去重失效；
- ④↔⑤：先做影子回归再合并，回归跑的是"将要被合并掉"的那条新记录，结论没有意义。

★★ 最要紧的一条：**闸门拦下是正常业务流，不是错误** ——
  `write()` 必须返回 `WriteResult(skipped=True, reason=...)`，**不得抛异常**。
  "未验证的结论不进长期记忆"是设计意图，不是故障。

★ 影子回归（⑤）当前怎么落地（诚实说明）：

- **影子集** = 库里**已有的最近 N 条**情景记忆（拿它们的症状当查询）；
- **判据** = 写入前每条查询的 Top-1 是不是**它自己**，写入后是否仍然如此；
- Top-1 命中率下降 > 0.03 → **回滚**（删掉刚写的那条）并 `skipped=True`。

★ 这不是"假装有评测集"：它测的是**真实性质**（新记忆有没有把旧记忆挤下去），
  只是覆盖面窄（只覆盖库内自洽）。将来换成人工标注集时，只需替换 `shadow_set` 的取法。
"""

from __future__ import annotations

from typing import Any, Final, Optional, Sequence

import logging
import re
from dataclasses import dataclass, field

from ..common.enums import VERIFY_GATE_THRESHOLD, VERIFY_WEIGHTS, VerificationMethod

__all__ = [
    "GateOutcome",
    "GateReport",
    "redact_text",
    "DEFAULT_REDACT_PATTERNS",
    "SENSITIVE_KEYS",
    "is_sensitive_key",
    "REDACT_MASK",
    "ROOT_CAUSE_GATE",
    "DEDUPE_SIMILARITY",
    "SHADOW_TOP1_TOLERANCE",
    "gate_verification",
    "gate_root_cause",
    "gate_redaction",
    "gate_dedupe",
    "gate_shadow_regression",
]

logger = logging.getLogger("agent.memory.gates")

#: ② 根因置信度门槛
ROOT_CAUSE_GATE: Final[float] = 0.70
#: ④ 相似度 ≥0.95 视为同一条记忆
DEDUPE_SIMILARITY: Final[float] = 0.95
#: ⑤ Top-1 下降超过它就熔断
SHADOW_TOP1_TOLERANCE: Final[float] = 0.03

#: 脱敏模式。
#: ★★ 别把它和 SDK 侧 `changes.redact` 混为一谈：`changes_redact` 是 6 个**裸词**
#:   （用于按字段名打码 diff），这里是 7 条**正则**（用于正文替换）—— 机制与用途都不同。
#:   真正逐字相同的是 `sdk/redact.DEFAULT_REDACT_PATTERNS`（遥测脱敏用同一套正则），
#:   由单测断言两边相等（两个发布物各一份表，漂移当场红）。
DEFAULT_REDACT_PATTERNS: Final[tuple[str, ...]] = (
    r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+",       # password=xxx
    r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*\S+",  # token=xxx
    r"sk-[A-Za-z0-9]{8,}",                            # 供应商 key
    r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",                   # 邮箱
    r"\b1[3-9]\d{9}\b",                               # 手机号（中国大陆）
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b",                   # IPv4
    r"(?i)postgres(?:ql)?://\S+",                      # 带凭据的 DSN
)

#: ★★ **键名命中即整值打码**的表。
#:   模式表只能抓"值里带 `token=…`"的形态，抓不到 `{"token": "ghp_…"}` ——
#:   而处置步骤常被写成 `{"DB_PASSWORD": "…"}` / `{"authorization": "Basic …"}`，
#:   其中的凭据会被原样写进情景记忆（Postgres JSONB，**长期保存、还会被召回读回**）。
#:   ★ 与 `sdk/redact.SENSITIVE_KEYS` **逐字相同**，由单测断言两边相等
#:   （两个发布物各一份表，漂移当场红 —— 沿用 `DEFAULT_REDACT_PATTERNS` 的既有做法）。
SENSITIVE_KEYS: Final[tuple[str, ...]] = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "credentials", "dsn", "private_key",
)

#: 敏感片段的替换目标。★ 公开（`_redact_remediation` 也要用它，且与
#: `sdk/redact.REDACT_MASK` 同口径 —— 由单测断言相等）。
REDACT_MASK: Final[str] = "***"


def is_sensitive_key(key: object) -> bool:
    """键名像不像凭据（`apiKey` / `X-Api-Key` / `db_password` 都算）。

    ★ 与 `sdk/redact.is_sensitive_key` 同口径：小写后**子串**匹配。
    """
    lowered = str(key or "").lower()
    return any(mark in lowered for mark in SENSITIVE_KEYS)


@dataclass(frozen=True)
class GateOutcome:
    """单道闸门的结论。"""

    gate: str
    passed: bool
    reason: str = ""
    #: 闸门产出（例如 ③ 脱敏后的文本、④ 命中的已有 memory_id）
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateReport:
    """五道闸门的总报告（**被拦在哪一道**必须能一眼看出）。"""

    outcomes: tuple[GateOutcome, ...]

    @property
    def passed(self) -> bool:
        return all(outcome.passed for outcome in self.outcomes)

    @property
    def blocked_by(self) -> Optional[GateOutcome]:
        return next((o for o in self.outcomes if not o.passed), None)

    def reason(self) -> str:
        blocked = self.blocked_by
        if blocked is None:
            return ""
        return f"闸门 {blocked.gate} 拦下：{blocked.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "blocked_by": (self.blocked_by.gate if self.blocked_by else None),
                "reason": self.reason(),
                "gates": [{"gate": o.gate, "passed": o.passed, "reason": o.reason}
                          for o in self.outcomes]}


# ======================================================================
#  ① 验证方式分级
# ======================================================================
def gate_verification(verification: VerificationMethod | str) -> GateOutcome:
    """验证方式权重 <0.7 直接拒写（闸门 ①）。"""
    try:
        method = verification if isinstance(verification, VerificationMethod) \
            else VerificationMethod(str(verification))
    except ValueError as exc:
        return GateOutcome("①验证方式", False,
                           f"未知的验证方式 {verification!r}（4 个取值之一）")
    weight = VERIFY_WEIGHTS[method.value]
    if weight < VERIFY_GATE_THRESHOLD:
        return GateOutcome(
            "①验证方式", False,
            f"verification={method.value}（权重 {weight:.2f}）< 门槛 "
            f"{VERIFY_GATE_THRESHOLD:.2f} —— 未经可靠验证的结论不进长期记忆",
            {"weight": weight},
        )
    return GateOutcome("①验证方式", True, payload={"weight": weight})


# ======================================================================
#  ② 根因置信度
# ======================================================================
def gate_root_cause(score: Optional[float]) -> GateOutcome:
    """根因置信度 ≥0.70（闸门 ②）。"""
    if score is None:
        return GateOutcome("②根因置信度", False, "没有根因结论（score=None）—— 没定论就别写长期记忆")
    if float(score) < ROOT_CAUSE_GATE:
        return GateOutcome(
            "②根因置信度", False,
            f"根因 score={float(score):.2f} < {ROOT_CAUSE_GATE:.2f} —— 结论不够硬，不进长期记忆",
        )
    return GateOutcome("②根因置信度", True, payload={"score": float(score)})


# ======================================================================
#  ③ 脱敏
# ======================================================================
def redact_text(text: str, *, patterns: Sequence[str] = DEFAULT_REDACT_PATTERNS) -> tuple[str, int]:
    """按模式抹掉敏感片段。返回 `(脱敏后文本, 抹掉几处)`。"""
    out = str(text or "")
    hits = 0
    for pattern in patterns:
        try:
            out, count = re.subn(pattern, REDACT_MASK, out)
        except re.error as exc:  # noqa: PERF203
            logger.warning("脱敏正则非法（已跳过）：%s → %s", pattern, exc)
            continue
        hits += count
    return out, hits


def gate_redaction(*texts: Optional[str]) -> GateOutcome:
    """脱敏（闸门 ③）。

    ★ 即使**没有**命中任何模式也算通过 —— 闸门的语义是"经过脱敏处理"，
      不是"必须发现敏感信息"。但抹掉几处要记在 payload 里（可审计）。
    """
    redacted: list[str] = []
    total = 0
    for text in texts:
        if text is None:
            redacted.append("")
            continue
        cleaned, hits = redact_text(text)
        redacted.append(cleaned)
        total += hits
    return GateOutcome("③脱敏", True, payload={"texts": redacted, "redacted_count": total},
                       reason=(f"抹掉 {total} 处敏感片段" if total else ""))


# ======================================================================
#  ④ 去重合并
# ======================================================================
def gate_dedupe(existing_id: Optional[str], similarity: float) -> GateOutcome:
    """相似度 ≥0.95 → 合并到已有记忆（闸门 ④）。

    ★ 注意"合并"不是"丢弃"：新记录命中了旧案例，应该让**旧的 `hit_count` +1**
      （"这个故障又出现了一次"本身就是有价值的信息）。
      所以这里返回 `skipped=True` **但带上 memory_id**，调用方据此去 bump_hit。
    """
    if existing_id and float(similarity) >= DEDUPE_SIMILARITY:
        return GateOutcome(
            "④去重合并", False,
            f"与已有记忆 {existing_id} 相似度 {float(similarity):.3f} ≥ "
            f"{DEDUPE_SIMILARITY:.2f} —— 合并到它（hit_count+1），不新增一条",
            {"merged_into": existing_id, "similarity": float(similarity)},
        )
    return GateOutcome("④去重合并", True,
                       payload={"similarity": float(similarity)})


# ======================================================================
#  ⑤ 影子回归
# ======================================================================
def gate_shadow_regression(before: float, after: float) -> GateOutcome:
    """Top-1 命中率下降 >0.03 → 熔断（闸门 ⑤）。

    ★ 差值要先**按 9 位小数取整**再比：`1.0 - 0.97` 在二进制浮点里是
      `0.030000000000000027`，不取整就会把"**刚好**下降 0.03"判成熔断
      —— 边界上的浮点误差不该改变结论。
    """
    drop = round(float(before) - float(after), 9)
    if drop > SHADOW_TOP1_TOLERANCE:
        return GateOutcome(
            "⑤影子回归", False,
            f"写入后 Top-1 命中率从 {float(before):.3f} 掉到 {float(after):.3f}"
            f"（下降 {drop:.3f} > {SHADOW_TOP1_TOLERANCE:.2f}）—— 新记忆把旧记忆挤下去了，"
            f"已回滚该条写入",
            {"before": float(before), "after": float(after), "drop": drop},
        )
    return GateOutcome("⑤影子回归", True,
                       payload={"before": float(before), "after": float(after), "drop": drop})
