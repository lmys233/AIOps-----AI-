"""`change_read` 的实现 —— 变更记录（Change Agent 的**唯一**数据源）。

★ 为什么单独一个模块：`handlers.py` 已经顶到 800 行的规模上限（棘轮 A 条），
  而"变更记录怎么查"与"指标/日志/链路怎么查"是**各自独立的一路**：
  它的入参（service / change_id / cluster）与返回形状都与别路无关。
  搬出来只搬代码，判据与口径一字未改。

★★ `entity_id` 的匹配必须**同时容纳两种写法**：规范写法 `svc:order@compose-cn-1`
  （同 `EntityRef.id`）与**裸服务名** `order`（被观测系统当前实际写入的形式）。
  早期只拼 `service:{name}@{cluster}%` → 前缀错 + 不认裸名 → **永远查不到**，
  而后果不是报错，是"这一路查了没异常" —— 把"有变更"误报成"没有变更"，
  RCA 会据此**排除变更假设**。假阴性比查询失败危险得多，所以两种都匹配。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Mapping, Optional

from ..common.contracts import ENTITY_ID_PREFIX, EntityType
from .sources import RawOutcome, SourceUnavailable

if TYPE_CHECKING:                      # 只为注解：运行期导入会与 handlers 成环
    from .handlers import ToolContext

__all__ = ["change_read", "CHANGE_SQL"]

#: `change_events` 的读取列（**不写 SELECT ***：
#: 明确列出来，DB 加列时不会悄悄改变工具返回的形状）
_CHANGE_COLUMNS: Final[str] = (
    "change_id, changed_at, entity_id, entity_type, change_type, "
    "operator, source, diff, git_commit, rollback_ref"
)

#: `EntityRef.id` 的服务前缀（形如 `svc:order@prod-cn-1`）。
#: ★ 用缩写 `svc` 而不是 `service`：前缀映射的唯一来源是
#:   `agent.common.contracts.ENTITY_ID_PREFIX`，手写字符串迟早写错一个字母。
_ENTITY_PREFIX: Final[str] = ENTITY_ID_PREFIX[EntityType.SERVICE.value]

#: 变更记录的查询语句（见模块头关于两种 `entity_id` 写法的说明）。
CHANGE_SQL: Final[str] = f"""
SELECT {_CHANGE_COLUMNS}
  FROM change_events
 WHERE changed_at >= to_timestamp($1)
   AND changed_at <= to_timestamp($2)
   -- ★ 两种写法是「或」的关系：只要命中一种就算这个服务的变更。
   --   `$3 IS NULL AND $4 IS NULL` 是"没给实体过滤"的放行分支
   --   （只按 change_id 查时走这里）。
   AND (
        ($3::text[] IS NULL AND $4::text IS NULL)
        OR entity_id = ANY($3::text[])
        OR entity_id LIKE $4
   )
   AND ($5::text IS NULL OR change_id = $5)
 ORDER BY changed_at DESC
 LIMIT $6
"""


async def change_read(ctx: "ToolContext", params: Mapping[str, Any]) -> RawOutcome:
    """变更记录（Change Agent 的**唯一**数据源）。"""
    from ..observed.context import current
    if current():
        from ..observed.archive_handlers import changes
        return await changes(params)
    if ctx.sql is None:
        raise SourceUnavailable("NotConfigured", "Postgres 未接入（config 缺少 AIOPS_DATABASE_URL）")

    service = str(params.get("service") or "").strip()
    change_id = str(params.get("change_id") or "").strip() or None
    cluster = str(params.get("cluster") or "").strip()

    entity_ids: Optional[list[str]] = None
    entity_like: Optional[str] = None
    if service:
        if ":" in service:
            # 已经是规范化的 entity_id（形如 `svc:order@compose-cn-1`）→ 精确匹配
            entity_ids = [service]
        else:
            # 裸服务名 → 既可能是表里存的原样值，也可能对应规范 id（见模块头的说明）
            entity_ids = [service]
            entity_like = f"{_ENTITY_PREFIX}:{service}@%" if not cluster else \
                f"{_ENTITY_PREFIX}:{service}@{cluster}"

    rows = await ctx.sql.fetch(
        CHANGE_SQL,
        float(params["start"]), float(params["end"]),
        entity_ids, entity_like, change_id,
        int(params.get("limit") or 50),
    )
    return RawOutcome(
        data=rows,
        raw_ref=(f"sql: change_events | entity_id∈{entity_ids or '*'} "
                 f"| entity_id~{entity_like or '*'} | change_id={change_id or '*'}"
                 f" | [{float(params['start']):.0f},{float(params['end']):.0f}]"),
        note=f"{len(rows)} 条变更记录",
    )
