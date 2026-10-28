"""Change Agent —— 变更清单 + diff + 时间。

--------------------------------------------------------------------------------
★ 这一路最容易出错的地方不是"查不到"，而是"**时间对不齐**"
--------------------------------------------------------------------------------

"变更触发了故障"这个假设，全部重量都压在**时间的先后**上：
变更在异常拐点**之前**才是原因，在**之后**只是"有人也在修"。
所以本 Agent 有两件事必须做对：

1. **往前多看一段**（`LOOKBACK_SEC`）。变更常常发生在异常**之前**几分钟到十几分钟
   （发布 → 流量上来 → 指标恶化），而观测窗口是从"用户报障时刻"往前划的。
   只看观测窗口，最容易漏掉**真正的那次变更**。

2. **如实报告自己查过的区间**。因为上面的原因，这一路的查询范围比观测窗口更宽，
   所以证据的 `time_range` 用**查询区间**而不是观测窗口
   （`make_evidence(time_range=...)`）—— 否则 `onset` 会被夹进窗口，
   "变更早于异常 12 分钟"会被改写成"变更与异常同时发生"，因果结论直接反转。

★ `Evidence.direction` 用 `FLAT`：变更**是一个事实**，不是一次涨跌。
  用 `up`/`spike` 会让下游以为"这个量在上升"，那是编造。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Final, Mapping, Optional

from ..common.contracts import Evidence
from ..common.enums import EvidenceDirection, EvidenceSource
from ..tools.semantics import MAX_QUERY_WINDOW_SEC
from .base import AgentContext, ForensicsAgent, ToolRequest, target_services, to_datetime

__all__ = ["ChangeAgent", "LOOKBACK_SEC", "MAX_CHANGE_EVIDENCE"]

#: 变更查询比观测窗口**往前多看**多久（发布型故障的典型提前量）
LOOKBACK_SEC: Final[int] = 900

#: 最多把几条变更转成证据（变更多的时候按时间倒序留最近的）
MAX_CHANGE_EVIDENCE: Final[int] = 10

#: 一次查询最多取几条变更（工具层的 limit 上限是 200）
_QUERY_LIMIT: Final[int] = 50

#: 每个服务一条查询，最多几个服务
_MAX_SERVICES: Final[int] = 3


class ChangeAgent(ForensicsAgent):
    batch_limit = _MAX_SERVICES
    """变更取证：谁在什么时候改了什么，以及它是不是"比异常更早"。"""

    role = "change_agent"
    source = EvidenceSource.CHANGES
    tools = ("change_read",)

    # ------------------------------------------------------------------
    def plan_calls(self, ctx: AgentContext) -> list[ToolRequest]:
        start, end = change_window(ctx)
        common = {"start": start.isoformat(), "end": end.isoformat(), "limit": _QUERY_LIMIT}

        change_id = str(ctx.slots.change_id or "").strip()
        if change_id:
            # 用户直接给了变更单号 → 一条查询搞定（这也正是 intent 层抽 change_id 的意义）
            return [ToolRequest(
                tool="change_read", params={"change_id": change_id, **common},
                signal="change_event",
                rationale=f"用户/槽位给出了变更单号 {change_id}，直接定位那一次变更")]

        services = target_services(ctx, limit=_MAX_SERVICES)
        if not services:
            # `change_read` 的语义校验要求 service 或 change_id 至少有一个
            # （防全表扫描）—— 都给不出就**不发调用**，由规划器如实标记这一路跳过。
            return []
        return [ToolRequest(
            tool="change_read", params={"service": name, **common},
            signal="change_event",
            rationale=(f"查 {name} 在观测窗口前 {LOOKBACK_SEC // 60} 分钟至今的变更"
                       f"（发布型故障的变更往往早于异常拐点）"))
            for name in services[:_MAX_SERVICES]]

    # ------------------------------------------------------------------
    def build_evidence(self, ctx: AgentContext, request: ToolRequest,
                       result: Any) -> list[Evidence]:
        data = getattr(result, "data", None)
        rows = (data or {}).get("changes") if isinstance(data, Mapping) else None
        window = change_window(ctx)
        out: list[Evidence] = []

        ordered = sorted((row for row in (rows or []) if isinstance(row, Mapping)),
                         key=lambda row: str(row.get("changed_at") or ""), reverse=True)
        for row in ordered[:MAX_CHANGE_EVIDENCE]:
            changed_at = _changed_at(row)
            if changed_at is None:
                # 没有时间锚点的变更**不能**当证据：它无法参与时序因果
                continue
            entity = self._entity_for(ctx, row)
            if entity is None:
                continue
            out.append(self.make_evidence(
                ctx, signal=request.signal or "change_event",
                direction=EvidenceDirection.FLAT,
                onset=changed_at, entity=entity,
                confidence=_confidence(changed_at, ctx),
                raw_ref=(f"{getattr(result, 'raw_ref', '') or request.tool}"
                         f" | change_id={row.get('change_id')}"),
                summary=_summary(row, changed_at),
                tags=["changes"] + [tag for tag in (str(row.get("change_type") or "") or None,)
                                    if tag],
                time_range=window))
        return out

    @staticmethod
    def _entity_for(ctx: AgentContext, row: Mapping[str, Any]) -> Optional[Any]:
        """把变更行挂到实体上。

        ★ 变更行的 `entity_id` 形如 `svc:payment@compose-cn-1`，
          它是**变更自己的**实体；优先用它，而不是硬套观测目标 ——
          否则"本次排查只关心 order，但变更发生在 payment 上"这条线索会被丢掉。
        """
        raw = str(row.get("entity_id") or "")
        kind, name, cluster = "", "", ""
        if ":" in raw:
            head, _, tail = raw.partition("@")
            kind, _, name = head.partition(":")
            cluster = tail
        if not name:
            name = str(row.get("service") or "")
        if not name:
            return ctx.entity()
        if name in ctx.services:
            return ctx.entity(name)
        return _make_entity(kind, name, cluster or (ctx.slots.cluster or ""), ctx)


def _make_entity(kind: str, name: str, cluster: str, ctx: AgentContext) -> Any:
    """按规范构造实体（`id` 前缀由 `EntityRef.make` 统一映射，不手拼）。

    ★ 集群名**可空**：变更行里没有集群、槽位里也没有时，实体就是 `svc:order`
      （契约允许省略 `@cluster`）。**不发明**默认集群名。
    """
    from ..common.contracts import EntityRef
    from ..common.enums import EntityType

    try:
        entity_type: Any = EntityType(kind) if kind else EntityType.SERVICE
    except ValueError:
        entity_type = EntityType.SERVICE
    namespace = ctx.slots.namespace if entity_type == EntityType.SERVICE else None
    return EntityRef.make(entity_type, name, cluster or ctx.slots.cluster, namespace)


# ======================================================================
def change_window(ctx: AgentContext) -> tuple[datetime, datetime]:
    """变更取证真正查询的区间：观测窗口 **往前多看 `LOOKBACK_SEC`**。

    ★★ 但总长**不得超过工具的时间窗上限**（≤24h）。

    为什么必须夹住：观测窗口本身就可能顶到上限（`slots.time_window` 允许 `24h`），
    再往前加 15 分钟就变成 24.2h —— 语义校验会以 `TOOL_SCOPE_TOO_BROAD` 拒绝**整条**
    变更查询，于是"变更归因"这一路会**整路缺失**（真链路实测踩到：
    24h 窗口下 3 个服务的变更查询全被拒）。宁可少看那 15 分钟，也不能整路没有。
    """
    span = (ctx.window_end - ctx.window_start).total_seconds()
    lookback = max(0.0, min(float(LOOKBACK_SEC), MAX_QUERY_WINDOW_SEC - span))
    return ctx.window_start - timedelta(seconds=lookback), ctx.window_end


def _changed_at(row: Mapping[str, Any]) -> Optional[datetime]:
    """变更时间（**必须拿得到**，否则这条变更无法参与时序因果）。

    ★ 这里不复用 `to_datetime` 的默认值：它拿不到会把"现在"当答案，
      那会凭空造出一个"刚刚发生的变更" —— 比没有更糟。
    """
    for key in ("changed_at", "occurred_at", "ts", "created_at"):
        value = row.get(key)
        if not value:
            continue
        text = str(value).strip()
        if text.isdigit():
            return to_datetime(text)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _confidence(changed_at: datetime, ctx: AgentContext) -> float:
    """越靠近观测窗口开头的变更，越可能是"刚上线就出事"。

    ★ 这只是**先验**（变更离异常多近），不是"它就是根因"——
      最终裁决在 RCA 打分，本 Agent 不越界下结论。
    """
    score = 0.60
    if changed_at >= ctx.window_start:
        score += 0.10                      # 落在观测窗口内：与异常同期
    else:
        gap = (ctx.window_start - changed_at).total_seconds()
        if gap <= LOOKBACK_SEC / 2:
            score += 0.10                  # 紧贴在异常之前
    return min(score, 0.85)


def _summary(row: Mapping[str, Any], changed_at: datetime) -> str:
    diff = row.get("diff")
    diff_text = ""
    if isinstance(diff, Mapping) and diff:
        keys = list(diff.keys())[:4]
        diff_text = "；diff 字段：" + "、".join(str(key) for key in keys)
    return (f"变更 {row.get('change_id')}：{row.get('change_type') or '（未标类型）'}"
            f" 由 {row.get('operator') or '（未记录执行人）'} 于 {changed_at.isoformat()}"
            f" 执行（source={row.get('source') or '-'}，实体={row.get('entity_id') or '-'}）"
            f"{diff_text}")
