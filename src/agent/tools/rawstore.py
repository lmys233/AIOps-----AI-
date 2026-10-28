"""原始载荷的**本地副本**（落盘可回放；`raw_read` 只从这里读）。

★★ 它解决的是"模型只看到处理过的摘要，却以为自己看到了全部数据"这个问题：

  · 抓取时把**原始载荷**落盘（`save_raw`），结果里的 `raw_ref` 就是取回它的钥匙；
  · 模型需要原文时自己回取（`raw_read`），**不重发上游请求**、也不改变判据。

★ 三条纪律：

  1. **不猜、不重发**：`load_raw` 只读磁盘，找不到就 `None`（"找不到"必须与
     "读失败"分开 —— 前者是这份副本没了，后者是磁盘不让你读）；
  2. **有界**：按 mtime 淘汰（`prune_raw`），不许把磁盘当无限归档；
  3. **fail-open**：写不进只是"这次没留副本"，**绝不影响取数**（缓存的职责是省时间）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Final, Optional

__all__ = [
    "DEFAULT_RAW_DIR",
    "DEFAULT_KEEP",
    "raw_dir",
    "raw_path",
    "save_raw",
    "load_raw", "resolve_raw_ref",
    "prune_raw",
]

logger = logging.getLogger("agent.tools.rawstore")

#: 原始数据的存放目录（相对仓库根；可用 `AIOPS_RAW_DIR` 覆盖）
DEFAULT_RAW_DIR: Final[str] = ".run/cache/raw"
#: 最多留几份（超了按 mtime 淘汰最旧的）
DEFAULT_KEEP: Final[int] = 500
#: 文件名里保留几个字符的 `raw_ref` 前缀（**只为可读**，唯一性靠摘要）
_PREFIX_CHARS: Final[int] = 24


def raw_dir(environ: Optional[dict[str, str]] = None) -> Path:
    """原始数据目录：`AIOPS_RAW_DIR` > 默认 `.run/cache/raw`。"""
    env = os.environ if environ is None else environ
    from ..observed.context import current
    scope=current()
    raw = str(env.get("AIOPS_RAW_DIR") or "").strip()
    if raw:
        base=Path(raw)
    else:base=Path(__file__).resolve().parents[3] / DEFAULT_RAW_DIR
    return base/scope.namespace if scope else base


def _slug(raw_ref: str) -> str:
    """文件名前缀：只留"看得出是哪一路"的字符（`/`、`|`、`[` 一律换掉）。"""
    return "".join(ch if ch.isalnum() or ch in "-._" else "_"
                   for ch in str(raw_ref))[:_PREFIX_CHARS]


def raw_path(raw_ref: str) -> Path:
    """`raw_ref` → 本地文件路径（摘要命名，**不把 `raw_ref` 当文件名**）。

    ★ 为什么必须哈希：`raw_ref` 里有 `/`、`|`、`[`，直接当文件名会变成目录穿越
      或非法名；而只做字符替换又会让两条不同的 `raw_ref` 撞成同一个文件
      （互相覆盖 = 悄悄改了历史）。
    """
    digest = hashlib.sha256(str(raw_ref).encode("utf-8")).hexdigest()[:16]
    prefix = _slug(raw_ref) or "raw"
    return raw_dir() / f"{prefix}-{digest}.json"


def save_raw(raw_ref: str, data: Any, *, fetched_at: float = 0.0) -> Path:
    """把原始载荷**原子写**进本地副本（先写临时文件再替换）。

    ★ 原子写是必须的：`raw_read` 可能正好读到半份文件，那份 JSON 既解析不了、
      又会被当成"这份副本坏了" —— 而它其实只是**正在被写**。
    """
    path = raw_path(raw_ref)
    body = {
        "raw_ref": str(raw_ref),
        "fetched_at": float(fetched_at or 0.0),
        "data": data,
    }
    text = json.dumps(body, ensure_ascii=False, default=str)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    prune_raw()
    return path


def resolve_raw_ref(raw_ref: str) -> Optional[tuple[str, dict[str, Any]]]:
    """把**任意一种写法**的引用解析到落盘的那一份 —— 返回 `(落盘键, 内容)`。

    ★★ 为什么需要它（真机 + 离线都踩过）：同一个引用在不同环节会长得不一样 ——
      · `promql_read` 的结果里是 `promql: <query> | step=… | [start,end]`；
      · 证据里还会再挂上 `| series=… | metric=…`（**归属信息**，不是取数参数）；
      · 落盘用的是补全了"取数局限"后的键。
      于是"引用明明在、却报找不到"。这里按**由严到宽**三级解析：
        ① 逐字命中；② 补全局限后命中；③ 前缀唯一命中（去掉尾部归属段再比）。
      ③ 只在**唯一**时才算命中 —— 有歧义就返回 `None`（宁可报找不到，也不猜错一份数据）。
    """
    ref = str(raw_ref or "").strip()
    if not ref:
        return None
    doc = load_raw(ref)
    if isinstance(doc, dict):
        return ref, doc
    try:
        from .handlers import raw_ref_with_limits  # noqa: PLC0415 —— 用同一套补全口径

        canonical = raw_ref_with_limits(ref)
    except Exception:                    # noqa: BLE001 —— 补全失败就跳过这一级
        canonical = ref
    if canonical != ref:
        doc = load_raw(canonical)
        if isinstance(doc, dict):
            return canonical, doc
    core = ref.split(" | series=")[0].split(" | metric=")[0].strip()
    if not core:
        return None
    hits = [path for path in raw_dir().glob("*.json") if _stored_ref(path).startswith(core)]
    if len(hits) == 1:
        stored = _stored_ref(hits[0])
        doc = load_raw(stored)
        if isinstance(doc, dict):
            return stored, doc
    # ④ 退到**文件名前缀**（`_slug` 只留"看得出是哪一路"的字符；尾部归属段的写法
    #    差异（`| series=…` / 取数局限的措辞变化）不该让"引用明明在"变成找不到）。
    #    ★ 仍然要求唯一命中；多份就报找不到（宁可说找不到，也不猜错一份数据）。
    slug = _slug(core)[:60]
    if slug:
        named = sorted((path for path in raw_dir().glob(f"{slug}*.json")),
                       key=lambda path: path.stat().st_mtime, reverse=True)
        if len(named) == 1:
            stored = _stored_ref(named[0])
            doc = load_raw(stored)
            if isinstance(doc, dict):
                return stored, doc
    return None


def _stored_ref(path: Path) -> str:
    """读一份落盘副本里记的 `raw_ref`（读不到就给空串，调用方据此跳过）。"""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("raw_ref") or "")
    except Exception:                    # noqa: BLE001 —— 坏文件跳过
        return ""


def load_raw(raw_ref: str) -> Optional[dict[str, Any]]:
    """读回本地副本：`{"data": …, "fetched_at": …}`；**不存在就 `None`**（不猜、不重发）。

    ★ 与"读失败"分开：文件在但读不出来（权限/半份文件）会抛 `OSError`/`ValueError`，
      由调用方翻译成"读失败"；文件不在是 `None` —— 两者对调用方的含义完全不同。
    """
    path = raw_path(raw_ref)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    return {"data": payload.get("data"),
            "fetched_at": float(payload.get("fetched_at") or 0.0)}


def prune_raw(*, keep: int = DEFAULT_KEEP) -> int:
    """按 mtime 淘汰最旧的副本，返回删掉的条数（有界；失败静默）。"""
    directory = raw_dir()
    try:
        files = sorted((p for p in directory.glob("*.json")),
                       key=lambda item: item.stat().st_mtime)
    except OSError:
        return 0
    victims = files[:max(0, len(files) - max(1, int(keep)))]
    removed = 0
    for old in victims:
        try:
            old.unlink()
            removed += 1
        except OSError:
            continue
    return removed
