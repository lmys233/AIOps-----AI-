"""本地会话表：**AIOps 目录下的一个 SQLite 单文件**（默认 `.run/sessions.db`）。

★ 它回答的是"我聊过哪些会话、上次是哪一个" —— 于是前端刷新能接着上次继续，
而不是把整个对话列表清空。

★★ **这是本地小表（条数有上限、单机访问），允许同步访问：微秒级、无网络往返。
往里放慢操作（远程调用、大文件读写、批量重算）就是把服务壳的响应时间押在磁盘上。**

它**不依赖 Redis/Postgres**：会话的列出/切换/删除只读写这个文件；
对话正文仍在会话记忆里（Redis 或进程内），记忆读不到时如实说"记忆不可读"，
而不是把会话一并弄丢。轮次台账只记"这一轮属于哪次事故、什么状态"，**不复制正文**。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Optional

from ..common.snowflake import new_id

__all__ = ["DEFAULT_DB_PATH", "ENV_DB", "MAX_SESSIONS", "CONTEXT_INDEX_MAX", "TURNS_META_MAX",
           "MESSAGE_MAX", "SESSION_ID_PREFIX", "ALERT_SESSION_PREFIX", "SessionRecord",
           "SessionStore", "open_session_store", "note_turn"]

logger = logging.getLogger("agent.sessions")

#: 库文件位置：环境变量优先（换了位置只改一个地方），默认落在项目目录下的 `.run/`。
ENV_DB: Final[str] = "AIOPS_SESSIONS_DB"
DEFAULT_DB_PATH: Final[str] = ".run/sessions.db"

#: 会话 id 的前缀（服务端铸 id；前端只保存指针，不再自己拼 id）。
SESSION_ID_PREFIX: Final[str] = "sess"

#: **告警触发**的排查用的会话前缀：那是排查记录，不是"我聊过什么"。
#: ★ 它们**不进用户会话列表**（否则告警一多就把用户的会话淹掉），但可以按 id 直接取；
#:   被藏起来的条数由 `hidden_alerts()` 如实报出 —— 过滤也不许静默。
ALERT_SESSION_PREFIX: Final[str] = "alert_"

#: 会话条数上限：超了就淘汰（先淘汰空会话，再淘汰最久未用的）。
MAX_SESSIONS: Final[int] = 200

#: 每轮台账最多留几条（与恢复时召回的近层轮数同一个量级）。
TURNS_META_MAX: Final[int] = 20

#: `last_message` 的截断长度（列表页只要一行摘要，不存整句）。
MESSAGE_MAX: Final[int] = 200

#: 每轮**状态摘要**的截断长度（恢复时每轮显示一句，不存整段答案）。
TURN_SUMMARY_MAX: Final[int] = 120

#: 一个会话最多留几条**远层索引**（超上限丢最老的）—— 与"近层召回条数"同一量级。
CONTEXT_INDEX_MAX: Final[int] = 500

_TABLE: Final[str] = "sessions"
_CONTEXT_INDEX: Final[str] = "context_index"
_CONTEXT_SUMMARY: Final[str] = "context_summary"

_SCHEMA: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    session_id   TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL DEFAULT 0,
    last_turn_at REAL NOT NULL DEFAULT 0,
    turn_count   INTEGER NOT NULL DEFAULT 0,
    last_message TEXT NOT NULL DEFAULT '',
    incident_ids TEXT NOT NULL DEFAULT '[]',
    turn_meta    TEXT NOT NULL DEFAULT '[]',
    is_last      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_last
    ON {_TABLE}(is_last) WHERE is_last = 1;
CREATE TABLE IF NOT EXISTS {_CONTEXT_INDEX} (
    session_id  TEXT NOT NULL,
    turn        INTEGER NOT NULL,
    at          REAL NOT NULL DEFAULT 0,
    preview     TEXT NOT NULL DEFAULT '',
    incident_id TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, turn)
);
CREATE INDEX IF NOT EXISTS context_index_by_time
    ON {_CONTEXT_INDEX}(session_id, at);
CREATE TABLE IF NOT EXISTS {_CONTEXT_SUMMARY} (
    session_id TEXT PRIMARY KEY,
    summary    TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class SessionRecord:
    """一条会话记录（本地表的一行；`incident_ids` 是**索引**，事故本身另存留痕）。"""

    session_id: str
    title: str
    created_at: float
    updated_at: float
    last_turn_at: float
    turn_count: int
    last_message: str
    incident_ids: tuple[str, ...]
    is_last: bool

    def to_dict(self) -> dict[str, Any]:
        """转成响应形状（元组在 JSON 里是数组，这里显式转成列表）。"""
        return {"session_id": self.session_id, "title": self.title,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "last_turn_at": self.last_turn_at, "turn_count": self.turn_count,
                "last_message": self.last_message,
                "incident_ids": list(self.incident_ids), "is_last": self.is_last}


class SessionStore:
    """会话表（增 / 删 / 改 / 查 / 选「上次会话」），线程安全、库文件按需创建。"""

    def __init__(self, path: str | Path | None = None) -> None:
        # ★ 只算路径、**不碰磁盘**：没用到会话表的进程（单测里的几十个服务壳）
        #   不该因为构造它而建目录、开文件。
        self.path: Final[Path] = _resolve_path(path)
        self._lock: Final[threading.Lock] = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._evicted: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    def list(self, limit: int = 50) -> tuple[SessionRecord, ...]:
        """会话列表：**最近活跃的在前**；空会话与**告警触发的排查**不列出。

        · 空会话（一轮都没聊过）不列 —— 列表别被空行塞满；
        · `alert_` 开头的会话不列 —— 那是告警排查，不是"我聊过什么"。
        ★ "没列出"不等于"不存在"：`get()` / `last()` 对它们**仍然可用**，
          被藏起来的告警条数由 `hidden_alerts()` 报出（过滤不许静默）。
        """
        with self._lock:
            rows = self._connection().execute(
                f"SELECT * FROM {_TABLE} WHERE turn_count > 0 "
                f"AND substr(session_id, 1, ?) <> ? "
                f"ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (len(ALERT_SESSION_PREFIX), ALERT_SESSION_PREFIX,
                 max(1, int(limit)))).fetchall()
        return tuple(_record(row) for row in rows)

    def hidden_alerts(self) -> int:
        """被列表藏起来的、**聊过内容**的告警会话条数（过滤的留痕，必须报得出来）。"""
        with self._lock:
            row = self._connection().execute(
                f"SELECT COUNT(*) AS total FROM {_TABLE} WHERE turn_count > 0 "
                f"AND substr(session_id, 1, ?) = ?",
                (len(ALERT_SESSION_PREFIX), ALERT_SESSION_PREFIX)).fetchone()
        return int(row["total"] or 0) if row is not None else 0

    def get(self, session_id: str) -> Optional[SessionRecord]:
        """按 id 取一条（没有 → `None`，不编一条空记录出来）。"""
        with self._lock:
            row = _fetch(self._connection(), str(session_id or ""))
        return _record(row) if row is not None else None

    def create(self, *, title: str = "", session_id: str = "") -> SessionRecord:
        """新建会话：**id 由服务端铸**（雪花）；给了 id 就用给的（重入不新建、不覆盖标题）。"""
        sid = str(session_id or "").strip() or new_id(SESSION_ID_PREFIX)
        now = _now()
        with self._lock:
            conn = self._connection()
            if _fetch(conn, sid) is None:
                conn.execute(
                    f"INSERT INTO {_TABLE} (session_id, title, created_at, updated_at) "
                    f"VALUES (?, ?, ?, ?)", (sid, str(title or "").strip(), now, now))
                conn.commit()
            self._evicted = self._enforce_bound(conn, keep=sid)
            row = _fetch(conn, sid)
        return _record(_require(row, sid))

    def touch(self, session_id: str, *, message: str, incident_id: str = "",
              status: str = "", summary: str = "") -> SessionRecord:
        """记一轮：已有则更新（轮次 +1、最近原话、事故去重追加、活跃时间刷新），没有就顺手建。

        `status` 是这一轮的落定状态（澄清/拒答/有结论……），`summary` 是这一轮的**状态摘要**
        （回答的第一句，有界）—— 与事故号一起进轮次台账，恢复时前端据此显示"这一轮聊到哪了"。
        ★ 台账**不存正文**，正文只在会话记忆里。
        """
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("touch 需要一个非空 session_id（会话 id 由调用方给）")
        text = str(message or "")[:MESSAGE_MAX]
        now = _now()
        with self._lock:
            conn = self._connection()
            row = _fetch(conn, sid)
            if row is None:
                conn.execute(
                    f"INSERT INTO {_TABLE} (session_id, created_at, updated_at, last_turn_at, "
                    f"turn_count, last_message, incident_ids, turn_meta) "
                    f"VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                    (sid, now, now, now, text, _dump(_append_incident((), incident_id)),
                     _dump(_append_meta([], status, incident_id, summary, now))))
            else:
                conn.execute(
                    f"UPDATE {_TABLE} SET updated_at = ?, last_turn_at = ?, turn_count = ?, "
                    f"last_message = ?, incident_ids = ?, turn_meta = ? WHERE session_id = ?",
                    (now, now, int(row["turn_count"] or 0) + 1, text,
                     _dump(_append_incident(_json_tuple(row["incident_ids"]), incident_id)),
                     _dump(_append_meta(_json_meta(row["turn_meta"]), status, incident_id,
                                        summary, now)), sid))
            conn.commit()
            self._evicted = self._enforce_bound(conn, keep=sid)
            row = _fetch(conn, sid)
        return _record(_require(row, sid))

    def rename(self, session_id: str, title: str) -> Optional[SessionRecord]:
        """重命名：**只改本地表的展示名**（不动记忆、不动事故，也不改活跃时间）。"""
        clean = str(title or "").strip()
        if not clean:
            raise ValueError("标题不能为空（空标题不覆盖原有名字）")
        sid = str(session_id or "")
        with self._lock:
            conn = self._connection()
            if _fetch(conn, sid) is None:
                return None
            conn.execute(f"UPDATE {_TABLE} SET title = ? WHERE session_id = ?", (clean, sid))
            conn.commit()
            row = _fetch(conn, sid)
        return _record(row) if row is not None else None

    def delete(self, session_id: str) -> bool:
        """删除会话（**幂等**：不存在返回 `False`）；事故留痕不在这里，不会被连带删除。

        ★ 会话的**远层索引**与**中层摘要**跟着一起清：它们只是这个会话的
          "笔记"，会话没了却留着，下一次同 id 的会话就会读到**别人**的旧索引。
        """
        sid = str(session_id or "")
        with self._lock:
            conn = self._connection()
            if _fetch(conn, sid) is None:
                return False
            conn.execute(f"DELETE FROM {_TABLE} WHERE session_id = ?", (sid,))
            conn.execute(f"DELETE FROM {_CONTEXT_INDEX} WHERE session_id = ?", (sid,))
            conn.execute(f"DELETE FROM {_CONTEXT_SUMMARY} WHERE session_id = ?", (sid,))
            conn.commit()
        return True

    def select(self, session_id: str) -> Optional[SessionRecord]:
        """把该会话设为「上次会话」（**全表只允许一条** —— 由唯一索引兜住）。"""
        sid = str(session_id or "")
        with self._lock:
            conn = self._connection()
            if _fetch(conn, sid) is None:
                return None
            conn.execute(f"UPDATE {_TABLE} SET is_last = 0 WHERE is_last = 1")
            conn.execute(f"UPDATE {_TABLE} SET is_last = 1 WHERE session_id = ?", (sid,))
            conn.commit()
            row = _fetch(conn, sid)
        return _record(row) if row is not None else None

    def last(self) -> Optional[SessionRecord]:
        """「上次会话」：显式选过就用它；没选过退化为**最近活跃**的那条（刷新即可恢复）。

        ★ 退化那条**必须排除告警触发的排查**（`alert_` 前缀）：用户点刷新，想要的是
          "我刚才聊到哪了"，而不是"最近有一条告警排查" —— 告警会话是列表里都不列出的东西
          （见 `list()`），把它当成"上次会话"恢复出来，会表现得像**用户自己的对话不见了**。
          被排除的条数由 `hidden_alerts()` 如实报出（过滤不许静默）。
        """
        with self._lock:
            conn = self._connection()
            row = conn.execute(f"SELECT * FROM {_TABLE} WHERE is_last = 1 LIMIT 1").fetchone()
            if row is None:
                row = conn.execute(
                    f"SELECT * FROM {_TABLE} "
                    f"WHERE substr(session_id, 1, ?) <> ? "
                    f"ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                    (len(ALERT_SESSION_PREFIX), ALERT_SESSION_PREFIX)).fetchone()
        return _record(row) if row is not None else None

    def turn_meta(self, session_id: str,
                  limit: int = TURNS_META_MAX) -> tuple[dict[str, Any], ...]:
        """该会话的轮次台账（状态 + 事故号 + 状态摘要 + 时间），**最近的 `limit` 条、按时间正序**。

        ★ 恢复时正文来自会话记忆，这里补"这一轮属于哪次事故、什么状态"；
          正文不在记忆里时，它还是那几行**唯一能显示的东西**（见 `api/sessions.py`）。
        """
        with self._lock:
            row = _fetch(self._connection(), str(session_id or ""))
        if row is None:
            return ()
        return tuple(_json_meta(row["turn_meta"])[-max(1, int(limit)):])

    def last_evicted(self) -> tuple[str, ...]:
        """最近一次写入触发的淘汰（空元组 = 没有淘汰）—— 有界性必须**看得见**。"""
        return self._evicted

    # ------------------------------------------------------------------
    #  远层索引与中层摘要（**加性**：只加表与读写，不动既有列与口径）
    # ------------------------------------------------------------------
    def index_put(self, session_id: str, entries: Any) -> int:
        """写入该会话的**远层索引**条目，返回真正落库的条数；超上限丢最老的。

        ★ 主键是 `(session_id, turn)`：同一轮重复写入是**覆盖**（幂等），
          而不是攒出第二条 —— 索引重复会让分页翻出两次同一轮。
        """
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("index_put 需要一个非空 session_id")
        rows = [(_sid(sid), _turn_of(item), _at_of(item), _text_of(item, "preview"),
                 _text_of(item, "incident_id"), _text_of(item, "status"))
                for item in (entries or ()) if item is not None]
        if not rows:
            return 0
        with self._lock:
            conn = self._connection()
            conn.executemany(
                f"INSERT INTO {_CONTEXT_INDEX} "
                f"(session_id, turn, at, preview, incident_id, status) "
                f"VALUES (?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT(session_id, turn) DO UPDATE SET "
                f"at = excluded.at, preview = excluded.preview, "
                f"incident_id = excluded.incident_id, status = excluded.status", rows)
            conn.commit()
            self._enforce_index_bound(conn, sid)
        return len(rows)

    def index_page(self, session_id: str, *, offset: int = 0,
                   limit: int = 20) -> tuple[dict[str, Any], ...]:
        """按**轮次号**升序分页读索引（稳定：同样的库、同样的页）。"""
        return self._index_query(
            f"SELECT * FROM {_CONTEXT_INDEX} WHERE session_id = ? "
            f"ORDER BY turn ASC LIMIT ? OFFSET ?",
            (str(session_id or ""), max(0, int(limit or 0)), max(0, int(offset or 0))))

    def index_search(self, session_id: str, *, start: float = 0.0, end: float = 0.0,
                     limit: int = 20) -> tuple[dict[str, Any], ...]:
        """按**时间区间**查索引（`end<=0` 表示不设上界）。"""
        upper = float(end) if float(end or 0) > 0 else float("inf")
        return self._index_query(
            f"SELECT * FROM {_CONTEXT_INDEX} WHERE session_id = ? AND at >= ? AND at <= ? "
            f"ORDER BY at ASC, turn ASC LIMIT ?",
            (str(session_id or ""), float(start or 0), upper, max(0, int(limit or 0))))

    def summary_put(self, session_id: str, text: str) -> None:
        """写入该会话的中层摘要（**替换**，不追加第二份）。"""
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("summary_put 需要一个非空 session_id")
        with self._lock:
            conn = self._connection()
            conn.execute(
                f"INSERT INTO {_CONTEXT_SUMMARY} (session_id, summary, updated_at) "
                f"VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                f"summary = excluded.summary, updated_at = excluded.updated_at",
                (sid, str(text or ""), _now()))
            conn.commit()

    def summary_get(self, session_id: str) -> str:
        """读该会话的中层摘要（没有就是空串）。"""
        with self._lock:
            row = self._connection().execute(
                f"SELECT summary FROM {_CONTEXT_SUMMARY} WHERE session_id = ?",
                (str(session_id or ""),)).fetchone()
        return str(row["summary"] or "") if row is not None else ""

    def context_counts(self) -> tuple[int, int]:
        """`(索引条数, 摘要条数)` —— 有界性与"删会话是否清干净"都要看得见。"""
        with self._lock:
            conn = self._connection()
            one = conn.execute(f"SELECT COUNT(*) AS total FROM {_CONTEXT_INDEX}").fetchone()
            two = conn.execute(f"SELECT COUNT(*) AS total FROM {_CONTEXT_SUMMARY}").fetchone()
        return (int(one["total"] or 0) if one is not None else 0,
                int(two["total"] or 0) if two is not None else 0)

    def _index_query(self, sql: str, params: tuple[Any, ...]) -> tuple[dict[str, Any], ...]:
        """跑一次索引查询并把行转成响应形状（列名与 `IndexEntry` 字段一一对应）。"""
        with self._lock:
            rows = self._connection().execute(sql, params).fetchall()
        return tuple({"session_id": str(row["session_id"]), "turn": int(row["turn"] or 0),
                      "at": float(row["at"] or 0.0), "preview": str(row["preview"] or ""),
                      "incident_id": str(row["incident_id"] or ""),
                      "status": str(row["status"] or "")} for row in rows)

    def _enforce_index_bound(self, conn: sqlite3.Connection, session_id: str) -> int:
        """有界：该会话索引超过上限 → 丢掉**最老的**（索引页按轮次号升序，故留最新的那些）。

        Returns: 这次丢掉了几条（0 = 没超上限）。
        """
        before = conn.execute(f"SELECT COUNT(*) AS total FROM {_CONTEXT_INDEX} "
                              f"WHERE session_id = ?", (session_id,)).fetchone()
        conn.execute(
            f"DELETE FROM {_CONTEXT_INDEX} WHERE session_id = ? AND turn NOT IN ("
            f"SELECT turn FROM {_CONTEXT_INDEX} WHERE session_id = ? "
            f"ORDER BY turn DESC LIMIT ?)",
            (session_id, session_id, max(0, CONTEXT_INDEX_MAX)))
        conn.commit()
        after = conn.execute(f"SELECT COUNT(*) AS total FROM {_CONTEXT_INDEX} "
                             f"WHERE session_id = ?", (session_id,)).fetchone()
        dropped = (int(before["total"] or 0) if before is not None else 0) \
            - (int(after["total"] or 0) if after is not None else 0)
        if dropped:
            logger.info("会话 %s 的远层索引超过上限 %d 条 → 丢掉最老的 %d 条",
                        session_id, CONTEXT_INDEX_MAX, dropped)
        return dropped

    # ------------------------------------------------------------------
    def close(self):
        if self._conn is not None:
            self._conn.close();self._conn=None

    def _connection(self) -> sqlite3.Connection:
        """取连接（**首次访问才建库**）；连接跨线程共享，读写由本类那把锁串行化。"""
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def _enforce_bound(self, conn: sqlite3.Connection,
                       *, keep: str = "") -> tuple[str, ...]:
        """有界：先淘汰**空会话**（一轮都没聊过），再按最久未用淘汰；「上次会话」与**刚写入的那条**不动。

        ★★ 为什么必须护住"刚写入的那条"：新建出来的会话**正是空会话**，而空会话排在淘汰序列的
          最前面 —— 表满时（有界机制真正生效的那一刻）`create()` 会把刚 INSERT 的行立刻删掉，
          随后 `_require()` 读不到它 → **新建会话恒 500**（真机实测：表里已有 200 条时必现）。
          "有界"要淘汰的是**别人的旧会话**，不是用户此刻正在创建的这一条。
        """
        rows = conn.execute(
            f"SELECT session_id, updated_at FROM {_TABLE} "
            f"ORDER BY (turn_count > 0) ASC, updated_at ASC").fetchall()
        overflow = len(rows) - MAX_SESSIONS
        if overflow <= 0:
            return ()
        protected = {str(item) for item in
                     (self._last_session_id(conn), str(keep or "")) if str(item)}
        victims = [str(row["session_id"]) for row in rows
                   if str(row["session_id"]) not in protected][:overflow]
        conn.executemany(f"DELETE FROM {_TABLE} WHERE session_id = ?",
                         [(item,) for item in victims])
        # ★ 被淘汰的会话的索引/摘要一并清掉（它们是该会话的笔记，不是独立事实）
        conn.executemany(f"DELETE FROM {_CONTEXT_INDEX} WHERE session_id = ?",
                         [(item,) for item in victims])
        conn.executemany(f"DELETE FROM {_CONTEXT_SUMMARY} WHERE session_id = ?",
                         [(item,) for item in victims])
        conn.commit()
        if victims:
            logger.info("会话表超过上限 %d 条 → 淘汰最久未用的 %d 条：%s",
                        MAX_SESSIONS, len(victims), victims)
        return tuple(victims)

    @staticmethod
    def _last_session_id(conn: sqlite3.Connection) -> str:
        """当前被标成「上次会话」的那条（没有就是空串）——淘汰时它不动。"""
        row = conn.execute(f"SELECT session_id FROM {_TABLE} WHERE is_last = 1").fetchone()
        return str(row["session_id"]) if row is not None else ""


def open_session_store(path: str | Path | None = None) -> SessionStore:
    """建一个会话表（显式路径优先 → `AIOPS_SESSIONS_DB` → `.run/sessions.db`）。"""
    return SessionStore(path)


def note_turn(store: SessionStore, session_id: str, *, message: str, status: str = "",
              incident_id: str = "", summary: str = "") -> None:
    """把一轮记进会话表；**失败只记日志** —— 台账是旁路，坏掉不该带走这一轮排查。"""
    try:
        store.touch(session_id, message=message, status=status, incident_id=incident_id,
                    summary=summary)
    except Exception as exc:  # noqa: BLE001 —— 会话表写不进去不是"这次排查失败"
        logger.warning("会话台账写入失败（%s）：%s", type(exc).__name__, exc)


# ======================================================================
#  行 ←→ 记录（取值只在这几个函数里，别处不再各写一份）
# ======================================================================
def _resolve_path(path: str | Path | None) -> Path:
    """库文件位置：显式路径优先 → 环境变量 → 项目目录下的默认位置。"""
    chosen = str(path).strip() if path is not None else ""
    if not chosen:
        chosen = str(os.environ.get(ENV_DB) or "").strip() or DEFAULT_DB_PATH
    return Path(chosen)


def _now() -> float:
    return time.time()


def _fetch(conn: sqlite3.Connection, session_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM {_TABLE} WHERE session_id = ?",
                        (str(session_id or ""),)).fetchone()


def _require(row: Optional[sqlite3.Row], session_id: str) -> sqlite3.Row:
    if row is None:                       # 理论上不会发生：刚写完就该读得到
        raise RuntimeError(f"会话 {session_id!r} 写入后读不到（本地会话表可能被外部改动）")
    return row


def _record(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        session_id=str(row["session_id"]), title=str(row["title"] or ""),
        created_at=float(row["created_at"] or 0.0), updated_at=float(row["updated_at"] or 0.0),
        last_turn_at=float(row["last_turn_at"] or 0.0),
        turn_count=int(row["turn_count"] or 0), last_message=str(row["last_message"] or ""),
        incident_ids=_json_tuple(row["incident_ids"]), is_last=bool(row["is_last"]))


def _append_incident(ids: tuple[str, ...], incident_id: str) -> tuple[str, ...]:
    """事故号**去重追加**（一轮排查一个事故；同一个事故不重复记）。"""
    value = str(incident_id or "").strip()
    if not value or value in ids:
        return ids
    return ids + (value,)


def _sid(session_id: str) -> str:
    return str(session_id or "")


def _turn_of(entry: Any) -> int:
    """索引条目的轮次号（读不出来 → 0，不猜）。"""
    try:
        return int(getattr(entry, "turn", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _text_of(entry: Any, name: str) -> str:
    """索引条目的一个文本字段（读不到 → 空串，不猜）。"""
    return str(getattr(entry, name, "") or "")


def _at_of(entry: Any) -> float:
    """索引条目的时间 → epoch 秒（ISO 字符串与数字都认；读不出来 → 0，不猜）。"""
    value = getattr(entry, "at", "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _append_meta(meta: Any, status: str, incident_id: str,
                 summary: str = "", at: float = 0.0) -> list[dict[str, Any]]:
    """轮次台账追加一条（状态 + 事故号 + **状态摘要** + 时间，不复制正文）。

    ★ 记时间是为了"正文不在记忆里"那条退路：台账行也得让用户判断新旧。
    """
    entry: dict[str, Any] = {"status": str(status or ""),
                             "incident_id": str(incident_id or ""),
                             "summary": str(summary or "")[:TURN_SUMMARY_MAX],
                             "at": float(at or 0.0)}
    return (list(meta) + [entry])[-TURNS_META_MAX:]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_tuple(raw: Any) -> tuple[str, ...]:
    """把一列 JSON 数组读成字符串元组（坏数据 → 空元组 + 一条警告，不让整表读不出来）。"""
    parsed = _load(raw, [])
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if str(item or "").strip())


def _json_meta(raw: Any) -> list[dict[str, Any]]:
    """把轮次台账读成 `[{status, incident_id, summary, at}]`（坏数据跳过，不猜）。"""
    parsed = _load(raw, [])
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            stamp = float(item.get("at") or 0.0)
        except (TypeError, ValueError):
            stamp = 0.0
        out.append({"status": str(item.get("status") or ""),
                    "incident_id": str(item.get("incident_id") or ""),
                    "summary": str(item.get("summary") or ""),
                    "at": stamp})
    return out


def _load(raw: Any, default: Any) -> Any:
    try:
        return json.loads(str(raw or ""))
    except (TypeError, ValueError) as exc:
        logger.warning("会话表里有一列不是合法 JSON（已按空值处理）：%s", type(exc).__name__)
        return default
