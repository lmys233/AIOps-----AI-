"""雪花 id：**携带时间戳**的单调 id（会话 id 与对话事件的同一规则族）。

结构：41 位毫秒时间戳 + 10 位 worker + 12 位序号（共 63 位，非负）。

★ 三条性质是它存在的理由：

· **可解出时间**：拿到 id 就知道它是什么时候铸的（排查"这条会话是刚才的还是上周的"
  不必再查表）；
· **同毫秒不撞**：同一毫秒内序号递增，序号用满就等下一毫秒 —— 于是"一毫秒内连开两个
  会话"不会得到同一个 id（`Date.now()` 拼出来的 id 就会撞）；
· **单调**：时钟回拨时沿用上一毫秒，宁可停表也不让新 id 比旧 id 小。

线程安全由**一把锁**保证：同一毫秒内并发调用各自拿到不同的序号。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Final

__all__ = ["EPOCH_MS", "TIMESTAMP_BITS", "WORKER_BITS", "SEQUENCE_BITS",
           "new_id", "id_timestamp_ms"]

#: 项目纪元（固定，**不许改**）：改它会让已经铸出的 id 解出另一个时间。
EPOCH_MS: Final[int] = 1_700_000_000_000

#: 位宽（合计 63 位，最高位留空以免出现负数）
TIMESTAMP_BITS: Final[int] = 41
WORKER_BITS: Final[int] = 10
SEQUENCE_BITS: Final[int] = 12

_SEQUENCE_MASK: Final[int] = (1 << SEQUENCE_BITS) - 1
_WORKER_MASK: Final[int] = (1 << WORKER_BITS) - 1
_WORKER_SHIFT: Final[int] = SEQUENCE_BITS
_STAMP_SHIFT: Final[int] = SEQUENCE_BITS + WORKER_BITS

#: worker 位取进程号：同机多实例（或"重启后的新进程"）也不会互相撞号。
_WORKER_ID: Final[int] = os.getpid() & _WORKER_MASK

_lock: Final[threading.Lock] = threading.Lock()
_last_ms: int = -1
_sequence: int = 0


def new_id(prefix: str) -> str:
    """铸一个新 id，形如 `<prefix>_<十进制雪花>`（同毫秒内多次调用互不相同）。"""
    return f"{str(prefix).strip()}_{_next_number()}"


def id_timestamp_ms(value: str) -> int:
    """从 id 解出毫秒时间戳；**解不出就抛 `ValueError`**（不猜、不返回 0）。"""
    text = str(value or "").strip()
    digits = text.rsplit("_", 1)[-1] if "_" in text else text
    if not digits.isdigit():
        raise ValueError(f"不是雪花 id（末尾不是十进制数字段）：{value!r}")
    return (int(digits) >> _STAMP_SHIFT) + EPOCH_MS


def _next_number() -> int:
    """在同一毫秒内递增序号；序号用满则等到下一毫秒（**单调** + 线程安全）。"""
    global _last_ms, _sequence
    with _lock:
        now = _now_ms()
        if now < _last_ms:                 # 时钟回拨：沿用上一毫秒，不让 id 倒退
            now = _last_ms
        if now == _last_ms:
            _sequence += 1
            if _sequence > _SEQUENCE_MASK:
                now = _wait_next_ms(_last_ms)
                _sequence = 0
        else:
            _sequence = 0
        _last_ms = now
        return ((now - EPOCH_MS) << _STAMP_SHIFT) | (_WORKER_ID << _WORKER_SHIFT) | _sequence


def _now_ms() -> int:
    return int(time.time() * 1000)


def _wait_next_ms(last_ms: int) -> int:
    """等到时间越过 `last_ms`（序号溢出时的**唯一**等待点；每毫秒最多一次）。"""
    now = _now_ms()
    while now <= last_ms:
        time.sleep(0.0005)
        now = _now_ms()
    return now
