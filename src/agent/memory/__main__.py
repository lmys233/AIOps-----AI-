"""`python -m agent.memory` —— 记忆层的运维入口（迁移 / 体检 / 往返自检）。

用法::

    .\\run.ps1 memory-migrate            # 建表（幂等）：pgvector + pg_trgm + 两张记忆表
    .\\run.ps1 memory-check              # 体检：四层各自能不能读写（真 Redis + 真 Postgres）
    .\\run.ps1 memory-check --demo       # 体检后写一条演示记忆并召回，打印注入文本

★ 为什么要有这个入口：记忆层引入了**两样新基础设施依赖**（Redis 与 Postgres 的记忆表），
  "连不上/没建表"必须能**一条命令**看出来 —— 而不是等到某次排查里
  `recall()` 静默降级（那时你只会觉得"这次没召回出东西"）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..common import envfile
from ..common.contracts import EntityRef
from ..common import identity
from ..common.enums import VerificationMethod
from .embedding import LexicalHashEmbedder
from .service import MemoryService, PgDatabase
from .stores import (
    EpisodicRecord,
    ProcedureRecord,
    RedisWorkingMemory,
    SqlEpisodicStore,
    SqlProcedureStore,
    ConversationTurn,
)


def _dsn(env: dict[str, str]) -> str:
    return (env.get("AIOPS_DATABASE_URL") or "").strip()


def _redis_url(env: dict[str, str]) -> str:
    return (env.get("AIOPS_REDIS_URL") or "").strip()


async def _connect(env: dict[str, str]) -> tuple[MemoryService, Any, Any]:
    """建"真"记忆服务：Postgres（记忆表）+ Redis（工作记忆）。"""
    import redis.asyncio as aioredis  # noqa: PLC0415

    db = PgDatabase(dsn=_dsn(env))
    client = aioredis.from_url(_redis_url(env), decode_responses=False)
    service = MemoryService(
        episodic=SqlEpisodicStore(db),
        working=RedisWorkingMemory(client),
        procedural=SqlProcedureStore(db),
        embedder=LexicalHashEmbedder(),
    )
    return service, db, client


async def cmd_migrate(env: dict[str, str]) -> int:
    service = MemoryService()
    db = PgDatabase(dsn=_dsn(env))
    try:
        result = await service.migrate(db)
    except Exception as exc:  # noqa: BLE001
        print(f"  [X] 迁移失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        await db.close()
    print(f"  [OK] 记忆层迁移完成：{result.statements} 条语句，扩展 {list(result.extensions)}")
    print("       表：agent_memory_episodic / agent_memory_procedures")
    print("       工作记忆在 Redis（会话级 TTL 24h），语义记忆走 TopologyStore（不另建表）")
    return 0


async def cmd_check(env: dict[str, str], *, demo: bool) -> int:
    service, db, client = await _connect(env)
    failures: list[str] = []
    token = uuid.uuid4().hex[:8]

    # ---- ① 工作记忆（Redis）----
    try:
        await service.working.append_turn("__check__", ConversationTurn(
            turn=1, user_message="体检", at=datetime.now(timezone.utc).isoformat()))
        turns = await service.working.turns("__check__")
        redis_ok = any(turn.user_message == "体检" for turn in turns)
        await service.working.clear("__check__")
    except Exception as exc:  # noqa: BLE001
        redis_ok = False
        failures.append(f"工作记忆（Redis）：{type(exc).__name__}: {exc}")
    print(f"  [{'OK' if redis_ok else 'X '}] 工作记忆（Redis，TTL {24}h）：读写往返")

    # ---- ② 情景记忆（Postgres + pgvector）----
    episodic_ok = False
    memory_id: Optional[str] = None
    try:
        # ★ 实体 id 用**规范前缀 + 身份阶梯**（不手拼、不发明集群名）：
        #   体检数据也必须符合契约，否则演示出来的形状是假的。
        demo_cluster = identity.resolve().cluster
        memory_id = f"mem_check_{token}"
        record = EpisodicRecord(
            memory_id=memory_id,
            symptom_text=f"[体检] order 服务 P99 延迟涨到 2.3s（{token}）",
            root_cause="连接池上限被改小，获取连接超时",
            verification=VerificationMethod.VERIFIED_METRICS,
            confidence=0.82, category="config_change",
            entity_id=EntityRef.make("service", "order", demo_cluster).id,
            cluster=demo_cluster,
            embedding=service.embedder.embed("order 延迟 P99 连接池"),
        )
        await service.episodic.insert(record)
        found = await service.episodic.get(memory_id)
        episodic_ok = found is not None and found.root_cause == record.root_cause
    except Exception as exc:  # noqa: BLE001
        failures.append(f"情景记忆（Postgres）：{type(exc).__name__}: {exc}")
    print(f"  [{'OK' if episodic_ok else 'X '}] 情景记忆（Postgres + pgvector）：写入后读回")
    if memory_id and not demo:
        try:
            await service.episodic.delete(memory_id)
        except Exception:  # noqa: BLE001
            pass

    # ---- ③ 程序性记忆 ----
    proc_ok = False
    try:
        proc_id = f"proc_check_{token}"
        await service.procedural.upsert(ProcedureRecord(
            procedure_id=proc_id, name="连接池耗尽排查",
            symptom_text="连接等待数升高、延迟上升",
            steps=({"order": 1, "action": "查 db_pool_waiting_connections"},
                   {"order": 2, "action": "查 db_pool_size 配置变更"}),
            embedding=service.embedder.embed("连接池 等待 延迟"),
        ))
        hits = await service.procedural.search("连接等待 延迟升高", top_k=1)
        proc_ok = bool(hits) and hits[0][0].procedure_id == proc_id
        await db.execute("DELETE FROM agent_memory_procedures WHERE procedure_id = $1", proc_id)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"程序性记忆（Postgres）：{type(exc).__name__}: {exc}")
    print(f"  [{'OK' if proc_ok else 'X '}] 程序性记忆（SOP，按成功率自我进化）：写入后召回")

    # ---- ④ 语义记忆（拓扑）----
    print(f"  [--] 语义记忆：走 TopologyStore（尚未接入实现 → 召回时如实标记该路缺失）")

    # ---- 五道闸门 + 召回（演示）----
    if demo and episodic_ok:
        result = await service.recall_detailed(
            symptom="order 延迟涨了，连接池是不是有问题", top_k=3)
        print(f"\n  ── 召回（三路并行 + RRF + 多维加权）──")
        print(f"     降级路：{list(result.degraded_paths) or '（无）'}")
        print(f"     RRF：{ {k: round(v, 4) for k, v in result.rrf.items()} }")
        print(f"     加权分：{ {k: round(v, 4) for k, v in result.scores.items()} }")
        print("  ── 注入文本（带元数据）──")
        for line in result.injection.splitlines() or ["（空）"]:
            print(f"     {line}")

        gate_result = await service.write({
            "symptom_text": f"[体检] order 延迟升高（{token}）",
            "root_cause": "连接池上限由 20 调成 5",
            "verification": VerificationMethod.VERIFIED_METRICS.value,
            "confidence": 0.88, "category": "config_change",
            "entity_id": EntityRef.make("service", "order", demo_cluster).id,
        })
        print(f"\n  ── 写入（五道闸门）──\n     skipped={gate_result.skipped} "
              f"memory_id={gate_result.memory_id}\n     reason={gate_result.reason}")
        if gate_result.memory_id:
            await service.episodic.delete(gate_result.memory_id)

    if memory_id:
        try:
            await service.episodic.delete(memory_id)
        except Exception:  # noqa: BLE001
            pass
    await db.close()
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001
        pass

    print()
    if failures:
        for item in failures:
            print(f"  [!] {item}")
        print("  结论: 记忆层**未就绪**（上面的层不可用；注意「没查成」≠「没命中」）")
        return 1
    print("  结论: 记忆层就绪（工作记忆 / 情景 / 程序性三层可读写）")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.memory",
        description="Agent 记忆层：迁移、体检、往返自检",
    )
    parser.add_argument("--migrate", action="store_true", help="建表（幂等）")
    parser.add_argument("--check", action="store_true", help="体检四层读写（默认动作）")
    parser.add_argument("--demo", action="store_true", help="体检后演示：写入 + 召回 + 注入文本")
    args = parser.parse_args(argv)

    envfile.load_env_file()
    env = dict(os.environ)
    if not _dsn(env):
        print("  [X] 缺 AIOPS_DATABASE_URL（见 .env.example）", file=sys.stderr)
        return 2

    if args.migrate:
        return asyncio.run(cmd_migrate(env))
    return asyncio.run(cmd_check(env, demo=args.demo))


if __name__ == "__main__":
    sys.exit(main())
