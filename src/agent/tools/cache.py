"""查询结果**本地缓存**（用户指示：「缓存查询记录到本地（而不是 redis），用时间戳作为键」）。

★★ 为什么是「缓存」而不是「状态」

**状态**（会话闸门 / 配额 / 待确认动作 / 检查点）必须外置到共享存储 ——
那说的是「重启与多实例下**不许丢**」的东西。而查询结果是**可重取的副本**：
丢了只是慢一点。把它塞进 Redis 去冒充共享状态，会让「缓存」拥有它不该有的权威；
落到本地还多一个好处：**同一次排查的原始取数能被回放**（`raw_ref` 指得到具体文件）。

★★ 关键设计：**闭合窗可长期复用，含「现在」的窗不可**

| 窗口 | 会发生什么 | 处置 |
|---|---|---|
| `end` 距现在已超过一个抓取周期（**闭合窗**） | 这段历史**不会被改写** | 落盘后**长期复用**（保留期内） |
| `end` 落在「现在」（**开窗**） | 尾部**仍在写入**（新样本还在进来） | 不缓存，或极短 TTL |

★ 为什么必须区分：把开窗的旧结果当"最新"用，会把「**还没发生**」读成「**没有发生**」——
这是同一族的错（"没查到" ≠ "没有异常"）。所以开窗只给**秒级** TTL。

★ 键 = `(数据源, 查询语句, start, end, step)` 的稳定摘要（**时间戳就是键的一部分**，
   因为窗口本身就是时间戳），文件里另存 `fetched_at`（**命中时要说清"这是什么时候取的"**）。

★ fail-open：读不到就查、写不进就照常返回，**任何 OSError 都不许影响取数**
   （缓存的职责是省时间，不是当单点）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Optional

__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_RETENTION_SEC",
    "DEFAULT_MEMORY_FRESHNESS_SEC",
    "MEMORY_FRESHNESS_ENV",
    "OPEN_WINDOW_TTL_SEC",
    "CacheKey",
    "LocalQueryCache",
    "cache_dir_for",
    "memory_freshness_sec",
    "memory_is_fresh",
    "memory_window_closed",
]

logger = logging.getLogger("agent.tools.cache")

#: 缓存目录（相对于仓库根；也可用 `AIOPS_QUERY_CACHE_DIR` 覆盖）
DEFAULT_CACHE_DIR: Final[str] = ".run/cache/queries"
#: 保留期：与"太久远的大概率已被修复"同口径（7 天）
DEFAULT_RETENTION_SEC: Final[float] = 7 * 86400.0
#: ★★ 开窗数据复用门槛的**唯一来源**：环境变量名（默认 60 秒）。
MEMORY_FRESHNESS_ENV: Final[str] = "AIOPS_MEMORY_FRESHNESS_SEC"
#: ★★ 门槛默认值：**60 秒**（"允许 1 分钟脏数据"）。
#:
#: ★ 为什么含"现在"的窗口只给 60s：尾部样本还在往里写，复用一份太旧的，
#:   会把「还没发生」读成「没有发生」。**这个数字只有这一处** ——
#:   提示词里那条规则（`now - 抓取时间 ≤ 60s`）与工具层的判断都从它派生，
#:   两边各写一份必然漂移（模型说"可以复用"、取数层却仍然重取，或反过来）。
DEFAULT_MEMORY_FRESHNESS_SEC: Final[float] = 60.0
#: 开窗（`end` 落在"现在"）的最长复用时间（秒）——**与提示词那条规则同一口径**。
#:
#: ★ 历史：这里曾是 120s（真机实测一轮排查要 20~60s，条目在下一问之前就超期）。
#:   改成 60s 后，"这份数据还新不新"在全系统只有一个答案。
#: ★ 要更严就设 `AIOPS_MEMORY_FRESHNESS_SEC=0`（等于不用开窗复用）。
OPEN_WINDOW_TTL_SEC: Final[float] = DEFAULT_MEMORY_FRESHNESS_SEC

#: ★★ 时间桶（秒）：键里的 `start`/`end` 按它向下对齐。
#:
#: 取值的两难（实测）：桶太小（如 = 步长 30s）则"再问一次"几乎必然跨桶；
#: 桶太大则复用到的窗口离"现在"更远。**一个取数周期的量级（120s）** 是能真正命中的组合，
#: 对齐偏差最多两个取数周期、且**写在 `raw_ref` 里**；开窗那一份到底还能不能用，
#: 由 `OPEN_WINDOW_TTL_SEC` 说了算（桶只决定"键能不能对上"）。
#: ★ 可用 `AIOPS_QUERY_CACHE_BUCKET_SEC` 覆盖。
DEFAULT_BUCKET_SEC: Final[float] = 120.0
#: 目录里最多留多少条（超了按 mtime 淘汰最旧的）
DEFAULT_MAX_ENTRIES: Final[int] = 2000
#: 抓取周期级别的"这算闭合了吗"（10s 抓取 → 留一个周期的余量）
CLOSED_MARGIN_SEC: Final[float] = 15.0


def cache_dir_for(environ: Optional[dict[str, str]] = None) -> Path:
    """缓存目录：`AIOPS_QUERY_CACHE_DIR` > 默认 `.run/cache/queries`（**沙箱内可写**）。"""
    env = os.environ if environ is None else environ
    raw = str(env.get("AIOPS_QUERY_CACHE_DIR") or "").strip()
    if raw:
        return Path(raw)
    root = Path(__file__).resolve().parents[3]        # <repo>
    return root / DEFAULT_CACHE_DIR


def memory_freshness_sec(environ: Optional[dict[str, str]] = None) -> float:
    """开窗数据的复用门槛（秒）：`AIOPS_MEMORY_FRESHNESS_SEC` > 默认 60（配错不炸）。"""
    env = os.environ if environ is None else environ
    raw = str(env.get(MEMORY_FRESHNESS_ENV) or "").strip()
    if not raw:
        return float(OPEN_WINDOW_TTL_SEC)
    try:
        value = float(raw)
    except ValueError:                     # 配错了不炸：退回默认
        return float(OPEN_WINDOW_TTL_SEC)
    return max(0.0, value)


def memory_window_closed(end: Any, *, now: float) -> bool:
    """这段窗口**已经结束了吗**（`end` 已过去 → 数据不可变，可长期复用）。"""
    value = _as_float(end)
    return value is not None and value < float(now)


def memory_is_fresh(record: Any, *, now: float, freshness: Optional[float] = None) -> bool:
    """这条记忆现在还能不能**直接复用**（纯判断，不含任何 IO）。

    · **闭合窗**（`end` 已过去）→ 可以复用（这段历史不会被改写）；
    · **开窗**（含"现在"）→ 仅当 `now - fetched_at ≤ 门槛`（默认 60s，允许 1 分钟脏数据）。

    ★ 字段读不到/认不出 → **False**（fail-closed：宁可变慢，也不拿旧数据当新数据）。
    """
    end = _as_float(_read(record, "end"))
    fetched = _as_float(_read(record, "fetched_at"))
    stamp = float(now)
    if end is None or fetched is None:
        return False
    if memory_window_closed(end, now=stamp):
        return True
    if fetched > stamp:                    # 未来时间戳 = 垃圾值
        return False
    limit = memory_freshness_sec() if freshness is None else max(0.0, float(freshness))
    return (stamp - fetched) <= limit


def _read(record: Any, name: str) -> Any:
    """双通道读一条记忆字段：`getattr` 优先，读不到退 `obj[name]`（dict 也认）。"""
    try:
        value = getattr(record, name)
    except Exception:                      # noqa: BLE001 —— 记录形状不该让判断失败
        value = None
    if value is not None:
        return value
    try:
        return record[name]
    except Exception:                      # noqa: BLE001
        return None


def _as_float(value: Any) -> Optional[float]:
    """把字段读成 epoch 秒（读不到/认不出 → `None`，不猜）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CacheKey:
    """一次查询的身份（★ **时间戳就是键的一部分**，且要**分桶**）。"""

    source: str
    query: str
    start: float
    end: float
    step: str

    def bucketed(self, bucket_sec: float) -> "CacheKey":
        """把 `start`/`end` **向下对齐**到一个时间桶。

        ★★ 为什么必须分桶（真机实测撞出来的）：实时提问的窗口 `end` 就是"**现在**" ——
          两次提问相差几毫秒，`end` 就不同 → 键不同 → **缓存永远不命中**（等于没做缓存）。
          按 step 量级分桶（如 30s）后，"同一分钟里的同一个问题"才是同一个键。
        ★ 代价是对齐带来的**最多一个桶**的偏差 —— 必须**说出来**（见 :meth:`bucket_note`），
          不能让调用方以为拿到的正好是他要的那个窗口。
        """
        size = max(1.0, float(bucket_sec))
        floor = lambda value: float(int(value // size) * size)  # noqa: E731
        return CacheKey(source=self.source, query=self.query, start=floor(self.start),
                        end=floor(self.end), step=self.step)

    @property
    def digest(self) -> str:
        raw = "|".join([self.source, self.query, f"{self.start:.3f}",
                        f"{self.end:.3f}", self.step])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    @property
    def stamp(self) -> str:
        """可读的时间戳（文件名用）：`<end 的 epoch 整数>` —— 用户要的"以时间戳作键"。"""
        return str(int(self.end))

    def filename(self) -> str:
        return f"{self.source.lower()}-{self.stamp}-{self.digest}.json"

    @staticmethod
    def bucket_note(bucket_sec: float) -> str:
        """对齐偏差的**如实说明**（进 `raw_ref`）。"""
        return f"窗口按 {bucket_sec:.0f}s 对齐（最多偏差一个桶）"


class LocalQueryCache:
    """落盘的查询结果缓存（**只读一次、可回放**；失败一律不影响取数）。"""

    def __init__(self, directory: Optional[Path] = None, *,
                 retention_sec: float = DEFAULT_RETENTION_SEC,
                 open_ttl_sec: Optional[float] = None,
                 max_entries: int = DEFAULT_MAX_ENTRIES,
                 now: Optional[Callable[[], float]] = None) -> None:
        self.directory = Path(directory) if directory is not None else cache_dir_for()
        self.retention_sec = float(retention_sec)
        # ★ 默认**跟环境变量走**（门槛只有一个来源）；显式传值只为单测钉住某个数
        self.open_ttl_sec = (memory_freshness_sec() if open_ttl_sec is None
                             else float(open_ttl_sec))
        self.max_entries = int(max_entries)
        self._now = now or time.time

    # ------------------------------------------------------------------
    def path_of(self, key: CacheKey) -> Path:
        return self.directory / key.filename()

    def is_closed(self, end: float) -> bool:
        """这段窗口**已经结束了吗**（结束得够久 = 不会被改写了）。"""
        return float(end) <= (self._now() - CLOSED_MARGIN_SEC)

    # ------------------------------------------------------------------
    def _trace(self, action: str, key: "CacheKey", detail: str = "") -> None:
        """★ 可选诊断：`AIOPS_QUERY_CACHE_DEBUG=1` 时把每次命中/未命中写一行到 `_debug.log`。

        ★ 为什么要留这个口子：「缓存为什么没命中」是 ops 一定会问的问题，
          而它**不能靠猜**（我实测就猜错过一次）。默认关闭、失败静默。
        """
        if str(os.environ.get("AIOPS_QUERY_CACHE_DEBUG") or "") not in ("1", "true", "on"):
            return
        try:
            line = (f"{time.strftime('%H:%M:%S')} {action} {key.filename()} "
                    f"{detail}\n")
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / "_debug.log").open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            return

    def get(self, key: CacheKey) -> Optional[dict[str, Any]]:
        """读缓存。**任何异常都返回 `None`**（= 当作没缓存，去查真实数据）。"""
        path = self.path_of(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._trace("MISS-FILE", key, type(exc).__name__)
            return None
        if not isinstance(payload, dict):
            return None
        fetched_at = float(payload.get("fetched_at") or 0.0)
        age = max(0.0, self._now() - fetched_at)
        # ★★ 判据看的是「**抓取时**这段窗口闭合没有」，**不是**「现在闭合没有」。
        #   为什么（实测撞出来的）：开窗时抓的那一份，**尾部本来就是不全的**
        #   （后面的样本还没产生）。等 30 秒后窗口"闭合"了再拿它来用，等于**永久丢掉**
        #   那 30 秒的样本 —— 而它看起来像一份完整的闭合窗数据（最坏的一种错：
        #   "不完整"被伪装成"完整"）。所以开窗抓的条目只给秒级 TTL，过期就去重取
        #   （重取时窗口多半已闭合，那一份才是可长期复用的）。
        if not bool(payload.get("closed")) and age > self.open_ttl_sec:
            self._trace("MISS-OPEN-TTL", key, f"age={age:.1f}s")
            return None
        if age > self.retention_sec:
            self._trace("MISS-RETENTION", key, f"age={age:.1f}s")
            return None
        self._trace("HIT", key, f"age={age:.1f}s closed={payload.get('closed')}")
        payload["cache"] = {"hit": True, "fetched_at": fetched_at,
                            "age_sec": round(age, 1),
                            "closed": bool(payload.get("closed")),
                            "captured_open": not bool(payload.get("closed"))}
        return payload

    def put(self, key: CacheKey, payload: dict[str, Any], *,
            open_window: bool = False) -> None:
        """写缓存（**失败静默**：写不进只是下次再查一次）。

        ★★ `open_window` 由调用方按**原始（未分桶）的 `end`** 判定后传进来 ——
          这一点是实测撞出来的坑：分桶会把 `end` 往前挪最多一个桶，
          若在这里用**分桶后的** `end` 反推开/闭，一份"抓取时尾部还不全"的数据
          会被记成"已闭合"，于是**长期**被复用（永久丢掉那一段样本）。
        ★ `window_end` 记的是**分桶后实际取到的窗口末端**（数据覆盖到哪），
          `requested_end` 记用户原本要的（对齐偏差可追溯）。
        """
        path = self.path_of(key)
        body = {"fetched_at": self._now(), "source": key.source, "query": key.query,
                "start": key.start, "end": key.end, "step": key.step,
                "closed": not bool(open_window), "payload": payload}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)                      # 原子替换：读到的永远是完整文件
        except OSError as exc:                     # 只记日志，不抛
            logger.debug("查询缓存写入失败（%s）→ 本次不缓存", type(exc).__name__)
            return
        self._prune()

    def _prune(self) -> None:
        """按 mtime 淘汰最旧的条目（有界；失败静默）。"""
        try:
            files = sorted(self.directory.glob("*.json"),
                           key=lambda item: item.stat().st_mtime)
        except OSError:
            return
        for old in files[:max(0, len(files) - self.max_entries)]:
            try:
                old.unlink()
            except OSError:
                continue

    def note_of(self, payload: dict[str, Any]) -> str:
        """命中时进 `raw_ref` 的**留痕**（"这份数据是什么时候取的"必须可追溯）。"""
        cache = payload.get("cache") or {}
        stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(float(cache.get("fetched_at") or 0.0)))
        kind = "闭合窗" if cache.get("closed") else "开窗抓取、尾部可能不全"
        return (f"本地缓存命中（{kind}，原始抓取于 {stamp}，"
                f"{cache.get('age_sec')}s 前）")
