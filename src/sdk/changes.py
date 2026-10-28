"""变更留痕：把"本服务生效的业务参数变了"写进 `change_events`。

★ 这条信号为什么必须存在：

    `change_events` 表 → 变更归因 → **缺失后果：无法解释"为什么突然变了"，只剩现象描述**

  遥测（指标/日志/链路）只会告诉你"**14:03 变差了**"，
  它**永远不会**告诉你"**14:01 有人把连接池上限从 20 改成了 3**" ——
  参数值、谁改的、改成什么、怎么回滚，这些**根本不产生遥测**。
  所以必须有这条**独立于遥测**的信号。Change Agent 查的就是它。

★ 记录什么：**本服务真正用到的业务参数**（由 `env_bridge` 声明）

  | 记 | 不记 |
  |---|---|
  | `db.pool.size`（池上限） | `aiops.*`（SDK 自己的接入清单） |
  | `db.pool.timeout_sec` | 别的服务的业务参数 |
  | `services.order.downstream.*`（下游地址） | 与归因无关的噪声 |

  ★ 判据是 `env_bridge` —— 它是"本服务真的要用这些参数"的**声明**，不是猜的。
    于是 diff 它就等于 diff"本服务实际生效的运行参数"。

  ★ 为什么**不记 `aiops` 段**：那是**采集器的配置**，改了它业务行为一点不变。
    把它记进"变更归因"的数据源，会让 Change Agent 去怀疑一个毫无影响的东西 ——
    **制造假相关，比漏记更坏**。

★ `diff` 的结构（五个键一个都不能少）：

      {field[], old_value[], new_value[], action_hash, scenario_id}

  三个数组**按下标对齐**：`field[i]` 的旧值/新值分别是 `old_value[i]`/`new_value[i]`。
  这正好是"逐键 diff"的形态 —— 一个字段变了就只记一个字段，
  而不是把整段配置塞进去（那样读的人还得自己再 diff 一遍）。

★ 全量快照存哪：**SDK 本地状态文件**（`<state_dir>/<service>.json`），不进 `change_events`。

  理由：`change_events` 里应当**只有真正的变更**。若把"首次基线"也写进去，
  Change Agent 会把"SDK 接入"误当成"业务变更"从而错误相关。
  代价是状态文件丢了就丢基线 —— 此时**告警一次**并重建（绝不静默）。

★ 写库用**受限角色 `aiops_runner`**：它对这张表只有 `SELECT` + `INSERT`，
  **被显式 REVOKE 掉 UPDATE/DELETE** —— 连业务自己都无法篡改已写下的变更历史。
  这是"写操作靠外部约束，不靠 prompt"那条红线在数据层的落点。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Optional

from .contract import CHANGE_DIFF_KEYS, CHANGE_TABLE, entity_id
from .state import Runtime

__all__ = ["record_startup_change", "build_diff", "config_fingerprint",
           "resolve_dsn", "CHANGE_TYPE"]

logger = logging.getLogger("sdk.changes")

# 变更类型：配置类
CHANGE_TYPE: Final = "configmap"

# ★ 变更来源（可用取值：gitops / manual / agent / scenario_script）
#
#   以前写的是 `scenario_script`，那是**重规划前**的措辞 ——
#   当时故障注入靠 `observed.scenario` 脚本，而那个脚本已随"正常态业务系统"
#   一起删除。现在写记录的是 SDK 自己、触发原因是"人改了项目配置"，
#   所以取 `manual` 才是**如实**的。可用 `changes.source` 覆盖（流水线下发时改 gitops）。
DEFAULT_SOURCE: Final = "manual"

# 连接超时：SDK 的变更留痕**绝不能拖住业务启动**
_CONNECT_TIMEOUT_SEC = 5.0

# 关闭连接的超时：留痕已经写完了，收尾不能把后台线程挂住
_CLOSE_TIMEOUT_SEC = 2.0

# `change_id` 里随机后缀的长度：够避免同日同服务重名即可
_CHANGE_ID_RANDOM_CHARS = 12

# 值里出现 `scheme://user:pass@host` 时把口令打码（DSN 类）
_USERINFO = re.compile(r"(://[^:/@]+):([^@]+)@")


def _redact(cfg: Any, field: str, value: Any) -> Any:
    """按需打码 —— **凭据绝不进审计表**。

    ★ 本项目的项目配置里本来就不放口令（DSN 从环境变量读），
      但这条防线必须留着：一旦有人往配置里写了 `api_key`，
      它就会原样进 `change_events` —— 那等于把凭据写进审计表，且很难撤回。
    """
    patterns = tuple(getattr(cfg, "changes_redact", ()) or ())
    lowered = field.lower()
    if any(p in lowered for p in patterns):
        return "***"
    if isinstance(value, str) and "://" in value and "@" in value:
        return _USERINFO.sub(r"\1:***@", value)
    return value


def config_fingerprint(keys: dict[str, Any]) -> str:
    """配置指纹：规范化 JSON 后的 sha256 前 16 位（用于日志/自检里一眼对比）。"""
    blob = json.dumps(keys, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_diff(*, fields: list[str], old_values: list[Any], new_values: list[Any],
               scenario_id: str, service: str) -> dict[str, Any]:
    """构造 `change_events.diff`（五个键，三个数组按下标对齐）。"""
    if not (len(fields) == len(old_values) == len(new_values)):
        raise ValueError("field / old_value / new_value 三个数组必须等长且按下标对齐")

    payload = {
        "entity": service,
        "field": fields,
        "old_value": old_values,
        "new_value": new_values,
        "scenario_id": scenario_id,
    }
    action_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()

    diff = {
        "field": fields,
        "old_value": old_values,
        "new_value": new_values,
        "action_hash": action_hash,
        "scenario_id": scenario_id,
    }
    missing = [k for k in CHANGE_DIFF_KEYS if k not in diff]
    if missing:  # pragma: no cover —— 最后的防线
        raise AssertionError(f"diff 缺少契约约定的键：{missing}")
    return diff


def resolve_dsn() -> Optional[str]:
    """写库用的 DSN：环境变量 `RUNNER_DATABASE_URL`。

    ★ 凭据**不进项目配置文件**（配置文件要进仓库），所以这里没有"配置文件优先"
      这一路 —— 唯一来源就是环境变量；没有就返回 None（调用方告警一次）。
    """
    return (os.environ.get("RUNNER_DATABASE_URL") or "").strip() or None


def _state_path(cfg: Any) -> Path:
    state_dir = Path(getattr(cfg, "changes_state_dir", ".run/sdk-state"))
    if getattr(cfg,'observed_name',''):
        import hashlib
        state_dir=state_dir/('system_'+hashlib.sha256((cfg.observed_name+'|'+str(cfg.env)).encode()).hexdigest()[:24])
    return state_dir / f"{cfg.service_name}.json"


def _read_state(cfg: Any) -> Optional[dict[str, Any]]:
    path = _state_path(cfg)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("business") if isinstance(data, dict) else None


def _write_state(cfg: Any, business: dict[str, Any]) -> None:
    path = _state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"service": cfg.service_name, "business": business,
                    "written_at": datetime.now(timezone.utc).isoformat()},
                   ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )


async def _insert_change(conn: Any, service: str, diff: dict[str, Any],
                         *, source: str, cluster: Optional[str] = None) -> str:
    """写一条变更记录。

    ★★ `entity_id` 必须是**规范实体 id**（格式同 `EntityRef.id`，
      即 `svc:order@compose-cn-1`；集群未知时省略 `@cluster`）。

      以前这里写的是**裸服务名**（`order`）—— 后果不是报错，而是
      Agent 侧按服务名生成的规范化 id 永远匹配不上这一行，
      于是"变更归因"这一路**恒为空**，被读成"窗口内没有变更"
      （**假阴性**：排除了一个真实的假设）。
    """
    change_id = f"chg_{service}_{uuid.uuid4().hex[:_CHANGE_ID_RANDOM_CHARS]}"
    await conn.execute(
        f"""
        INSERT INTO {CHANGE_TABLE}
            (change_id, changed_at, entity_id, entity_type, change_type,
             operator, source, diff, git_commit, rollback_ref)
        VALUES ($1, $2, $3, 'service', $4, $5, $6, $7::jsonb, NULL, $8)
        """,
        change_id,
        datetime.now(timezone.utc),
        entity_id("service", service, cluster),
        CHANGE_TYPE,
        "aiops-sdk",
        source,
        json.dumps(diff, ensure_ascii=False),
        "恢复项目配置里被改动的那几个键，然后重启该服务",
    )
    return change_id


async def _record_async(runtime: Runtime, cfg: Any) -> dict[str, Any]:
    import asyncpg  # noqa: PLC0415

    current = dict(cfg.watched_business_keys())
    previous = _read_state(cfg)

    if previous is None:
        # ★ 首次运行：没有"上一次"可比 → **这不是变更**，所以不写 change_events。
        #   只落一份快照，供下次比对。（把基线写进变更表会让 Change Agent
        #   把"SDK 接入"误当成"业务变更"，那是假的证据。）
        _write_state(cfg, current)
        return {"recorded": False, "kind": "baseline",
                "fingerprint": config_fingerprint(current)}

    fields = sorted(k for k in set(current) | set(previous)
                    if current.get(k) != previous.get(k))
    if not fields:
        return {"recorded": False, "kind": "unchanged",
                "fingerprint": config_fingerprint(current)}

    if getattr(cfg,'observed_name',''):
        diff=build_diff(fields=fields,old_values=[_redact(cfg,f,previous.get(f)) for f in fields],
            new_values=[_redact(cfg,f,current.get(f)) for f in fields],scenario_id='boot',service=runtime.service_name)
        row=runtime.diagnostics.emit('change_event',dict(diff,change_type='configuration'),'sdk_runtime')
        if row:_write_state(cfg,current)
        return {'recorded':False,'queued':bool(row),'kind':'scoped_queued','fields':fields,'fingerprint':config_fingerprint(current)}
    dsn = resolve_dsn()
    if not dsn:
        runtime.warn_once(
            "changes_dsn_missing",
            "检测到配置变更，但环境变量 RUNNER_DATABASE_URL 没有设置 —— "
            "变更无法落库（缺了它就无法解释「为什么突然变了」）",
        )
        return {"recorded": False, "kind": "changed_but_no_dsn", "fields": fields}

    diff = build_diff(
        fields=fields,
        old_values=[_redact(cfg, f, previous.get(f)) for f in fields],
        new_values=[_redact(cfg, f, current.get(f)) for f in fields],
        scenario_id=str(getattr(cfg, "changes_scenario_id", "") or "") or "boot",
        service=runtime.service_name,
    )
    source = str(getattr(cfg, "changes_source", DEFAULT_SOURCE) or DEFAULT_SOURCE)

    conn = await asyncpg.connect(dsn, timeout=_CONNECT_TIMEOUT_SEC)
    try:
        change_id = await _insert_change(
            conn, runtime.service_name, diff, source=source,
            cluster=getattr(cfg, "cluster", None))
    finally:
        try:
            await conn.close(timeout=_CLOSE_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 —— 关闭失败不影响已写入的记录
            runtime.warn_once(
                "change_conn_close_failed",
                f"关闭变更留痕连接失败（{type(exc).__name__}: {exc}）—— 记录已写入，可忽略",
            )

    _write_state(cfg, current)
    for f, old, new in zip(diff["field"], diff["old_value"], diff["new_value"]):
        logger.warning("配置变更已留痕：%s: %s -> %s（change_id=%s）", f, old, new, change_id)
    return {"recorded": True, "kind": "changed", "change_id": change_id,
            "fields": fields, "fingerprint": config_fingerprint(current)}


def record_startup_change(runtime: Runtime, cfg: Any, *, background: bool = True) -> None:
    """比对"本次启动生效的业务参数"与上次快照，有差异就写一条变更记录。

    ★ 为什么放后台线程：留痕是**旁路**的事 —— 数据库不可达时 `asyncpg.connect`
      要等到超时（5s）。让业务启动为此等 5 秒不可接受；而"等不到就跳过"
      又违反"绝不静默"。所以：后台跑 + 失败告警一次。
    """
    def _runner() -> None:
        try:
            result = asyncio.run(_record_async(runtime, cfg))
            runtime.set_counter("change_last_kind", 1 if result.get("recorded") else 0)
            runtime.note_patch(
                "changes",
                f"{result.get('kind')}"
                + (f" fields={result.get('fields')}" if result.get("fields") else ""),
            )
        except Exception as exc:  # noqa: BLE001 —— 留痕失败绝不能影响业务
            runtime.warn_once(
                "change_record_failed",
                f"变更留痕失败（{type(exc).__name__}: {exc}）—— "
                f"业务不受影响，但「为什么突然变了」这一路证据会缺失",
            )
            if _state_path(cfg).exists():
                # 快照没能更新：下次重启会拿旧快照再 diff 一遍 —— 不会漏，
                # 但会重复报同一次变更，这里说清楚免得看着奇怪。
                runtime.warn_once(
                    "change_state_stale",
                    "配置快照未更新（本次写入失败）—— 下次启动会再报一次同样的变更，"
                    "直到成功写入为止",
                )

    if background:
        threading.Thread(target=_runner, name="aiops-sdk-changes", daemon=True).start()
    else:
        _runner()
