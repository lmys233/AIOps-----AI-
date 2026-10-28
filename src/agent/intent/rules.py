"""第一级：确定性规则前置（高危意图 100% 召回）。

--------------------------------------------------------------------------------
★ 规则**存在 `rules.yaml` 里**，这个文件只负责"怎么用规则"
--------------------------------------------------------------------------------

| 放哪 | 内容 | 为什么 |
|---|---|---|
| `rules.yaml` | 命中什么词、权重多少、每条规则的采信门槛 | 这是**数据**，会随真实语句不断增补；放代码里每次加同义词都要改 .py，于是没人愿意加 |
| 本文件 | 顺序即优先级、重叠去重、饱和归一、兜底语义、**红线校验** | 这是**语义/红线**：一旦能改，就能把"写请求"压到只读意图后面 —— 那是自动提权 |

★ 一句话：**外置"证据表"，不动"裁决逻辑"。**

--------------------------------------------------------------------------------
★ 三条设计要点（讲清"为什么"，避免以后被"顺手改成更简单的写法"）
--------------------------------------------------------------------------------

1. **高危意图 100% 召回**：`REMEDIATION` 是唯一解锁写能力的意图。
   规则层要求它的 gate ≥ 高危锁定阈值（0.60），保证"命中 ⇒ 定案"，
   **不交给模型翻盘** —— 模型最可能的错法就是把"重启 order"理解成
   "查 order 的重启记录"，那是危险方向的降级。

2. **MECE 兜底**：任何输入都必须落到 6 类之一，兜底是 `OUT_OF_SCOPE`。
   ★ 兜底的**方向**是刻意的：宁可把业务问题误判成越界（用户再问一次），
   也不要把越界输入误判成业务意图（会触发一堆无意义的取证与工具调用）。

3. **可解释**：`RuleHit.matched` 存**命中的原文片段**。
   没有它，`confidence_raw` 就是凭空冒出来的数字，校准与排障都无从下手。

★ 权重是**证据强度**，不是概率：`score = min(1.0, 命中权重和 / 2.0)`。
  之后由 `classifier.py` 的校准表修成 `confidence`（两者必须分开）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Optional

import yaml

from ..common.enums import Intent
from .scopes import FaultScope, parse_scope

__all__ = [
    "RuleHit",
    "Pattern",
    "RuleSpec",
    "RuleSet",
    "RuleConfigError",
    "match_rules",
    "is_decided_by_rules",
    "load_rules",
    "parse_rules",
    "validate_rules",
    "reload_rules",
    "ACTIVE_RULES",
    "RULES_FILE",
    "RULE_DECIDE_THRESHOLD",
]

#: 规则分 ≥ 它 → **直接采信**，不调小模型
RULE_DECIDE_THRESHOLD: Final[float] = 0.70


#: 归一化饱和值：命中权重和 ÷ 它，再截到 1.0。
_SATURATION: Final[float] = 2.0

_SUPPORTED_VERSION: Final[int] = 1
_DEFAULT_FALLBACK_ID: Final[str] = "R6_fallback"

#: 规则文件位置
RULES_FILE: Final[Path] = Path(__file__).with_name("rules.yaml")


class RuleConfigError(Exception):
    """
    规则文件不合法 —— **必须 fail-fast**。
    """


@dataclass(frozen=True)
class Pattern:
    """一条模式：编译好的正则 + 权重 + 人类可读的依据。"""

    regex: re.Pattern[str]
    weight: float
    note: str = ""

    def __str__(self) -> str:
        return f"{self.regex.pattern!r}(w={self.weight})"


@dataclass(frozen=True)
class RuleSpec:
    """一条规则：谁（intent）、什么门槛（gate）、靠哪些证据（patterns）。"""

    rule_id: str
    intent: Intent
    gate: float
    patterns: tuple[Pattern, ...]
    scope: Optional[FaultScope] = None


@dataclass(frozen=True)
class RuleSet:
    """一整张规则表 + 兜底配置（`source` 便于日志说清"这次用的是哪份规则"）。"""

    rules: tuple[RuleSpec, ...]
    fallback_rule_id: str
    fallback_score: float
    #: `mutex.imperative_write` 编译后的正则（见 `rules.yaml` 顶部说明）。
    #: ★ `parse_rules` **必须**从文件里取到它：取不到就拒绝启动（`RuleConfigError`），
    #:   代码里**不留**第二份词表 —— 留一份"兜底"就等于把漂移又装回去。
    imperative_write: re.Pattern[str]
    source: str = "<memory>"


@dataclass(frozen=True)
class RuleHit:
    """规则层的一次判定结果（**可解释**是它的核心价值）。"""

    intent: Intent
    score: float
    rule_id: str
    matched: tuple[str, ...]

    @property
    def is_fallback(self) -> bool:
        """
        是不是"规则层毫无线索"的兜底提议。
        """
        return self.rule_id == _DEFAULT_FALLBACK_ID

    def __str__(self) -> str:
        hit = "/".join(self.matched) or "（无）"
        return (f"{self.intent.value}(score={self.score:.2f}, "
                f"rule={self.rule_id}, 命中={hit})")


# ======================================================================
#  解析与校验
# ======================================================================
def _as_float(raw: Any, where: str, field: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise RuleConfigError(f"{where} 的 {field} 不是数字：{raw!r}") from exc


def parse_rules(text: str, *, source: str = "<string>") -> RuleSet:
    """把 YAML 文本解析成 `RuleSet`。"""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleConfigError(f"{source} 不是合法 YAML：{exc}") from exc
    if not isinstance(doc, dict):
        raise RuleConfigError(f"{source} 顶层必须是映射（mapping）")

    version = doc.get("version")
    if version != _SUPPORTED_VERSION:
        raise RuleConfigError(
            f"不支持的规则文件版本 {version!r}（本实现只认 {_SUPPORTED_VERSION}）—— "
            f"版本号是为了将来改结构时能明确拒绝旧文件，而不是猜着读"
        )

    raw_rules = doc.get("rules")
    if not isinstance(raw_rules, list):
        raise RuleConfigError(f"{source} 的 rules 必须是列表")

    rules: list[RuleSpec] = []
    for index, raw in enumerate(raw_rules):
        where = f"{source} 第 {index + 1} 条规则"
        if not isinstance(raw, dict):
            raise RuleConfigError(f"{where} 必须是映射")
        rule_id = str(raw.get("id") or "").strip()
        if not rule_id:
            raise RuleConfigError(f"{where} 缺少 id")
        intent_raw = str(raw.get("intent") or "").strip()
        try:
            intent = Intent(intent_raw)
        except ValueError as exc:
            raise RuleConfigError(
                f"{where} 的 intent {intent_raw!r} 不在 6 类内"
            ) from exc
        gate = _as_float(raw.get("gate"), where, "gate")

        raw_patterns = raw.get("patterns")
        if not isinstance(raw_patterns, list) or not raw_patterns:
            raise RuleConfigError(f"{where} 的 patterns 必须是非空列表")
        patterns: list[Pattern] = []
        for p_index, raw_pattern in enumerate(raw_patterns):
            p_where = f"{where} 的第 {p_index + 1} 条 pattern"
            if not isinstance(raw_pattern, dict):
                raise RuleConfigError(f"{p_where} 必须是映射")
            expr = str(raw_pattern.get("pattern") or "")
            if not expr:
                raise RuleConfigError(f"{p_where} 缺少 pattern")
            try:
                compiled = re.compile(expr)
            except re.error as exc:
                raise RuleConfigError(f"{p_where} 正则非法：{exc}") from exc
            patterns.append(Pattern(
                regex=compiled,
                weight=_as_float(raw_pattern.get("weight"), p_where, "weight"),
                note=str(raw_pattern.get("note") or ""),
            ))
        try:scope=parse_scope(raw.get('scope'))
        except ValueError as exc:raise RuleConfigError(f'{where}: {exc}') from exc
        rules.append(RuleSpec(rule_id=rule_id, intent=intent, gate=gate,
                              patterns=tuple(patterns),scope=scope))

    raw_fallback = doc.get("fallback") or {}
    if not isinstance(raw_fallback, dict):
        raise RuleConfigError(f"{source} 的 fallback 必须是映射")

    # ---- 互斥判据：**必须**来自文件 ----
    raw_mutex = doc.get("mutex")
    if not isinstance(raw_mutex, dict):
        raise RuleConfigError(
            f"{source} 缺少 `mutex:` 段 —— 知识型问句与祈使式写动作的互斥判据"
            f"（`mutex.imperative_write`）必须在规则文件里给出来："
            f"它是一份**词表**，写在代码里就会与 YAML 的动作词表各自漂移"
        )
    mutex_expr = str(raw_mutex.get("imperative_write") or "").strip()
    if not mutex_expr:
        raise RuleConfigError(f"{source} 的 `mutex.imperative_write` 为空")
    try:
        imperative_write = re.compile(mutex_expr)
    except re.error as exc:
        raise RuleConfigError(
            f"{source} 的 `mutex.imperative_write` 正则非法：{exc}") from exc

    ruleset = RuleSet(
        rules=tuple(rules),
        fallback_rule_id=str(raw_fallback.get("rule_id") or _DEFAULT_FALLBACK_ID).strip(),
        fallback_score=_as_float(raw_fallback.get("score"), source, "fallback.score"),
        imperative_write=imperative_write,
        source=source,
    )
    validate_rules(ruleset)
    return ruleset


def validate_rules(ruleset: RuleSet) -> None:
    """校验规则表的不变量。任何一条不过 → `RuleConfigError`（拒绝启动）。

    ★ 这里查的都是"改坏了不会报错、只会静默判错"的东西 ——
      它们没法靠 review 保证，只能靠代码拦。
    """
    if not ruleset.rules:
        raise RuleConfigError("规则表为空 —— 至少要有 REMEDIATION 那条（高危必须 100% 召回）")

    ids = [rule.rule_id for rule in ruleset.rules]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise RuleConfigError(f"规则 id 重复：{duplicated}（排障会指向错误的那条）")
    if ruleset.fallback_rule_id in ids:
        raise RuleConfigError(
            f"兜底 rule_id {ruleset.fallback_rule_id!r} 与某条规则重名 —— is_fallback 会误判"
        )
    if not 0.0 <= ruleset.fallback_score < 1.0:
        raise RuleConfigError(f"兜底分必须在 [0,1)，实际 {ruleset.fallback_score}")

    for rule in ruleset.rules:
        if not 0.0 < rule.gate <= 1.0:
            raise RuleConfigError(f"{rule.rule_id}: gate 必须在 (0,1]，实际 {rule.gate}")
        if any(pattern.weight <= 0 for pattern in rule.patterns):
            raise RuleConfigError(f"{rule.rule_id}: 存在 weight ≤ 0 的模式（它永远不贡献证据）")
        # ★ 语义序：兜底必须比任何一条规则的采信门槛都低
        if ruleset.fallback_score >= rule.gate:
            raise RuleConfigError(
                f"兜底分 {ruleset.fallback_score} 不得 ≥ {rule.rule_id} 的 gate {rule.gate}"
                f" —— 否则'兜底 = 证据最弱'这个语义序被破坏"
            )

    # ★ 兜底不得达到决策阈值，否则规则层会对"完全没线索"的输入直接定案
    if ruleset.fallback_score >= RULE_DECIDE_THRESHOLD:
        raise RuleConfigError(
            f"兜底分 {ruleset.fallback_score} 不得达到决策阈值 {RULE_DECIDE_THRESHOLD} —— "
            f"否则规则层会对越界输入「直接定案」，模型级再也没机会翻盘"
        )

    # ★★ 红线：必须有一条 REMEDIATION 规则。
    #   "命中即定案"由 `is_decided_by_rules()` 保证（不看分数），所以这里只查
    #   "有没有这条规则 + 它至少有一条强模式"。
    remediation = [r for r in ruleset.rules if r.intent is Intent.REMEDIATION]
    if not remediation:
        raise RuleConfigError(
            "缺少 intent=REMEDIATION 的规则 —— 写意图必须 100% 召回，"
            "不能只靠模型（模型会把『重启 order』降级成『查重启记录』）"
        )
    for rule in remediation:
        if max(p.weight for p in rule.patterns) < 1.0:
            raise RuleConfigError(
                f"{rule.rule_id}: 至少要有一条强模式（weight ≥ 1.0）—— "
                f"全是弱词的话，写请求会被判成兜底"
            )


def load_rules(path: Optional[Path] = None) -> RuleSet:
    """从文件加载规则表。

    ★ 文件不存在时**刻意不回退到"代码里的内置默认副本"**：
      规则表只能有一份事实来源，代码里再存一份副本迟早漂移
      （本项目头号教训：同一事实有两份就一定会漂移）。
      真缺文件就 fail-fast —— 它随代码走，不该缺。
    """
    rules_path = Path(path) if path is not None else RULES_FILE
    try:
        text = rules_path.read_text(encoding="utf-8-sig")     # utf-8-sig：带 BOM 也能读
    except FileNotFoundError as exc:
        raise RuleConfigError(f"规则文件不存在：{rules_path}") from exc
    except OSError as exc:
        raise RuleConfigError(f"读不了规则文件 {rules_path}：{exc}") from exc
    return parse_rules(text, source=str(rules_path))


#: 进程启动时加载并校验（坏规则 → **import 就炸**，不会带病运行）
ACTIVE_RULES: RuleSet = load_rules()


def reload_rules(path: Optional[Path] = None) -> RuleSet:
    """重新加载并替换生效规则表（运维/测试用）。

    ★ 单独提供它，是为了让将来的"候选规则 → 评测 → 生效"流程
      不必重启进程，也不必改代码。
    """
    global ACTIVE_RULES
    ACTIVE_RULES = load_rules(path)
    return ACTIVE_RULES


# ======================================================================
#  匹配
# ======================================================================
def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    """新区间是否与已计分的区间重叠（同一段文字不重复计权）。

    ★ 为什么需要它：`把 order 重启一下` 会同时命中"祈使式写动作(1.4)"和
      "裸动作词(0.6)" —— **同一段文字**被计两次权重，分数虚高。
      去重后，"分数 = 命中了几个**不同位置**的证据"，语义才对得上。
    """
    start, end = span
    return any(start < other_end and other_start < end
               for other_start, other_end in taken)


def _score_rule(message: str, patterns: tuple[Pattern, ...]) -> tuple[float, tuple[str, ...]]:
    total = 0.0
    hits: list[str] = []
    taken: list[tuple[int, int]] = []
    for pattern in patterns:
        found = pattern.regex.search(message)
        if found is None:
            continue
        if _overlaps(found.span(), taken):
            continue                      # 与已计分证据重叠 → 不重复计权
        taken.append(found.span())
        total += pattern.weight
        hits.append(found.group(0))
    return min(1.0, total / _SATURATION), tuple(hits)


def match_rules(message: str, *, ruleset: Optional[RuleSet] = None, slots_config=None) -> RuleHit:
    """对一句话做规则判定。**永远有返回**（兜底是 OUT_OF_SCOPE）。

    Args:
        message: 用户原始输入。
        ruleset: 用哪张规则表；``None`` = 生效中的那张（``ACTIVE_RULES``）。
            ★ 允许显式传入，是为了让测试能验证"换一份规则文件行为就变"，
              而不必去改全局状态。

    Returns:
        命中的 `RuleHit`。没命中任何规则时返回 `OUT_OF_SCOPE`，分数只有
        `fallback_score`（默认 0.20）—— 这样模型级仍有充分理由翻盘。
    """
    active = ruleset or ACTIVE_RULES
    text = (message or "").strip()
    if not text:
        return RuleHit(Intent.OUT_OF_SCOPE, active.fallback_score,
                       active.fallback_rule_id, ())

    for rule in active.rules:
        score, matched = _score_rule(text, rule.patterns)
        if score < rule.gate:
            continue
        if rule.scope is not None and not rule.scope.matches(text,slots_config):
            continue

        # ★ 知识型问句 vs 祈使式写动作的**互斥**处理：
        #   "我们的扩容流程是什么" → 在问流程（KNOWLEDGE_QA），不是要扩容；
        #   "把 order 扩容到 5 副本" → 要动手（REMEDIATION）。
        #   ★ 判据用 **intent** 而不是规则 id：id 现在能在文件里改，
        #     代码里硬编码某个 id 等于把文件格式焊死。
        if rule.intent is Intent.KNOWLEDGE_QA and active.imperative_write.search(text):
            continue

        return RuleHit(rule.intent, score, rule.rule_id, matched)

    return RuleHit(Intent.OUT_OF_SCOPE, active.fallback_score,
                   active.fallback_rule_id, ())


def is_decided_by_rules(hit: RuleHit) -> bool:
    """规则层的判定是否"足以定案"（决定要不要调用模型）。

    ★ 两条判据语义不同：

      · **高危意图（REMEDIATION）：命中即定案**，不再比分数阈值。
        "命中"本身已经过了它自己的 gate（弱信号在那一步就筛掉了），
        而让模型参与只会往"更良性"的方向改 —— 那正是危险方向
        （模型会把『重启 order』读成『查 order 的重启记录』）。

      · 其它意图：分够高（`RULE_DECIDE_THRESHOLD`）才定案 —— 为省 token。

    ★ 注意别把这里改成"抬高 REMEDIATION 的 gate"来实现同样效果：
      gate 一高，"回滚一下"这类弱写信号就会掉进兜底（变成礼貌拒答），
      用户明明要动生产却被拒答 —— 那比多问一次审批糟得多。
    """
    if hit.is_fallback:
        return False
    if hit.intent is Intent.REMEDIATION:
        return True
    return hit.score >= RULE_DECIDE_THRESHOLD
