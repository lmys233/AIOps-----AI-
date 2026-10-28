"""请求上下文里的 `env` / `cluster` 落到槽位上 —— **上下文优先**。

★ 为什么单独一个模块：`router.py` 已逼近 800 行的规模棘轮，而"请求上下文"
  本来就是一段**自成一节**的逻辑（原先在 router 里也是一节独立的分隔带）。
  拆出来既守住棘轮，也让"上下文怎么覆盖推理层的结果"有一个可单独审的落点。

★ 为什么上下文优先：它们是**调用方的事实**（前端/告警系统知道自己在哪个环境），
  而"从一句话里猜环境"只是兜底。环境判错会让后续取证去查另一套数据 ——
  查得到、但查的是错的那套，这比查不到更糟。

--------------------------------------------------------------------------------
★ 为什么是现在这样
--------------------------------------------------------------------------------

**返回值多了"由上下文落实的槽位名"。**
`unresolved_entities` 的自然读义是"这些我还没确定，请补充"。而调用方刚刚在
`context` 里给了 `env`，响应却仍把 `env` 列进"未确定" —— 它在对调用方**自己
提供的事实**说"未确定"，是一条可机器核对的误报。调用方据此把已落实的名字摘掉。

**上下文覆盖 `env` 时要留一条 note。**
否则同一响应里会出现"`degraded` 说『不采信模型的 `staging`，以正则为准』，
而 `slots.env` 就是 `staging`"（上下文随后把它改了）—— 两条互相矛盾的声明。
"""

from __future__ import annotations

from typing import Optional

from ..common.contracts import EntityRef
from .slots import SlotConfig, Slots

__all__ = ["apply_context"]


def apply_context(
    slots: Slots,
    *,
    env: Optional[str],
    cluster: Optional[str],
    config: SlotConfig,
) -> tuple[Slots, tuple[str, ...], tuple[str, ...]]:
    """把上下文里的 `env` / `cluster` 落到槽位上（理由见模块头）。

    Returns `(槽位, 说明留痕, 由上下文**落实**的槽位名)` —— 第三项交给调用方，
    用它把已落实的名字从 `unresolved_entities` 里摘掉。
    """
    notes: list[str] = []
    settled: list[str] = []
    data = slots.model_dump()
    changed = False

    if env:
        allowed = {name.value for name, _ in config.envs}
        if env in allowed:
            if data.get("env") != env:
                notes.append(
                    f"请求上下文把 env 从 {data.get('env')!r} 覆盖为 {env!r} —— "
                    f"上下文是调用方的事实，**优先于正则与模型**"
                    f"（若上文有『不采信模型』一类的冲突说明，最终以本条为准）"
                )
                data["env"] = env
                changed = True
            settled.append("env")
        else:
            notes.append(
                f"请求上下文里的 env={env!r} 不在 {sorted(allowed)} 内 → 忽略"
                f"（契约规定的取值集合）"
            )

    if cluster:
        if data.get("cluster") != cluster:
            data["cluster"] = cluster
            changed = True
        settled.append("cluster")

    if not changed:
        return slots, tuple(notes), tuple(settled)

    names = list(data.get("service_names") or [])
    if names:
        # 集群变了 → 规范化 id（`svc:name@cluster`）也要跟着变
        data["service"] = EntityRef.make("service", names[0], data.get("cluster"),
                                         data.get("namespace"))
    return Slots(**data), tuple(notes), tuple(settled)
