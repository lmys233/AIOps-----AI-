"""审批绑定的**动作指纹**（`action_hash`）—— ★ 全仓唯一实现。

--------------------------------------------------------------------------------
★ 为什么单独一个模块
--------------------------------------------------------------------------------

同一个指纹有**两个使用点**，它们必须算出同一个值：

  · HTTP 层（`api/util._action_fingerprint`）拿到的是 `ProposedAction` 的 dict：
    `{"action_class", "target": {"id": …}, "params"}`
  · 执行端（`tools/executor._action_hash`）拿到的是 `change_write` 的**入参**：
    `{"action_class", "target": "svc:order@c1", "params", …}`

这两处以前各写一套：API 侧哈希"类别 + 目标 id + 参数"，执行端只哈希
`params` —— 而 `api/util.py` 的注释却声称"口径与 `tools/executor._action_hash` 一致"。
今天不可达（当前还没有执行端），**一旦接上执行器，审批绑定的指纹就会与执行端校验的对不上**，
而那正是这套指纹要防的"批的是 A、执行的是 B"。

所以口径写死在这里：**只取能改变执行语义的三个字段** ——
`action_class`（做什么）、`target`（对谁做）、`params`（怎么做）。
两处对"目标"的写法不同（对象 vs 裸字符串），这里归一。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

__all__ = ["action_fingerprint", "ACTION_FINGERPRINT_FIELDS"]

#: 参与指纹的字段（★ 加字段 = 改变审批绑定的语义，必须同时想清楚两个使用点）
ACTION_FINGERPRINT_FIELDS = ("action_class", "target", "params")


def action_fingerprint(action: Mapping[str, Any]) -> str:
    """审批绑定的动作指纹（稳定序列化后取 sha256 前 16 位）。

    ★ 目标两种写法都认：`{"id": "svc:order@c1"}`（HTTP 层的 `ProposedAction`）
      与裸字符串 `"svc:order@c1"`（工具调用的入参）→ 归一成同一个字符串再算。
    """
    target = action.get("target")
    if isinstance(target, Mapping):
        target_id = target.get("id")
    else:
        target_id = target
    #   ★★ 字段名从 `ACTION_FINGERPRINT_FIELDS` **取**，不再硬编码一遍 ——
    #     以前常量与函数各写一份同样的三个字面量：加字段时改了一处忘了另一处
    #     **不会有任何报错**，而"审批绑定的到底是哪几个字段"是安全语义。
    #     这里用常量做键、按语义取值（`target` 需要归一成 id 字符串）。
    normalized = {"action_class": action.get("action_class"),
                  "target": str(target_id or ""),
                  "params": action.get("params") or {}}
    payload = {field: normalized.get(field) for field in ACTION_FINGERPRINT_FIELDS}
    missing = [field for field in ACTION_FINGERPRINT_FIELDS if field not in normalized]
    if missing:                                  # pragma: no cover - 加字段时的护栏
        raise KeyError(f"ACTION_FINGERPRINT_FIELDS 里的 {missing} 没有归一化规则 —— "
                       f"改这张表时必须同时想清楚两个使用点（HTTP 侧 / 执行端）")
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
