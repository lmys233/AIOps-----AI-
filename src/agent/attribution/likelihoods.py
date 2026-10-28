"""似然比表与先验权重 —— 归因层唯一的口径来源。

--------------------------------------------------------------------------------
★ 为什么这些数字**放在代码里**、而不是像 `rules.yaml` 那样外置
--------------------------------------------------------------------------------

一条既定边界：把代价表/校准系数放进 YAML，等于把它们当成**策略与判定口径**；
能被一次数据改动放宽的口径 = 后门。

似然比正是这种数字：把某条 LR 从 8.0 改成 0.8，就能让"变更引入"这类结论
从"强支持"变成"弱反对"，而**没有任何测试会红**。
所以它和 `classifier.MISTAKE_COST`、`enums.DEFAULT_ACTION_LEVEL` 一样留在代码里，
改动必须过 review。（输入词表仍归 YAML —— 那管的是"听懂什么"，这里管的是"怎么判"。）

--------------------------------------------------------------------------------
★ 口径来源
--------------------------------------------------------------------------------

  · 先验四权重 `0.45 / 0.25 / 0.20 / 0.10`：**加权平均**（和为 1.0，**不截断**）
  · 打分形式 `log Score = log Prior + Σ log LR` → softmax（连乘已废弃）
  · LR 的离散档位 `8.0 / 3.0 / 1.2 / 1.0 / 0.5 / 0.2`：本项目自定 ——
    LR 是"有这条证据 vs 没这条证据"的倍数，取整数倍便于人读
    （`log` 后是 `+2.08 / +1.10 / +0.18 / 0 / -0.69 / -1.61`，正数支持、负数反对）
  · 类别与信号词表：与 `attribution/categories.py` 的 `CATEGORY_SIGNALS`
    **同一套 8 大类**（"哪一路证据主导归因"），import 期校验一致性

--------------------------------------------------------------------------------
★★ 表必须自洽（否则拒绝启动）
--------------------------------------------------------------------------------

写错一张概率表的代价是"静默判错"，没有任何运行期异常会提醒你。
所以 `_validate()` 在 **import 期**就查：每个类别至少一条支持 + 一条反对 LR、
所有 LR > 0、类别集合与 `CATEGORY_SIGNALS` 完全一致。

★★ 方向也要自洽：证据方向有 **5 个取值**（`up/down/flat/spike/drop`），
而规则侧只需要"涨了 / 跌了 / 不限"三种。
两者必须**显式对齐**，否则 `spike`/`drop` 的证据会一条规则也命中不了 ——
`log_error_burst` 恒为 `SPIKE`，而它是 `error_burst` 的**必要条件**：
结果就是"必要条件在、支持证据却是空的"（报告自相矛盾，实链路已复现）。
口径：规则侧只写 `up` / `down` / `""`；证据侧的 `spike→up`、`drop→down`
由 :func:`rule_direction` 归一 —— **在打分这一处归一，证据本身仍保留 5 值原样展示**
（"一次爆发后回落"与"持续上升"是不同的事实，界面上不该抹平）。
"""

from __future__ import annotations

from typing import Final, Optional

from dataclasses import dataclass

from .categories import CATEGORY_SIGNALS

__all__ = [
    "LIKELIHOOD_TABLE",
    "PRIOR_WEIGHTS",
    "LR_LEVELS",
    "RULE_DIRECTIONS",
    "NON_DIRECTIONAL",
    "STRONG_LR",
    "SUPPORT_LR",
    "WEAK_LR",
    "NEUTRAL_LR",
    "AGAINST_LR",
    "STRONG_AGAINST_LR",
    "EvidenceRule",
    "likelihood_for",
    "rules_of",
    "category_of",
    "rule_direction",
    "is_rule_direction",
]

#: 似然比的离散档位（人读友好；取 log 后是 +2.08 / +1.10 / +0.18 / 0 / -0.69 / -1.61）
STRONG_LR: Final[float] = 8.0
SUPPORT_LR: Final[float] = 3.0
WEAK_LR: Final[float] = 1.2
NEUTRAL_LR: Final[float] = 1.0
AGAINST_LR: Final[float] = 0.5
STRONG_AGAINST_LR: Final[float] = 0.2

LR_LEVELS: Final[tuple[float, ...]] = (
    STRONG_LR, SUPPORT_LR, WEAK_LR, NEUTRAL_LR, AGAINST_LR, STRONG_AGAINST_LR,
)

#: 先验的四项权重（和为 1.0 的**加权平均**，不是截断累加）
PRIOR_WEIGHTS: Final[dict[str, float]] = {
    "change": 0.45,
    "memory": 0.25,
    "topology": 0.20,
    "base_rate": 0.10,
}

# ----------------------------------------------------------------------
#  ★★ 方向词汇的对齐
# ----------------------------------------------------------------------
#: 规则侧允许的方向：`""` = 不限方向（结构事实、变更事实用它）
RULE_DIRECTIONS: Final[tuple[str, ...]] = ("", "up", "down")

#: 证据侧"不涨不跌"的方向 —— 结构事实（依赖图/爆炸半径）与变更事实用它。
#  ★ 它**不**归一成 `""`：`""` 在规则侧的语义是"规则不限方向"，
#    若把 `flat` 也归一成 `""`，依赖图这类结构事实就会去"支持"上升类假设
#    —— 那个假根因（仅凭依赖图给出 topology_spread）正是这么来的。
#    `flat` 的正确行为是：**只**命中 `direction=""` 的规则，命中不了 `up`/`down` 规则。
NON_DIRECTIONAL: Final = "flat"

#: 证据侧（`EvidenceDirection`，五值）→ 规则侧三值。
#  ★ 为什么是这两个映射：
#    `spike`（峰值超阈值、末值已回落）与 `drop` 在"涨/跌"这一维上与 up/down 同类 ——
#    "出现过爆发"对"错误爆发"这类假设当然算支持证据；不归一的话，
#    `log_error_burst`（恒为 SPIKE）永远满足不了 `error_burst` 的**必要条件**。
_DIRECTION_ALIASES: Final[dict[str, str]] = {"spike": "up", "drop": "down"}


def rule_direction(direction: Optional[str]) -> str:
    """把**证据侧**方向归一成**规则侧**方向（未登记的原样返回，由 `_validate` 拦）。"""
    text = str(direction or "")
    return _DIRECTION_ALIASES.get(text, text)


def is_rule_direction(value: str) -> bool:
    """这个方向能不能被"带方向的规则"表达（`flat` 与空值不算）。"""
    return rule_direction(value) in RULE_DIRECTIONS


@dataclass(frozen=True)
class EvidenceRule:
    """一条证据规则：某类信号在某个方向上，对某类假设的似然比。

    字段与含义：

      · `signal`：信号名（子串匹配 `Evidence.signal`）
      · `direction`：规则侧口径：`up` / `down` / `""`（不限方向）。**只写这三个** ——
        证据侧的 `spike`/`drop` 由 :func:`rule_direction` 归一成 `up`/`down` 后再比
        （写别的值会被 `_validate()` 在 import 期拦下）
      · `lr`：似然比（**支持**该假设用 > 1，**反对**用 < 1）
      · `necessary`：这条证据是不是该假设的**必要条件**（缺了就强反证）
      · `note`：人话理由（进 `excluded_reason` / 报告）
    """

    signal: str
    lr: float
    direction: str = ""
    necessary: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"signal": self.signal, "lr": self.lr}
        if self.direction:
            out["direction"] = self.direction
        if self.necessary:
            out["necessary"] = True
        if self.note:
            out["note"] = self.note
        return out


# ----------------------------------------------------------------------
# ★ LR 表：类别 → 规则清单（与 `CATEGORY_SIGNALS` 的 8 大类一一对应）
# ----------------------------------------------------------------------
#: 设计口径（写清楚，免得被当成拍脑袋）：
#:   · **必要条件**（`necessary=True`）只给"缺了就不可能成立"的那一条：
#:     例如"连接池争用"必须看到池等待类指标；看不到就是强反证。
#:   · 支持证据按"有多直接"分档：直接量到该故障的指标用 STRONG，
#:     间接相关（例如 QPS 上涨对容量类）用 SUPPORT/WEAK。
#:   · **反对证据**同样重要：一个假设必须能被反证，否则 Critic 无事可做。
LIKELIHOOD_TABLE: Final[dict[str, tuple[EvidenceRule, ...]]] = {
    "pool_contention": (
        EvidenceRule("pool_waiting", STRONG_LR, "up", necessary=True,
                     note="池等待数上升 —— 连接池争用的直接证据（真实信号名 pool_waiting）"),
        EvidenceRule("pool_acquire", SUPPORT_LR, "up",
                     note="获取连接耗时上升（pool_acquire_p95）—— 与争用一致"),
        EvidenceRule("pool_in_use", SUPPORT_LR, "up",
                     note="在用连接数上升（pool_in_use）"),
        EvidenceRule("http_error_rate", AGAINST_LR, "up",
                     note="错误率上升更指向「依赖报错」而不是单纯争用"),
        EvidenceRule("client_server_gap", AGAINST_LR, "up",
                     note="client-server 差值上升 → 慢在下游，不是本服务的池子"),
    ),
    "downstream_latency": (
        EvidenceRule("client_server_gap", STRONG_LR, "up", necessary=True,
                     note="client-server 耗时差上升 —— 「慢在下游」的直接证据"),
        EvidenceRule("trace_duration_client", SUPPORT_LR, "up",
                     note="客户端侧 span 变慢（本服务发起的调用）"),
        EvidenceRule("trace_duration_server", AGAINST_LR, "up",
                     note="服务端自身 span 也慢 → 更像自身变慢"),
    ),
    "downstream_error": (
        EvidenceRule("downstream_error_rate", STRONG_LR, "up", necessary=True,
                     note="下游调用错误率上升 —— 依赖报错的直接证据"),
        EvidenceRule("http_error_rate", SUPPORT_LR, "up",
                     note="本服务错误率上升（下游报错会传染成本服务的 5xx）"),
        EvidenceRule("http_latency_p95", AGAINST_LR, "up",
                     note="只有延迟上升而没有下游错误 → 更像「下游变慢」"),
    ),
    "self_slowness": (
        EvidenceRule("trace_duration_server", STRONG_LR, "up", necessary=True,
                     note="**服务端自身 span** 变慢 —— 自身逻辑变慢的直接证据"),
        EvidenceRule("http_latency_p95", SUPPORT_LR, "up",
                     note="HTTP P95 上升（含网络，弱一层）"),
        EvidenceRule("pool_waiting", AGAINST_LR, "up",
                     note="池等待上升 → 更指向资源争用"),
        EvidenceRule("client_server_gap", AGAINST_LR, "up",
                     note="差值上升 → 慢在下游，不是自身"),
    ),
    "error_burst": (
        EvidenceRule("log_error_burst", STRONG_LR, "up", necessary=True,
                     note="错误日志聚类爆发 —— 应用层错误的直接证据"),
        EvidenceRule("http_error_rate", SUPPORT_LR, "up",
                     note="5xx 比例上升（与日志爆发互相印证）"),
        EvidenceRule("downstream_error_rate", AGAINST_LR, "up",
                     note="错误集中在下游调用 → 更像「依赖报错」"),
    ),
    "change_induced": (
        EvidenceRule("change_event", STRONG_LR, "", necessary=True,
                     note="窗口内有变更记录 —— 变更引入的**必要条件**"
                          "（且必须早于异常 onset，见 Critic 的时域判据）"),
        EvidenceRule("http_error_rate", SUPPORT_LR, "up",
                     note="变更后错误率上升（时间对齐时才计入）"),
        EvidenceRule("pool_waiting", AGAINST_LR, "up",
                     note="纯资源类信号且无变更时间对齐 → 反对变更归因"),
    ),
    "capacity_surge": (
        #  ★ 证据缺口（如实写在这里，不假装能判）：
        #    QPS 走 aux 通道（写黑板 `qps_estimates`、**不产生 Evidence**），
        #    所以"容量突变"当前只能靠"延迟随负载上升"间接判断。
        #    给 Metrics Agent 加 `traffic_surge` 证据后，这里换成它做必要条件。
        EvidenceRule("http_latency_p95", SUPPORT_LR, "up", necessary=True,
                     note="负载上升最先体现为延迟上升（暂无流量证据）"),
        EvidenceRule("pool_in_use", SUPPORT_LR, "up",
                     note="在用连接数上升（并发升高的伴随现象）"),
        EvidenceRule("http_error_rate", AGAINST_LR, "up",
                     note="错误率也大幅上升 → 更像错误爆发而不是纯容量"),
    ),
    "topology_spread": (
        EvidenceRule("blast_radius", STRONG_LR, "", necessary=True,
                     note="**爆炸半径**（受影响服务数）—— 拓扑传播的必要条件；"
                          "只有依赖图不能定因（实测过这个假确认）"),
        EvidenceRule("dependency_graph", WEAK_LR, "",
                     note="依赖图本身只是「谁连着谁」，几乎无区分度"),
        EvidenceRule("trace_duration_server", AGAINST_LR, "up",
                     note="只有单点自身变慢、没有上下游扩散 → 反对传播型"),
    ),
}

# ----------------------------------------------------------------------
#  import 期自洽校验（写错一张概率表不会在运行期报错，只会静默判错）
# ----------------------------------------------------------------------
def _category_universe() -> set[str]:
    """类别全集 —— 与 `categories.CATEGORY_SIGNALS` **同一套**（不一致就拒绝启动）。"""
    return set(CATEGORY_SIGNALS)


def _validate() -> None:
    expected = _category_universe()
    missing = sorted(expected - set(LIKELIHOOD_TABLE))
    extra = sorted(set(LIKELIHOOD_TABLE) - expected)
    if missing or extra:
        raise AssertionError(
            f"LIKELIHOOD_TABLE 与 CATEGORY_SIGNALS 的类别不一致：缺 {missing}、多 {extra}"
            f" —— 两处口径必须一一对应（否则某类假设永远拿不到 LR）")

    for category, rules in LIKELIHOOD_TABLE.items():
        if not rules:
            raise AssertionError(f"{category} 没有任何证据规则")
        bad = [rule for rule in rules if not (rule.lr > 0)]
        if bad:
            raise AssertionError(f"{category} 存在非正 LR：{bad}")
        # ★ 每个类别必须**既有支持又有反对**：只有支持的假设无法被反证，
        #   Critic 就无事可做（反事实检验会退化成走过场）
        if not any(rule.lr > NEUTRAL_LR for rule in rules):
            raise AssertionError(f"{category} 没有一条支持证据（LR > 1）")
        if not any(rule.lr < NEUTRAL_LR for rule in rules):
            raise AssertionError(f"{category} 没有一条反对证据（LR < 1）—— 无法反证")

    if abs(sum(PRIOR_WEIGHTS.values()) - 1.0) > 1e-9:
        raise AssertionError(f"先验权重之和必须为 1.0，实际 {sum(PRIOR_WEIGHTS.values())}")

    # ★★ 护栏：每条规则的关键词都必须能命中一个**真实产出**的信号名。
    #    写错名字不会报错，只会让那条规则永不命中（甚至让整个类别无法被确认）。
    from .signals import unknown_rules

    for category, rules in LIKELIHOOD_TABLE.items():
        unknown = unknown_rules([rule.signal for rule in rules])
        if unknown:
            raise AssertionError(
                f"{category} 的 LR 规则指向了不存在的信号 {unknown} —— "
                f"它们永远命中不了（真实信号名见 attribution/signals.py）")
        # ★ 每个类别都必须有一条**必要条件**：没有必要条件的类别会在
        #   "只有弱证据"时被确认（实测：仅凭依赖图给出了 topology_spread 根因）
        if not any(rule.necessary for rule in rules):
            raise AssertionError(
                f"{category} 没有任何必要条件 —— 它会在证据很弱时被确认，"
                f"而「必要条件缺失是强反证」正是 Critic 的基础")

    _validate_directions()


def _validate_directions() -> None:
    """★★ 方向这一维也要"能命中"。

    与信号名那条护栏**同一形状** —— 写错一个方向，那条规则就永不命中，
    而运行期不会有任何异常：只是"某类假设的证据永远少一条"，
    看起来像"这次没证据"（正是"静默失效"要防的那种形态）。

    两条判据：
      ① 规则里写的方向必须能被规则表达（`up`/`down`/`""`）；
      ② **证据侧的每一个方向**都必须落进这两种处置之一 ——
         能被归一（`spike→up`、`drop→down`），或显式声明为 `NON_DIRECTIONAL`（`flat`）。
         这一条是"棘轮"：将来给 `EvidenceDirection` 加第 6 个取值时，
         如果规则表达不了它，**import 就失败**，而不是那片证据悄悄消失。
    """
    from ..common.enums import EvidenceDirection

    bad_rules = sorted({rule.direction for rules in LIKELIHOOD_TABLE.values()
                        for rule in rules if not is_rule_direction(rule.direction)})
    if bad_rules:
        raise AssertionError(
            f"LR 表里出现了规则侧不认识的方向 {bad_rules} —— 这些规则永远命中不了"
            f"（规则侧只允许 {list(RULE_DIRECTIONS)}；证据侧的 spike/drop 会由 "
            f"rule_direction 归一）")

    unreachable = sorted(value.value for value in EvidenceDirection
                         if not is_rule_direction(value.value)
                         and value.value != NON_DIRECTIONAL)
    if unreachable:
        raise AssertionError(
            f"证据侧的方向 {unreachable} 归一不到任何规则侧方向 —— "
            f"带这些方向的证据一条规则也命中不了（要么补 _DIRECTION_ALIASES，"
            f"要么别让取证层发这个方向）")


_validate()


# ----------------------------------------------------------------------
#  查询
# ----------------------------------------------------------------------
def rules_of(category: str) -> tuple[EvidenceRule, ...]:
    return LIKELIHOOD_TABLE.get(category, ())


def category_of(signal: str) -> tuple[str, ...]:
    """这条信号属于哪几类假设（子串匹配；可能同时属于多类）。"""
    text = str(signal or "")
    return tuple(category for category, keywords in CATEGORY_SIGNALS.items()
                 if any(keyword in text for keyword in keywords))


def likelihood_for(category: str, signal: str,
                   direction: Optional[str] = None) -> tuple[float, str]:
    """某条证据对某类假设的似然比，返回 `(LR, note)`。

    ★ **没有匹配到任何规则时返回 `(1.0, "无相关规则")`** —— 中性，而不是"猜一个"：
      无规则意味着这条证据与该类假设无关，既不该支持也不该反对
      （`log 1.0 = 0`，不会污染分数）。
    """
    text = str(signal or "")
    want = rule_direction(direction)
    found: list[EvidenceRule] = []
    for rule in rules_of(category):
        if rule.signal not in text:
            continue
        # ★★ 比的是**规则侧口径**（`spike→up`、`drop→down`）——
        #    直接用证据原值比，会让 24 条方向规则对 spike/drop 证据全部不可达。
        if rule.direction and want and rule.direction != want:
            continue
        found.append(rule)
    if not found:
        return NEUTRAL_LR, "无相关规则（该证据与此类假设无关）"
    # ★ 同一类里可能命中多条：取**最强的一条**（绝对值最大），
    #   而不是相乘 —— 相乘会把"同一条证据被两条规则描述"变成双重计数。
    strongest = max(found, key=lambda rule: abs(_log(rule.lr)))
    return strongest.lr, strongest.note or "命中规则"


def _log(value: float) -> float:
    import math

    return math.log(value) if value > 0 else 0.0
