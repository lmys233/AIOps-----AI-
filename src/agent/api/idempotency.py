"""重复请求判定：唯一轮次 id + 抖动窗口内的同内容 → **直接拒绝**（不回放旧答案）。

两条路的判据不同：

· **`turn_id` 相同** → 一律判重复（id 是唯一性键，**与时间窗口无关**：同一轮绝不能跑两次）；
· **仅内容一致**（`message` + `user` + `env` + `cluster` + `options` 的指纹相同）→
  只在**抖动窗口**内判重复。窗口外的一致输入**允许重新排查** —— "用户就是要再问一遍"
  是用户自己的事，是否复用数据由记忆层决定，**不由输入内容决定**。

判定状态落**共享存储**（`ResultStore`，键里带 `session_id`）：多实例一致、重启不丢。
落入失败时**不静默**：读失败按"没判过"放行（fail-open，宁可多跑一次也不误拒），
写失败记一条警告并说明"本次未能持久化"。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Final, Optional
import hashlib
import json
import os
import threading
import time

from ..common.snowflake import new_id
from .constants import (DUPLICATE_WINDOW_JITTER, DUPLICATE_WINDOW_SEC, _ENV_DUPLICATE_WINDOW,
                        logger)
from .errors import DuplicateRequest
from .requests import ChatRequest

__all__ = ["DuplicateGuard", "content_fingerprint", "ensure_turn_id", "window_base_sec",
           "window_sec_for"]

#: 轮次 id 的前缀（雪花 id：`turn_<十进制雪花>`，可解出生成时间）
TURN_ID_PREFIX: Final[str] = "turn"

#: 判定状态的键前缀（同一个 `ResultStore` 里与"事故留存"分开命名空间）
_TURN_KEY: Final[str] = "dup-turn:"
_CONTENT_KEY: Final[str] = "dup-content:"

#: 拒绝时给用户的一句话（**人话**：说清是什么、下一步怎么办）
_REJECT_MESSAGE: Final[str] = ("这是重复请求；要重新排查请换一轮新的 turn_id 或稍后再问")

#: 进程级轮次镜像的条数上限（有界；超了淘汰最早的一条）
_MIRROR_MAX: Final[int] = 4096

#: 进程级镜像：`(session_id, turn_id) → 受理时刻(ms)`。**只覆盖"轮次 id"这一路** ——
#: id 全局唯一，记它不会误判；"内容一致"那条路是会话级窗口判定，以共享存储为准。
#: ★ 只在**共享存储缺席**（进程内实现）时启用：那时它让同一进程里的多个服务壳一致。
_MIRROR: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_MIRROR_LOCK: Final[threading.Lock] = threading.Lock()


from ..common.turns import ensure_turn_id


def content_fingerprint(request: ChatRequest) -> str:
    """内容指纹：`message` + `user` + `env` + `cluster` + `options`（**不含**会话 id）。"""
    payload = {
        "message": request.message,
        "user": request.user.id,
        "env": request.context.env,
        "cluster": request.context.cluster,
        "options": request.options.model_dump(mode="json"),
    }
    if getattr(request.context,'dialogue',None):
        payload['dialogue']=request.context.dialogue.model_dump(mode='json')
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def window_sec_for(key: str, base: float) -> float:
    """该 key 的抖动窗口（秒）：基准 ±20%，由 key 的哈希派生（同 key 稳定、异 key 不同）。"""
    if base <= 0:
        return 0.0
    raw = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big")
    ratio = (raw / 0xFFFFFFFF) * 2.0 - 1.0            # → [-1, 1]
    return max(0.0, float(base) * (1.0 + DUPLICATE_WINDOW_JITTER * ratio))


def window_base_sec() -> float:
    """窗口基准（秒）：读 `AIOPS_DUPLICATE_WINDOW_SEC`；缺省/非法 → 默认值（不猜负数）。"""
    raw = str(os.environ.get(_ENV_DUPLICATE_WINDOW) or "").strip()
    if not raw:
        return DUPLICATE_WINDOW_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字 → 退回默认窗口 %.0fs",
                       _ENV_DUPLICATE_WINDOW, raw, DUPLICATE_WINDOW_SEC)
        return DUPLICATE_WINDOW_SEC
    return max(0.0, value)


class DuplicateGuard:
    """重复判定 + 占用登记（判定状态由 `ResultStore` 承载）。"""

    def __init__(self, results: Any, *, shared: bool = False) -> None:
        self._results = results
        #: 共享存储在场 → **唯一真相就是它**（进程级镜像不参与判定，免得两处各判一半）；
        #: 进程内实现下才启用镜像 —— 否则同一进程里的多个服务壳会各记一份。
        self.shared = bool(shared)

    async def reject(self, session_id: str, turn_id: str, fingerprint: str) -> None:
        """判重复：是重复就抛 `DuplicateRequest`（**不回放**上一次的响应体）。"""
        if turn_id and (self.turn_seen(session_id, turn_id)
                        or await self._stored(session_id, _TURN_KEY + turn_id)):
            raise DuplicateRequest(_REJECT_MESSAGE, turn_id=turn_id, detail="dup_turn_id")
        key = _CONTENT_KEY + fingerprint
        record = await self._stored(session_id, key)
        if record is not None and self._within_window(record, key):
            raise DuplicateRequest(_REJECT_MESSAGE, turn_id=turn_id, detail="dup_content")

    async def claim(self, session_id: str, turn_id: str, fingerprint: str) -> None:
        """占住这一轮（**跑之前**就落盘）：并发/重试的后来者据此判重复。"""
        marker = {"claimed_at_ms": _wall_ms(), "turn_id": turn_id}
        await self._store(session_id, _TURN_KEY + turn_id, marker)
        await self._store(session_id, _CONTENT_KEY + fingerprint, marker)
        if not self.shared:
            _mirror_add(session_id, turn_id)

    def turn_seen(self, session_id: str, turn_id: str) -> bool:
        """这个轮次 id 在本进程里是否已受理过（判定与窗口无关）。"""
        if not turn_id or self.shared:
            return False
        with _MIRROR_LOCK:
            return (session_id, turn_id) in _MIRROR

    @staticmethod
    def _within_window(record: Any, key: str) -> bool:
        """这条登记是否还在**该 key 的抖动窗口**内（时间戳解不出 → 当作不在窗口内）。"""
        try:
            claimed = float((record or {}).get("claimed_at_ms"))
        except (TypeError, ValueError):
            return False
        return (_wall_ms() - claimed) < window_sec_for(key, window_base_sec()) * 1000.0

    async def _stored(self, session_id: str, key: str) -> Optional[dict[str, Any]]:
        """读一条判定登记；读不到/读失败都返回 `None`（**fail-open**：不误拒）。"""
        try:
            return await self._results.get_response(session_id, key)
        except Exception as exc:  # noqa: BLE001 —— 判定读不到不该让用户连问都问不了
            logger.warning("重复判定读取失败（%s）：%s —— 本次按「没判过」继续",
                           type(exc).__name__, exc)
            return None

    async def _store(self, session_id: str, key: str, marker: dict[str, Any]) -> None:
        """写一条判定登记；写失败**说出来**（"没持久化"不许静默当成"已经记过了"）。"""
        try:
            await self._results.put_response(session_id, key, dict(marker))
        except Exception as exc:  # noqa: BLE001 —— 记不住就去重打折，但不能因此拒绝服务
            logger.warning("重复判定**未能持久化**（%s）：%s —— 多实例去重会打折",
                           type(exc).__name__, exc)


def _mirror_add(session_id: str, turn_id: str) -> None:
    """把轮次 id 记进进程级镜像（有界：超上限淘汰最早的），让同进程的多个服务壳一致。"""
    if not turn_id:
        return
    with _MIRROR_LOCK:
        _MIRROR[(session_id, turn_id)] = _wall_ms()
        _MIRROR.move_to_end((session_id, turn_id))
        while len(_MIRROR) > _MIRROR_MAX:
            _MIRROR.popitem(last=False)


def _wall_ms() -> float:
    """**墙钟**毫秒（判定要跨进程比对，不能用进程内的单调时钟）。"""
    return time.time() * 1000.0
