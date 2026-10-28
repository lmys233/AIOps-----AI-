"""实现告警接入：去重/抑制队列、批量与并发闸门。"""

from __future__ import annotations

from ..common.enums import ALERT_SUPPRESS_WINDOW_MINUTES
from ..orchestration import new_incident_id
from typing import Any, Final, Mapping, Optional, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import os
import time


#: 告警抑制窗口（分钟）
ALERT_SUPPRESS_WINDOW_SEC: Final[float] = ALERT_SUPPRESS_WINDOW_MINUTES * 60.0

#: `/v1/alerts` 单批上限：一个请求最多入队多少条告警。
#: ★ Alertmanager 的 `max_alerts` 默认远小于此，超过它一定是异常调用或攻击。
ALERT_MAX_BATCH: Final[int] = 20

#: 同时在跑的告警排查上限 —— 防"一次扇出把模型账单打上去"。
ALERT_MAX_CONCURRENCY: Final[int] = 4

#: ★★ **全局吞吐配额** —— 一个抑制窗口内允许"真正入队"的告警总数。
#: 每请求 20 条挡不住"重复发不同指纹的批次"，这一条才是账单的硬上界。
#: ★ 可被环境变量覆盖（`AIOPS_ALERT_MAX_PER_WINDOW`）：拒绝话术里承诺了这个旋钮，
#:   就必须真的能设 —— 而 `.env` 是在 `create_app()` 里才装载的，所以这里**不在
#:   import 期读**，改成 `AlertQueue` 构造时读（`default_factory`）。
ALERT_MAX_PER_WINDOW: Final[int] = 60
_ENV_ALERT_QUOTA: Final[str] = "AIOPS_ALERT_MAX_PER_WINDOW"


def _quota_from_env(default: int = ALERT_MAX_PER_WINDOW) -> int:
    """读取环境变量里的全局配额（取不到/写错 → 用默认值，**不炸**）。"""
    raw = (os.environ.get(_ENV_ALERT_QUOTA) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


#: ★ 指纹表的硬上界（窗口外的指纹本来就没用了）
ALERT_MAX_SEEN: Final[int] = 4096

#: ★ 备注表的硬上界（只留最近若干条）
ALERT_MAX_NOTES: Final[int] = 200

#: ★ 幂等映射（fingerprint → incident_id）的上界。
#: 比"窗口内指纹数"大得多：它要服务跨窗口的"同一告警一个事件号"。
ALERT_MAX_IDENTITIES: Final[int] = 8192

# ======================================================================
#  告警接入（按 fingerprint 幂等 + 抑制窗口 + 202 不阻塞）
# ======================================================================
@dataclass
class AlertDecision:
    """承载一条告警的处置结论（`accepted=False` 时**原因必须写出来**）。"""

    fingerprint: str
    incident_id: str
    accepted: bool
    suppressed: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"fingerprint": self.fingerprint, "incident_id": self.incident_id,
                "accepted": self.accepted, "suppressed": self.suppressed,
                "reason": self.reason}

@dataclass
class AlertQueue:
    """把一批告警按 fingerprint 去重，并判定抑制与配额。

    ★ 为什么"抑制窗口"与"幂等"要分开写：

    | 概念 | 判据 | 行为 |
    |---|---|---|
    | **幂等** | 同 `fingerprint` | 永远映射到**同一个 incident_id**（不会起第二个排查） |
    | **抑制** | 同 `fingerprint` 在窗口内（默认 5min）**再次**上报 | 不重复触发（`accepted=False, suppressed=True`），但仍然返回 202 |
    | 窗口过后再来 | 超过窗口 | 视为**新的一次告警** → 起一次新排查（但复用同一个 fingerprint→新 incident 的映射） |

    ★ 无 `fingerprint` 的告警**不静默丢弃**：按内容哈希兜底一个（并记 `note`），
      否则 Alertmanager 少填一个字段就会变成"告警吞了"。
    """

    window_sec: float = ALERT_SUPPRESS_WINDOW_SEC
    #: fingerprint → **最近一次触发排查的时刻**（只服务"抑制窗口"，窗口一过即可回收）
    _seen: dict[str, float] = field(default_factory=dict)
    #: ★★ fingerprint → `incident_id`（服务**幂等**，必须活得比抑制窗口长）。
    #:   以前这两件事挤在一个 `_seen` 里，而 `submit()` 一进来就把窗口外的条目删掉 ——
    #:   于是"窗口已过 → 沿用同一事件号"那一支**永远走不到**，同一个真实告警跨窗口
    #:   会被拆成**两个 incident**（Agent 侧当成两起）。这里的不变量是
    #:   "一个 fingerprint 一个事件号"，所以这里必须分开存。
    _incidents: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: ★★ `_seen` / `notes` 都是"只写不读"的容器 —— 以前**永不回收**，
    #:   随告警量线性增长。指纹条目**只在抑制窗口内有用**（窗口一过就当新告警），
    #:   所以过期的条目可以直接丢；`notes` 只留最近若干条。
    max_seen: int = ALERT_MAX_SEEN
    max_notes: int = ALERT_MAX_NOTES
    #: 幂等映射的上界（**与抑制窗口无关**：它要活得比窗口长）
    max_identities: int = ALERT_MAX_IDENTITIES
    #: 因容量被丢弃的指纹（**有界**的最近若干条）——"不是没登记，是被挤掉了"
    _evicted_identities: list[str] = field(default_factory=list)
    #: ★ 上面那句"有界"以前是**空头承诺**（只 append 从不裁剪）。
    #:   这个上界就是它的承兑：保留最近 64 条（够解释"最近为什么被挤掉"）。
    max_evicted_identities: int = 64
    #: ★★ **全局吞吐配额**（滑动窗口内允许真正入队的告警数）。
    #:   每请求 20 条的上限挡不住"重复发不同指纹的批次" —— 那仍然持续烧钱。
    max_per_window: int = field(default_factory=_quota_from_env)
    #: 最近**入队**（真跑）的时刻，用于全局配额（滑动窗口）
    _admitted: list[float] = field(default_factory=list)

    # ------------------------------------------------------------------
    def _evict_expired(self, moment: float) -> None:
        """丢掉**窗口外**的触发时刻（它对"要不要再触发"已经没有任何用处）。

        ★ 只动 `_seen`：`_incidents` 是幂等映射，**不能**跟着窗口一起过期；
          它的有界性由 `_evict_identities()` 单独负责。
        """
        expired = [key for key, last_at in self._seen.items()
                   if moment - last_at >= self.window_sec]
        for key in expired:
            self._seen.pop(key, None)
        # ★★ `max_seen` 那道**硬上界**曾经被漏掉 ——
        #   窗口回收只对"窗口内的高频"有效，灌不重复的指纹仍能把它顶上去。
        #   （字段还在，所以没有任何测试发现它已经不生效。）
        while len(self._seen) > self.max_seen:
            oldest = min(self._seen.items(), key=lambda item: item[1])[0]
            self._seen.pop(oldest, None)
        self._evict_identities()

    def _evict_identities(self) -> None:
        """给幂等映射设硬上界：超了就丢**最早登记**的那些，并**如实留痕**。

        ★ 为什么可以丢：无界 = 内存随告警量线性增长。
          为什么必须留痕：丢了就意味着"同一个 fingerprint 会铸新的事件号"——
          那正是幂等承诺的破口，用户必须能看到"这里是容量导致的"，而不是以为系统正常。
        """
        while len(self._incidents) > self.max_identities:
            oldest = next(iter(self._incidents))
            self._incidents.pop(oldest, None)
            self._evicted_identities.append(oldest)
            self._note(f"幂等映射超过上限（{self.max_identities}）→ "
                       f"丢弃最早的 {oldest}（该指纹下次会铸新事件号）")
        # ★★ `_evicted_identities` 以前**只 append 从不裁剪**，
        #   而它的注释写着"**有界**的最近若干条"—— 注释与实现相反。
        #   `/v1/alerts` 是对网络开放的端点：灌不同指纹即可推动它无界增长。
        #   → 按注释许诺的上界裁剪（新条目在右，保留最近 N 条）。
        if len(self._evicted_identities) > self.max_evicted_identities:
            del self._evicted_identities[:-self.max_evicted_identities]

    def _note(self, text: str) -> None:
        self.notes.append(text)
        while len(self.notes) > self.max_notes:
            self.notes.pop(0)

    def _quota_left(self, moment: float) -> int:
        """算出本窗口内还剩多少次"真正入队"的额度。"""
        cutoff = moment - self.window_sec
        self._admitted = [at for at in self._admitted if at > cutoff]
        return max(0, self.max_per_window - len(self._admitted))

    def _admit(self, moment: float) -> None:
        self._admitted.append(moment)

    def submit(self, alerts: Sequence[Mapping[str, Any]], *,
               now: Optional[float] = None) -> list[AlertDecision]:
        moment = time.monotonic() if now is None else float(now)
        self._evict_expired(moment)
        out: list[AlertDecision] = []
        for raw in alerts:
            out.append(self._decide_one(raw, moment))
        return out

    def _decide_one(self, raw: Mapping[str, Any], moment: float) -> AlertDecision:
        """处置一条告警：幂等 → 抑制 → **全局配额** → 入队。"""
        fingerprint = str(raw.get("fingerprint") or "").strip()
        if not fingerprint:
            fingerprint = "fp_auto_" + hashlib.sha256(
                json.dumps(raw, sort_keys=True, ensure_ascii=False,
                           default=str).encode("utf-8")).hexdigest()[:12]
            self._note(f"告警没有 fingerprint → 按内容哈希兜底 {fingerprint}")
        # ★★ 事件号来自**独立的幂等映射**：跨窗口也沿用，不再铸新的
        known_id = self._incidents.get(fingerprint)
        last_at = self._seen.get(fingerprint)
        if known_id is not None and last_at is not None \
                and moment - last_at < self.window_sec:
            # ① 窗口内重复上报 → 抑制（幂等 + 抑制都成立）
            return AlertDecision(
                fingerprint=fingerprint, incident_id=known_id,
                accepted=False, suppressed=True,
                reason=(f"同一 fingerprint 在抑制窗口内（{moment - last_at:.0f}s < "
                        f"{self.window_sec:.0f}s）→ 不重复触发（spec §6.4 要求②）"))
        if known_id is not None:
            # ② 窗口已过（或首次的抑制时刻已过期）→ 允许再触发，但**事件号沿用**
            if self._quota_left(moment) <= 0:
                return self._quota_denied(fingerprint, known_id)
            self._seen[fingerprint] = moment
            self._admit(moment)
            return AlertDecision(
                fingerprint=fingerprint, incident_id=known_id, accepted=True,
                reason=(f"抑制窗口已过 → 重新触发（**沿用同一事件号**："
                        f"同一 fingerprint 只有一个 incident）"))
        # ③ 首次接入 → 铸新事件号
        incident_id = new_incident_id()
        if self._quota_left(moment) <= 0:
            return self._quota_denied(fingerprint, incident_id)
        self._incidents[fingerprint] = incident_id
        self._seen[fingerprint] = moment
        self._admit(moment)
        return AlertDecision(fingerprint=fingerprint, incident_id=incident_id,
                             accepted=True, reason="首次接入 → 入队")

    def _quota_denied(self, fingerprint: str, incident_id: str) -> AlertDecision:
        """在配额用尽时**如实拒绝**（不静默丢、也不假装入队）。"""
        self._note(f"全局配额用尽（{self.max_per_window}/{self.window_sec:.0f}s）"
                   f"→ 拒绝入队 {fingerprint}")
        return AlertDecision(
            fingerprint=fingerprint, incident_id=incident_id, accepted=False,
            suppressed=False,
            reason=(f"全局吞吐配额已用尽（{self.max_per_window} 次 / "
                    f"{self.window_sec:.0f}s）→ 本次不入队，请稍后重试或提高配额"
                    f"（AIOPS_ALERT_MAX_PER_WINDOW）"))

    def snapshot(self) -> dict[str, Any]:
        return {"window_sec": self.window_sec,
                "fingerprints": {key: {"incident_id": incident}
                                 for key, incident in self._incidents.items()},
                "identities": len(self._incidents),
                "max_identities": self.max_identities,
                "evicted_identities": list(self._evicted_identities[-5:]),
                # ★ 有界性 + 全局配额都要**看得见**（否则排障时只能猜）
                "fingerprint_count": len(self._seen),
                "max_seen": self.max_seen,
                "quota": {"max_per_window": self.max_per_window,
                          "used_in_window": len(self._admitted)},
                "notes": list(self.notes[-5:])}
