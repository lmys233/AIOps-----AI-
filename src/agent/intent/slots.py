"""槽位抽取：从一句话里抽出服务、时间窗、症状等槽位。

--------------------------------------------------------------------------------
★ 三个来源，各管一段（**不要把它们的职责混起来**）
--------------------------------------------------------------------------------

| 数据 | 来源 | 为什么是这里 |
|---|---|---|
| **服务清单**（有哪些服务） | `src/agent/observed_info.yaml` | ★ **部署事实**，由探测器动态问出来（`agent/discovery/`）。AIOps 侧**唯一来源** |
| **中文别名**（用户怎么叫它们） | `slots.yaml` 的 `aliases` | **语言数据**：不随服务增减变化，是"人怎么说"而不是"系统有什么" |
| 时间单位/症状词/实体正则/默认值 | `slots.yaml` | 抽取规则的数据部分 |

★ 边界与 `rules.py` 完全同构：**外置"数据"，不动"抽取逻辑"**。
  逻辑（分句、时间窗归一化、组装 `Slots`、汇报缺了什么）全在本文件里。

--------------------------------------------------------------------------------
★ 四条设计口径
--------------------------------------------------------------------------------

1. **只抽，不判断**：本模块不决定"要不要追问缺的槽位"，它只如实回报
   "抽到了什么" + "哪些关键槽位没抽到"（`unresolved`）。
   要不要追问由 classifier 按"猜错代价 × 概率 > 问一句成本"决定。

2. **默认值 ≠ 抽到了**：`time_window` 契约有默认值，我们会用它填，**同时**把
   "time_window" 记进 `unresolved` —— 否则"用户没说"会被静默当成"用户说了 30 分钟"。

3. 服务实体用 `EntityRef.make()` 构造，**不手拼** `svc:order@cluster`
   （contracts.py 明说手拼会出现格式偏差）。

4. **不抽 tenant_id**：单租户阶段固定 `"default"`，"抽出来"的只会是假信息。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Optional

import yaml

from ..common.contracts import (  # noqa: F401  (PLACEHOLDER_CLUSTERS 见下方别名)
    PLACEHOLDER_CLUSTERS,
    EntityRef,
    EnvName,
    Slots,
)
from ..common.query import assert_default_within_budget, duration_seconds
from ..observed.references import enumeration_reference
from ..discovery import OBSERVED_INFO_FILE, load_observed_info

__all__ = [
    "SlotExtraction",
    "SlotConfig",
    "SlotConfigError",
    "extract_slots",
    "entity_is_wellformed",
    "normalize_time_window",
    "load_slot_config",
    "parse_slot_config",
    "validate_slot_config",
    "reload_slot_config",
    "ACTIVE_SLOTS",
    "SLOTS_FILE",
    "SYMPTOM_MAX",
]

_SUPPORTED_VERSION: Final[int] = 1

#: 数据表位置（与本模块同目录，随代码一起走）
SLOTS_FILE: Final[Path] = Path(__file__).with_name("slots.yaml")

#: 实体正则里**必须存在**的命名组名（代码只认它取值）
_VALUE_GROUP: Final[str] = "value"

#: `patterns` 段必须提供的键（缺一个就无法抽对应槽位）
_REQUIRED_PATTERNS: Final[tuple[str, ...]] = ("change_id", "cluster", "namespace", "slo")

#: 被明确禁止的"占位集群名"—— ★ **不自建一份**，直接用契约层的那一份
#: （以前这里多一个 `"default"`，同一套取值漂移成两套）。
_PLACEHOLDER_CLUSTERS: Final[frozenset[str]] = PLACEHOLDER_CLUSTERS

#: 时间窗单位符号（Slots.time_window 契约就是 `\d+(s|m|h|d)`）
_UNIT_SYMBOLS: Final[tuple[str, ...]] = ("s", "m", "h", "d")

#: 分句符：抽症状时按它把整句切开。
#: ★ 留在代码里 —— 它是**标点**，不是项目数据（改它等于改"什么算一句话"）。
_CLAUSE_SPLIT: Final[re.Pattern[str]] = re.compile(r"[。！？；;\n]|，|,\s*")

#: 症状文本的截断长度 —— ★ **只此一处**：抽取层截到多长，
#:   `router.merge_slots` 采纳模型的症状时就截到多长（两处各写一个数字必然漂移）。
SYMPTOM_MAX: Final[int] = 60


class SlotConfigError(Exception):
    """槽位数据表不合法 —— **fail-fast**。

    ★ 为什么必须 fail-fast：一个写错的单位词（比如 `m: [分钟]` 漏了 `分`）
      不会报任何错，只会让某些说法**静默抽不到时间窗**；
      而"抽不到"又会被当成"用户没说"，最后表现为
      「Agent 追问了一个用户明明刚说过的信息」—— 这种错最难查。
    """


@dataclass(frozen=True)
class SlotConfig:
    """编译好的槽位数据表（词表 → 正则／查表，一次性做好）。

    ★ 下面 `time_*` / `anomaly` / `service_re` / `alias_pairs` 都是**编译产物**：
      由上面的词表派生，不单独配置（否则又是"同一事实两份"）。
    """

    # ---- 来自 YAML 的原始数据 ----
    services: tuple[tuple[str, tuple[str, ...]], ...]      # ((规范名, (别名...)), ...)
    envs: tuple[tuple[EnvName, tuple[str, ...]], ...]
    time_cues: tuple[str, ...]
    unit_symbols: dict[str, str]                          # 单位词 → s/m/h/d
    cn_digits: dict[str, int]
    anomaly_words: tuple[str, ...]
    patterns: dict[str, re.Pattern[str]]
    default_time_window: str
    default_cluster: str
    source: str

    # ---- 编译产物 ----
    time_en: re.Pattern[str]
    time_cn: re.Pattern[str]
    anomaly: re.Pattern[str]
    service_re: dict[str, re.Pattern[str]]
    alias_pairs: tuple[tuple[str, str], ...]
    #: 各实体正则里 `(?P<value>...)` 那段子模式 —— 用来校验**模型给的实体格式对不对**
    #: （单一事实来源：数据表改了，校验跟着改，不会出现两份真相）
    entity_value_re: dict[str, re.Pattern[str]]

    @property
    def service_names(self) -> tuple[str, ...]:
        """服务清单（从 `services` 派生，不再存第二份）。"""
        return tuple(name for name, _ in self.services)


# ======================================================================
#  解析与校验
# ======================================================================
def _require_list(raw: Any, where: str) -> list[Any]:
    if not isinstance(raw, list) or not raw:
        raise SlotConfigError(f"{where} 必须是非空列表，实际 {raw!r}")
    return raw


def _require_str_list(raw: Any, where: str) -> tuple[str, ...]:
    out: list[str] = []
    for item in _require_list(raw, where):
        text = str(item).strip()
        if not text:
            raise SlotConfigError(f"{where} 含空字符串")
        out.append(text)
    return tuple(out)


def _value_group_source(expr: str, where: str) -> str:
    """取出 `(?P<value>...)` 里那段**子正则的源码**。

    ★ 为什么需要它：模型可能自己编一个 `change_id`。判断"这个值格式对不对"的
      唯一权威来源就是数据表里那条正则 —— 把它的 `value` 子模式抽出来做 fullmatch，
      就不会出现"YAML 改了、代码里的校验还是老一套"这种两份真相。

    ★ 为什么手写括号配平而不是用 `re._parser`：那是私有 API（3.13 已改名），
      而这里只需要处理"转义 / 字符类 / 嵌套括号"三种情况。
    """
    marker = f"(?P<{_VALUE_GROUP}>"
    start = expr.find(marker)
    if start < 0:
        raise SlotConfigError(f"{where} 必须含名为 {_VALUE_GROUP!r} 的捕获组")
    index = start + len(marker)
    depth = 1
    in_class = False
    escaped = False
    out: list[str] = []
    while index < len(expr):
        char = expr[index]
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            out.append(char)
            escaped = True
        elif in_class:
            out.append(char)
            if char == "]":
                in_class = False
        elif char == "[":
            out.append(char)
            in_class = True
        elif char == "(":
            depth += 1
            out.append(char)
        elif char == ")":
            depth -= 1
            if depth == 0:
                return "".join(out)
            out.append(char)
        else:
            out.append(char)
        index += 1
    raise SlotConfigError(f"{where} 的 (?P<{_VALUE_GROUP}>...) 括号不配平")


def entity_is_wellformed(config: "SlotConfig", key: str, value: str) -> bool:
    """`value` 是否符合数据表里 `patterns.<key>` 的**取值格式**。

    ★ 用途：模型抽出来的实体（变更单号 / SLO / 集群 / 命名空间）在采信前过一遍这里。
      格式不对 = 大概率是编的 → 丢弃并留痕，而不是让它流进取证去查一个不存在的东西。
    """
    pattern = config.entity_value_re.get(key)
    if pattern is None:
        return False
    return bool(pattern.fullmatch(str(value).strip()))


def normalize_time_window(text: str, *, config: Optional["SlotConfig"] = None) -> Optional[str]:
    """把一段文本归一成契约要求的 `\\d+(s|m|h|d)`（拿不到就返回 `None`）。

    ★ 供两处使用：① 槽位抽取（内部）；② **校验模型给的 `time_window`**
      —— 模型常回 "30分钟" / "半小时" 这类中文写法，直接塞进 `Slots`
      会被契约的校验器拒掉（那会变成 500 而不是"丢弃一个坏槽位"）。
    """
    from ..observed.slot_config import scoped
    active = scoped(config or ACTIVE_SLOTS)
    raw = (text or "").strip()
    if not raw:
        return None
    # ★ 时长写法的**唯一解析**在 `common.query.duration_seconds`（不在这里再写一份正则）。
    #   `raw.islower()` 是为了与 `Slots` 契约的 `\d+(s|m|h|d)` 严格对齐：
    #   契约不收 `30M`，这里就不能把它当成"已经归一好了"直接放行。
    if duration_seconds(raw) is not None and raw.islower():
        return raw
    return _time_window(raw, active)


def _compile_with_value_group(expr: str, where: str) -> re.Pattern[str]:
    """编译实体正则，并要求它带名为 `value` 的捕获组。

    ★ 为什么强制命名组：代码只认 `match.group("value")`。
      若允许位置组，正则一改（多插一个括号）取值就会**静默取错**。
    """
    try:
        compiled = re.compile(expr)
    except re.error as exc:
        raise SlotConfigError(f"{where} 正则非法：{exc}") from exc
    if _VALUE_GROUP not in compiled.groupindex:
        raise SlotConfigError(
            f"{where} 必须含名为 {_VALUE_GROUP!r} 的捕获组（如 (?P<{_VALUE_GROUP}>...)）"
            f" —— 代码只按这个名字取值，否则改正则就会静默取错"
        )
    return compiled


def _build_time_regexes(unit_symbols: dict[str, str],
                        cn_digits: dict[str, int]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """**从词表派生**时间窗正则。

    ★ 关键一步：单位词按**长度降序**拼 alternation。
      正则 alternation 从左到右取第一个能匹配的，所以「分钟」必须排在「分」前面，
      否则 `30 分钟` 会被切成 `30 分` + 残留「钟」。
      ★ 这件事由代码保证，而不是要求"改数据的人记住顺序" —— 数据文件里不该埋这种陷阱。
    """
    cn_words = sorted(unit_symbols, key=len, reverse=True)
    cn_alt = "|".join(re.escape(word) for word in cn_words)
    digits = "".join(re.escape(char) for char in cn_digits)
    cn_re = re.compile(rf"(?<![\d.])(?P<num>半|\d+|[{digits}]+)\s*(?P<unit>{cn_alt})")

    en_alt = "|".join(sorted(set(unit_symbols.values()), key=len, reverse=True))
    en_re = re.compile(rf"(?<![\d.])(?P<num>\d+)\s*(?P<unit>{en_alt})\b")
    return en_re, cn_re


def _inflect(name: str) -> str:
    """服务名的**英语变形后缀**（判"用户说的是不是这个服务"时要认）。

    ★★ 为什么要认变形：用户说的是人话 ——「orders 工作线程池已满」里的 `orders`
      指的就是服务 `order`。只认原形的后果实测过：**槽位抽不到服务** →
      追问"你想查哪个服务" → 那一轮**一次取证都没有**，答案里又出现
      "没发现异常"（把"没查成"说成"没有"）。
    ★ 词边界仍然两头都要（`(?<![a-z])` / `(?![a-z])`）：`orders` 能命中 `order`，
      而 `preorder` / `reordering` 仍然不会被误判成它。
    """
    base = re.escape(name)
    if name.endswith(("s", "x", "z", "ch", "sh")):
        return rf"(?:{base}(?:es|'s|'|s)?)"
    if name.endswith("y") and len(name) > 1 and name[-2] not in "aeiou":
        return rf"(?:{base[:-1]}ies|{base}s?|{base}'s|{base}')"
    return rf"(?:{base}s?|{base}'s|{base}')"


def parse_slot_config(
    text: str,
    *,
    source: str = "<string>",
    service_names: Iterable[str],
) -> SlotConfig:
    """把 YAML 文本解析成 `SlotConfig`（**纯函数**，便于单测喂坏样例）。

    Args:
        text: `slots.yaml` 的文本。
        source: 来源标识（进错误信息与 `SlotConfig.source`）。
        service_names: **服务清单**（来自 `observed_info.yaml`，由探测器维护）。
            ★ 必须由调用方传入，而不是从这个文件里读 ——
              服务清单只有一个来源，本文件不该有它的第二份副本。
    """
    names = tuple(dict.fromkeys(str(n).strip() for n in service_names if str(n).strip()))
    if not names:
        raise SlotConfigError(
            "服务清单为空 —— 先跑一次探测器：`.\\run.ps1 observed-discover`"
            "（它会把服务写进 src/agent/observed_info.yaml）"
        )

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SlotConfigError(f"{source} 不是合法 YAML：{exc}") from exc
    if not isinstance(doc, dict):
        raise SlotConfigError(f"{source} 顶层必须是映射（mapping）")
    if doc.get("version") != _SUPPORTED_VERSION:
        raise SlotConfigError(
            f"不支持的槽位表版本 {doc.get('version')!r}（本实现只认 {_SUPPORTED_VERSION}）"
        )

    # ---- 中文别名 → 规范服务名 ----
    #  ★ 别名是**语言数据**（用户怎么叫），服务清单是**部署事实**（观测来的）。
    #    两者分开维护；这里只校验"别名指向的服务确实存在"。
    raw_aliases = doc.get("aliases") or {}
    if not isinstance(raw_aliases, dict):
        raise SlotConfigError("aliases 必须是映射（中文别名 → 服务名）")
    aliases_by_service: dict[str, list[str]] = {name: [] for name in names}
    for alias, target in raw_aliases.items():
        alias_text, target_text = str(alias).strip(), str(target).strip()
        if not alias_text or not target_text:
            raise SlotConfigError(f"aliases 里出现空值：{alias!r} → {target!r}")
        if target_text not in aliases_by_service:
            raise SlotConfigError(
                f"别名 {alias_text!r} 指向的服务 {target_text!r} 不在观测清单里 "
                f"（当前：{list(names)}）—— 要么删掉这个别名，要么改指向；"
                f"服务清单请改被观测系统，然后重跑探测器"
            )
        aliases_by_service[target_text].append(alias_text)
    services = tuple((name, tuple(aliases_by_service[name])) for name in names)

    # ---- 环境词 ----
    raw_envs = doc.get("envs")
    if not isinstance(raw_envs, dict) or not raw_envs:
        raise SlotConfigError("envs 必须是非空映射（键 ∈ prod/staging/dev）")
    envs: list[tuple[EnvName, tuple[str, ...]]] = []
    for key, words in raw_envs.items():
        try:
            env_name = EnvName(str(key))
        except ValueError as exc:
            raise SlotConfigError(
                f"envs 的键 {key!r} 不在允许的环境集合 "
                f"{[e.value for e in EnvName]} 内"
            ) from exc
        envs.append((env_name, _require_str_list(words, f"envs.{key}")))

    # ---- 时间窗 ----
    raw_time = doc.get("time")
    if not isinstance(raw_time, dict):
        raise SlotConfigError("time 必须是映射（cues / units / cn_digits）")
    cues = _require_str_list(raw_time.get("cues"), "time.cues")
    raw_units = raw_time.get("units")
    if not isinstance(raw_units, dict) or not raw_units:
        raise SlotConfigError("time.units 必须是非空映射（s/m/h/d → 说法列表）")
    unit_symbols: dict[str, str] = {}
    for symbol, words in raw_units.items():
        sym = str(symbol).strip()
        if sym not in _UNIT_SYMBOLS:
            raise SlotConfigError(
                f"time.units 的键 {sym!r} 非法 —— 只能是 {list(_UNIT_SYMBOLS)}"
                f"（Slots.time_window 的校验规则就是 \\d+(s|m|h|d)）"
            )
        for word in _require_str_list(words, f"time.units.{sym}"):
            if word in unit_symbols:
                raise SlotConfigError(f"单位词 {word!r} 在 time.units 里出现了两次")
            unit_symbols[word] = sym
    raw_digits = raw_time.get("cn_digits") or {}
    if not isinstance(raw_digits, dict):
        raise SlotConfigError("time.cn_digits 必须是映射（中文数字 → 整数）")
    cn_digits: dict[str, int] = {}
    for char, value in raw_digits.items():
        try:
            num = int(value)
        except (TypeError, ValueError) as exc:
            raise SlotConfigError(f"time.cn_digits.{char} 不是整数：{value!r}") from exc
        if num <= 0:
            raise SlotConfigError(f"time.cn_digits.{char} 必须为正数，实际 {num}")
        cn_digits[str(char)] = num

    # ---- 症状词 ----
    anomaly_words = _require_str_list(doc.get("anomaly_words"), "anomaly_words")

    # ---- 实体正则 ----
    raw_patterns = doc.get("patterns")
    if not isinstance(raw_patterns, dict):
        raise SlotConfigError("patterns 必须是映射")
    patterns: dict[str, re.Pattern[str]] = {}
    value_patterns: dict[str, re.Pattern[str]] = {}
    for key in _REQUIRED_PATTERNS:
        expr = str(raw_patterns.get(key) or "")
        if not expr:
            raise SlotConfigError(f"patterns 缺少 {key}（无法抽对应槽位）")
        patterns[key] = _compile_with_value_group(expr, f"patterns.{key}")
        # ★ 同一份正则里再抽出"取值"那一段，供"校验模型给的实体格式"使用
        try:
            value_patterns[key] = re.compile(
                _value_group_source(expr, f"patterns.{key}")
            )
        except re.error as exc:
            raise SlotConfigError(
                f"patterns.{key} 的取值子模式本身不是合法正则：{exc}"
            ) from exc

    # ---- 默认值 ----
    raw_defaults = doc.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        raise SlotConfigError("defaults 必须是映射")
    default_window = str(raw_defaults.get("time_window") or "").strip()
    # ★ 集群名允许留空（`slots.yaml` 里不写）：它是**部署事实**，
    #   取值走 `common/identity.py` 的阶梯。这里只收"配置里显式写了什么"。
    default_cluster = str(raw_defaults.get("cluster") or "").strip()

    en_re, cn_re = _build_time_regexes(unit_symbols, cn_digits)
    config = SlotConfig(
        services=tuple(services),
        envs=tuple(envs),
        time_cues=cues,
        unit_symbols=unit_symbols,
        cn_digits=cn_digits,
        anomaly_words=anomaly_words,
        patterns=patterns,
        default_time_window=default_window,
        default_cluster=default_cluster,
        source=source,
        time_en=en_re,
        time_cn=cn_re,
        anomaly=re.compile("|".join(re.escape(w) for w in anomaly_words)),
        service_re={name: re.compile(rf"(?<![a-z]){_inflect(name)}(?![a-z])")
                    for name, _ in services},
        alias_pairs=tuple((alias, name) for name, aliases in services for alias in aliases),
        entity_value_re=value_patterns,
    )
    validate_slot_config(config)
    return config


def validate_slot_config(config: SlotConfig) -> None:
    """校验数据表的不变量。任何一条不过 → `SlotConfigError`（拒绝启动）。"""
    if not config.services:
        raise SlotConfigError("services 为空 —— 抽不到任何服务实体")
    if not config.unit_symbols:
        raise SlotConfigError("time.units 里一个单位词都没有")
    if not config.anomaly_words:
        raise SlotConfigError("anomaly_words 为空 —— 症状永远抽不到")
    # ★ `defaults.cluster` **允许为空**：集群名是部署事实，配置里不写是常态。
    #   取不到就是"未知"，契约允许（`EntityRef.id` 可省略 `@cluster`）。
    #   ★ 但**不许是占位值** —— 那等于发明一个假集群名（生成端会被契约拒掉，
    #     与其等到装配时才炸，不如在装载配置时就拦住）。
    if config.default_cluster.lower() in _PLACEHOLDER_CLUSTERS:
        raise SlotConfigError(
            f"defaults.cluster={config.default_cluster!r} 是占位值 —— "
            f"集群未知时应当**留空**（取值走 identity 阶梯），不得发明占位值")

    # ★ 默认时间窗必须符合 Slots 契约的 `\d+(s|m|h|d)`，否则装配 Slots 时才炸。
    #   时长写法统一由 `common.query.duration_seconds` 解析（不再各写一份正则）；
    #   `default.islower()` + 正秒数同时守住"契约只收小写"与"`00s` 不算合法窗"。
    default = str(config.default_time_window or "").strip()
    seconds = duration_seconds(default)
    if seconds is None or default != default.lower() or seconds <= 0:
        raise SlotConfigError(
            f"defaults.time_window={config.default_time_window!r} 不符合 "
            f"Slots 契约要求的 `\\d+(s|m|h|d)`（如 30m / 2h / 1d）"
        )
    # ★ 兜底语义：默认窗不得"看起来像用户说的" —— 它只是个填充值，
    #   真正重要的是 extract_slots 会把它同时记进 unresolved（口径 2）。
    #   ★ 上一条的 `seconds <= 0` 已经把 `0s` / `00s` 一起挡住（不再单列一个字面量）。
    # ★★ 默认窗**不得超过取数预算的长度上限**（默认 7 天）。
    #   为什么抛错而不是悄悄夹住：配置与预算打架是**设计问题**，
    #   让取数层替它兜着，就会变成"说 30d、实际只查 7d"的另一套口径。
    try:
        assert_default_within_budget(config.default_time_window)
    except ValueError as exc:
        raise SlotConfigError(str(exc)) from exc


def load_slot_config(
    path: Optional[Path] = None,
    *,
    observed_path: Optional[Path] = None,
) -> SlotConfig:
    """加载槽位数据表。

    ★ 服务清单**不从这个文件读**，而是从 `observed_info.yaml`（探测器维护的唯一来源）；
      本文件只提供中文别名与抽取规则。

    Args:
        path: `slots.yaml` 路径；默认与本模块同目录。
        observed_path: `observed_info.yaml` 路径；默认 `src/agent/observed_info.yaml`
            （测试可指向临时文件）。
    """
    slots_path = Path(path) if path is not None else SLOTS_FILE
    try:
        text = slots_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise SlotConfigError(f"槽位数据表不存在：{slots_path}") from exc
    except OSError as exc:
        raise SlotConfigError(f"读不了槽位数据表 {slots_path}：{exc}") from exc

    observed = load_observed_info(observed_path if observed_path is not None
                                  else OBSERVED_INFO_FILE)
    return parse_slot_config(text, source=str(slots_path),
                             service_names=observed.names)


#: 进程启动时加载并校验（坏数据表 → **import 就炸**）
ACTIVE_SLOTS: SlotConfig = load_slot_config()


def reload_slot_config(path: Optional[Path] = None) -> SlotConfig:
    """重新加载并替换生效数据表（运维/测试用）。"""
    global ACTIVE_SLOTS
    ACTIVE_SLOTS = load_slot_config(path)
    return ACTIVE_SLOTS


# ======================================================================
#  抽取
# ======================================================================
#: **枚举式指代**：问"哪些服务"时，`service` 指的是**拓扑里的全部服务**，
#: 不是"用户忘了说哪一个"。判据是措辞（确定性），不是猜。
#: ★ 为什么要把"全系统 / 全服务"这类**简写**也收进来：它们是同一类范围事实。
#:   少收一个写法，那句话就会掉进"没抽到服务"的分支 → 追问"你要查哪个系统"，
#:   而用户其实已经把范围说清了（问的是全局）。
_SERVICE_ENUM: Final[re.Pattern[str]] = re.compile(
    r"哪些服务|哪个服务|谁在报错|都有谁|分别|各自|所有服务|全部服务|哪些系统|哪些组件"
    r"|整个系统|所有系统|全部系统|整个服务|各个服务|每个服务|所有组件|全部组件"
    r"|全系统|全服务|全站|所有微服务|全部微服务")


@dataclass(frozen=True)
class SlotExtraction:
    """抽取结果：填好的槽位 + **没抽到的关键槽位名** + 抽取期的说明（留痕）。

    ★ `enumerated`：这句用了**枚举指代**（"所有服务/整个系统"）—— 它是**范围事实**，
      下游合并槽位时**不许被模型收窄**成一个服务。
    """

    slots: Slots
    unresolved: tuple[str, ...]
    notes: tuple[str, ...] = ()
    enumerated: bool = False
    excluded_services: tuple[str, ...] = ()


def _norm_time(num_raw: str, unit_raw: str, config: SlotConfig) -> Optional[str]:
    """把 `(数字, 单位)` 归一成 `\\d+(s|m|h|d)`。"""
    if num_raw == "半":
        value = 0.5
    elif num_raw.isdigit():
        value = float(num_raw)
    else:
        value = float(config.cn_digits.get(num_raw, 0))
    if value <= 0:
        return None

    symbol = config.unit_symbols.get(unit_raw)
    if symbol is None:
        return None
    if symbol == "h" and value == 0.5:
        return "30m"                       # "半小时"
    if value != int(value):
        return f"{int(value * 60)}m"
    return f"{int(value)}{symbol}"


def _cued(text: str, pos: int, config: SlotConfig) -> bool:
    """数字前面 6 个字符内有没有"窗口提示词"。"""
    window = text[max(0, pos - 6):pos]
    return any(cue in window for cue in config.time_cues)


def _time_window(text: str, config: SlotConfig) -> Optional[str]:
    """抽时间窗。

    ★ 两条防线（都是被真实用例逼出来的）：
      1. `(?<![\\d.])`：`延迟涨到 2.3s` 里的 `3s` 不能被当成窗口（数字前是小数点）；
      2. **无提示词时只接受 分/时/天**：裸 `2s`/`3 秒` 绝大多数是"耗时值"而不是窗口，
         而"2 小时""30 分钟"没有提示词也基本可以确定是窗口。
    """
    for m in config.time_en.finditer(text):
        symbol = config.unit_symbols.get(m.group("unit"), m.group("unit"))
        if symbol in ("m", "h", "d") or _cued(text, m.start(), config):
            return f"{int(m.group('num'))}{symbol}"
    for m in config.time_cn.finditer(text):
        num_raw, unit_raw = m.group("num"), m.group("unit")
        symbol = config.unit_symbols.get(unit_raw)
        if symbol is None:
            continue
        if symbol in ("m", "h", "d") or _cued(text, m.start(), config):
            return _norm_time(num_raw, unit_raw, config)
    return None


def _service_excluded(text: str, position: int) -> bool:
    """仅识别服务附近的否定动作；没有问题等事实否定不排除查询目标。"""
    before = _CLAUSE_SPLIT.split(text[:position])[-1]
    after = _CLAUSE_SPLIT.split(text[position:])[0]
    pattern = r"(?P<no>(?:不|(?<!分)别|不要|不用|无需|暂缓|跳过)\s*)?(?:修复|修理|处理|解决|重启|回滚|排查|检查|查询|查|看)"
    actions = list(re.finditer(pattern, before))
    if actions:
        return bool(actions[-1].group("no"))
    return bool(re.search(r"(?:不|别|不要|不用|无需)\s*(?:修复|处理|解决|重启|回滚)", after))


def _services(text: str, config: SlotConfig, *, include_excluded: bool = False) -> list[str]:
    """抽服务名（英文原样 + 中文别名），按**在句子里出现的先后**排序、去重。

    ★ 顺序必须是"用户先提到谁，谁就排前"，**不能是"观测清单里的顺序"**：
      清单是**生成物**（探测器按 `/services` 的结果写），它的排序一变、
      抽取结果就跟着变 —— 那就把"用户怎么说的"变成了"文件怎么排的"。
      （实测踩过：清单改成按名字排序后，`order 和 inventory` 被抽成 `['inventory','order']`；
       而且旧写法把**别名**一律排在**英文名**之后，`网关和 order` 会得到 `['order','gateway']`。）
    """
    low = text.lower()
    hits: list[tuple[int, str]] = []
    for name, _ in config.services:
        # 词边界：避免 order 命中 preorder 这类词
        for matched in config.service_re[name].finditer(low):
            if include_excluded or not _service_excluded(text, matched.start()):
                hits.append((matched.start(), name))
    for alias, name in config.alias_pairs:
        for matched in re.finditer(re.escape(alias), text):
            if include_excluded or not _service_excluded(text, matched.start()):
                hits.append((matched.start(), name))

    ordered: list[str] = []
    for _, name in sorted(hits, key=lambda pair: pair[0]):
        if name not in ordered:
            ordered.append(name)
    return ordered


def _symptom(text: str, config: SlotConfig) -> Optional[str]:
    """取第一个"含异常词的分句"作为症状描述（截断 `SYMPTOM_MAX` 字）。"""
    for clause in _CLAUSE_SPLIT.split(text):
        clause = clause.strip()
        if clause and config.anomaly.search(clause):
            return clause[:SYMPTOM_MAX]
    return None


def _entity(text: str, key: str, config: SlotConfig) -> Optional[str]:
    found = config.patterns[key].search(text)
    return found.group(_VALUE_GROUP) if found else None


def extract_slots(message: str, *, config: Optional[SlotConfig] = None) -> SlotExtraction:
    """从一句话抽槽位。**永远有返回**（抽不到的槽位留在 `unresolved` 里）。

    Args:
        message: 用户原始输入。
        config: 用哪份数据表；``None`` = 生效中的那份（``ACTIVE_SLOTS``）。
            ★ 允许显式传入，是为了让测试验证"换一份 YAML 行为就变"。
    """
    from ..observed.slot_config import scoped
    active = scoped(config or ACTIVE_SLOTS)
    text = (message or "").strip()

    services = _services(text, active)
    excluded = tuple(name for name in _services(text, active, include_excluded=True)
                     if name not in services)
    window = _time_window(text, active)
    symptom = _symptom(text, active)
    change_id = _entity(text, "change_id", active)
    namespace = _entity(text, "namespace", active)
    slo_target = _entity(text, "slo", active)
    cluster = _entity(text, "cluster", active) or active.default_cluster or None

    # ★★ `env` 与时间窗是**同一类**槽位 ——
    #   它也有默认值（`prod`），所以"用户说了生产"与"用户什么都没说"在
    #   `slots.env` 上**长得一模一样**。下游（`router.merge_slots`）要区分这两者，
    #   唯一的依据就是这里记的 `unresolved`（与 `time_window` 的口径 2 完全同构）。
    env = EnvName.PROD
    env_extracted = False
    low = text.lower()
    for env_name, words in active.envs:
        if any(word in low for word in words):
            env = env_name
            env_extracted = True
            break

    notes: list[str] = []
    if excluded:
        notes.append(f"本轮明确暂不处理的服务：{'、'.join(excluded)}；不纳入取证与方案目标")
    # ★★ 枚举指代：`哪些服务 / 分别 / 都有谁` —— 用户**已经说清了范围**（全部），
    #   不该再问"你要查哪个系统"。填全部服务名，并且**不把 service 记进 unresolved**。
    reference=enumeration_reference(text,_SERVICE_ENUM,services);enumerated=bool(reference)
    if enumerated:
        services = [name for name in active.service_names if name not in excluded]
        notes.append(
            f"「{reference}」是**枚举指代** → "
            f"service 指代拓扑里的全部服务（{len(services)} 个），不再反问目标")
    service = (EntityRef.make("service", services[0], cluster, namespace)
               if services and not enumerated else None)

    slots = Slots(
        service=service,
        env=env,
        cluster=cluster,
        # ★ 填默认值，但下面照样把它记进 unresolved（"没说" ≠ "说了默认值"）
        time_window=window or active.default_time_window,
        symptom=symptom,
        change_id=change_id,
        slo_target=slo_target.replace(" ", "") if slo_target else None,
        namespace=namespace,
        service_names=list(services),
    )

    unresolved: list[str] = []
    if service is None and not enumerated:
        unresolved.append("service")
    if window is None:
        unresolved.append("time_window")
    if symptom is None:
        unresolved.append("symptom")
    if change_id is None:
        unresolved.append("change_id")
    if not env_extracted:
        # ★★ 默认 env 也记进 unresolved（"没说"≠"说了 prod"）。
        unresolved.append("env")
    return SlotExtraction(slots=slots, unresolved=tuple(unresolved),
                          notes=tuple(notes), enumerated=enumerated, excluded_services=excluded)
