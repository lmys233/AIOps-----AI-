"""读数**新鲜度**：一个数是什么时候采到的，以及据此**不能说什么**。

★★ 为什么单列：同一个判断要在两条成文路径上一致 ——
  值问答的结构化答案（`agents/analyst.py`）与最终白话（`api/answer.py`）。
  各写一份的后果实测过：一边带采样时刻、另一边仍说"当前正常"。

★ 三条硬纪律：

  ① 取不到时间戳就**留空**（"不知道这是什么时候的数" ≠ "就是此刻的数"）；
  ② 末次采样超过 `METRIC_STALE_SEC` → 不许说「当前正常 / 没有异常」，
     改写成「最近一次采样（时刻 X）没有异常迹象；**这不是此刻的读数**」；
  ③ 改写**不是删掉不表**：把"我没有此刻的数据"这件事如实说出来。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Optional, Sequence

from .thresholds import METRIC_STALE_SEC

__all__ = ["METRIC_STALE_SEC", "freshness_of", "neutralize_stale_claim",
           "join_caveat", "freshness_text"]

#: 「此刻正常」类断言：数据不新鲜时**不许**说
CURRENCY_CLAIM: Final[re.Pattern[str]] = re.compile(
    r"(当前|现在|目前|此刻)[^。；，,\n]{0,8}?(没有异常|无异常|一切正常|运行正常|正常)")


def freshness_of(results: Sequence[Mapping[str, Any]]) -> tuple[str, Optional[float]]:
    """这次结果**采样到什么时候**：返回 `(ISO 时刻, 距今秒数)`；取不到就给空。"""
    stamps = [float((item.get("data") or {}).get("last_ts") or 0.0)
              for item in results if isinstance(item.get("data"), Mapping)]
    landed = [stamp for stamp in stamps if stamp > 0]
    if not landed:
        return "", None
    newest = max(landed)
    stamp = datetime.fromtimestamp(newest, timezone.utc)
    return stamp.isoformat(timespec="seconds"), round(time.time() - newest, 1)


def neutralize_stale_claim(text: str, staleness_sec: Optional[float],
                           as_of: str = "") -> str:
    """数据不新鲜时，把「当前正常」改写成**成立的**说法（保留"没看到异常迹象"这半句）。"""
    if not text or staleness_sec is None or staleness_sec <= METRIC_STALE_SEC:
        return text
    when = f"（末次采样 {as_of}）" if as_of else ""
    return CURRENCY_CLAIM.sub(
        lambda hit: (f"{hit.group(1)}只能按**末次采样**说{when}："
                     f"那一次读数没有异常迹象，**这不是此刻的读数**"), text)


def join_caveat(caveats: str, staleness_sec: float, as_of: str) -> str:
    """把"这是旧样本"写进口径（**必须带具体时刻**，不能只说"数据可能不新"）。"""
    note = (f"★ 末次采样 {as_of or '（时刻未知）'}（距今 {staleness_sec:.0f}s）——"
            f"**这不是此刻的读数**；要说「现在正常」需要重新取一次。")
    return f"{caveats}；{note}" if caveats else note


def freshness_text(metric: Mapping[str, Any]) -> str:
    """读数新鲜度一句话：`末次采样 <时刻>（距今 <n>s）`；不知道就说不知道。"""
    as_of = str(metric.get("as_of") or "")
    stale = metric.get("staleness_sec")
    if not as_of:
        return "末次采样时刻未知（因此**不能**据此说此刻是否正常）"
    if stale is None:
        return f"末次采样 {as_of}"
    return f"末次采样 {as_of}（距今 {float(stale):.0f}s）"
