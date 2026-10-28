"""本地会话管理：会话表（本地 SQLite 单文件）+ 轮次台账。

★ 分工：
· 会话表（本包）= "我聊过哪些会话、上次是哪一个、聊到第几轮、出过哪些事故"；
· 会话记忆（`memory`）= 每轮**正文**（Redis 或进程内，按会话级 TTL 分层）。
两者**不互相依赖**：Redis 不可用时照样能列出/切换/删除会话，只是恢复不出正文 ——
那时要如实说"记忆不可读"，而不是把会话也一起弄丢。
"""

from .store import (ALERT_SESSION_PREFIX, DEFAULT_DB_PATH, ENV_DB, MAX_SESSIONS, MESSAGE_MAX,
                    SESSION_ID_PREFIX, TURNS_META_MAX, SessionRecord, SessionStore, note_turn,
                    open_session_store)

__all__ = [
    "ALERT_SESSION_PREFIX", "DEFAULT_DB_PATH", "ENV_DB", "MAX_SESSIONS", "MESSAGE_MAX",
    "SESSION_ID_PREFIX", "TURNS_META_MAX", "SessionRecord", "SessionStore", "note_turn",
    "open_session_store",
]
