"""`python -m agent.topology` —— 拓扑层的运维入口（迁移 / 同步 / 体检）。

用法::

    .\\run.ps1 topology-migrate          # 建拓扑表（幂等）
    .\\run.ps1 topology-sync             # 从"来源"取一次拓扑事实并灌库
    .\\run.ps1 topology-check --show     # 体检 + 打印图（服务 / 依赖边 / 爆炸半径示例）

★ 为什么要有这个入口：拓扑这一路在取证时**只会表现为"有没有证据"**；
  如果"库里是空的"与"没查成"分不清，排查时只能猜。所以它必须能一条命令看出来。

★ 来源的选择**打印出来**（`self_report` 还是 `repo:app.yaml(dev)`）：
  拓扑事实的权威性取决于来源，使用者有权知道这份图是从哪来的。
"""

from __future__ import annotations

from typing import Optional

import argparse
import asyncio
import json
import os
import sys

from ..common import envfile
from .source import (
    SOURCE_REPO_CONFIG,
    SOURCE_SELF_REPORT,
    RepoConfigSource,
    TopologySource,
    pick_source,
)
from .store import SqlTopologyStore, TopologyUnavailable


def _env(env: dict[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def build_sources(env: dict[str, str], args: argparse.Namespace) -> list[TopologySource]:
    """按**权威性顺序**给出候选来源：自报优先，同仓配置兜底。"""
    sources: list[TopologySource] = []

    # ① 自报（`/health` 会带上身份与依赖；`/services` 汇总）
    try:
        from ..discovery.probe import load_observed_info  # noqa: PLC0415

        info = load_observed_info()
    except Exception as exc:  # noqa: BLE001 —— 没探测过就用不了，不是错误
        info = None
        print(f"  [i] 读不到 observed_info.yaml（{type(exc).__name__}）—— "
              f"自报来源不可用", file=sys.stderr)
    from .source import SelfReportSource

    sources.append(SelfReportSource(info=info, name=SOURCE_SELF_REPORT))

    # ② 同仓配置（开发期便利；来源会被如实标注）
    if not args.no_repo_config:
        from ..common.identity import resolve as resolve_identity

        # ★ 集群名走身份阶梯（自报 → 环境 → 空）：配置来源也**不发明**默认值
        observed = getattr(info, "services", None) if info is not None else None
        identity = resolve_identity(user={"cluster": args.cluster or None,
                                          "env": args.env or None},
                                    observed=observed)
        sources.append(RepoConfigSource(
            cluster=identity.cluster, env=identity.env or "prod",
            namespace=identity.namespace, name=SOURCE_REPO_CONFIG))
    return sources


async def cmd_migrate(env: dict[str, str]) -> int:
    dsn = _env(env, "AIOPS_DATABASE_URL")
    if not dsn:
        print("  [X] 缺 AIOPS_DATABASE_URL（见 .env.example）", file=sys.stderr)
        return 2
    store = SqlTopologyStore(dsn=dsn)
    try:
        statements = await store.migrate()
        ok, detail = await store.health()
    except TopologyUnavailable as exc:
        print(f"  [X] 迁移失败：{exc.message}", file=sys.stderr)
        return 2
    finally:
        await store.close()
    print(f"  [OK] 拓扑表已就绪（执行 {statements} 条语句，幂等）：{detail}")
    return 0


async def cmd_sync(env: dict[str, str], args: argparse.Namespace) -> int:
    dsn = _env(env, "AIOPS_DATABASE_URL")
    if not dsn:
        print("  [X] 缺 AIOPS_DATABASE_URL（见 .env.example）", file=sys.stderr)
        return 2

    sources = build_sources(env, args)
    source, why = pick_source(sources)
    print(f"  来源选择：{why}")
    if source is None:
        print("  [X] 没有可用的拓扑来源 —— 拓扑这一路会如实降级（不是「没有依赖关系」）",
              file=sys.stderr)
        return 1

    snapshot = await source.fetch()
    print(f"  取到：{len(snapshot.nodes)} 个服务 / {len(snapshot.links)} 条依赖边"
          f"（来源 {snapshot.source}）")
    if snapshot.note:
        print(f"  说明：{snapshot.note}")

    store = SqlTopologyStore(dsn=dsn)
    try:
        counts = await store.apply(snapshot)
        ok, detail = await store.health()
    except TopologyUnavailable as exc:
        print(f"  [X] 同步失败（先跑 topology-migrate？）：{exc.message}", file=sys.stderr)
        return 2
    finally:
        await store.close()
    print(f"  [OK] 已灌库：{counts['services']} 个节点 / {counts['edges']} 条边 · {detail}")
    if args.json:
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
    return 0


async def cmd_check(env: dict[str, str], args: argparse.Namespace) -> int:
    dsn = _env(env, "AIOPS_DATABASE_URL")
    if not dsn:
        print("  [X] 缺 AIOPS_DATABASE_URL（见 .env.example）", file=sys.stderr)
        return 2
    store = SqlTopologyStore(dsn=dsn)
    try:
        ok, detail = await store.health()
        print(f"  可用性：{'✅' if ok else '❌'} {detail}")
        if not ok:
            print("  → 先跑 `topology-migrate` 建表")
            return 1
        graph = await store.snapshot()
        print(f"  服务 {len(graph['services'])} 个 · 依赖边 {len(graph['edges'])} 条")
        if args.show:
            print("  ── 服务 ──")
            for row in sorted(graph["services"], key=lambda item: str(item["name"])):
                print(f"    {row['name']:12s} {row['entity_id']:28s} "
                      f"cluster={row.get('cluster') or '（未提供）':14s} "
                      f"来源={row.get('identity_source')}")
            print("  ── 依赖边 ──")
            for row in sorted(graph["edges"], key=lambda item: str(item["from"])):
                print(f"    {row['from']} → {row['to']}  ({row.get('criticality')})")

        # 爆炸半径示例：给每个服务算一遍（QPS 从入参来 —— 这里刻意传空）
        print("  ── 爆炸半径（QPS 未提供，故 qps_affected=0）──")
        for row in sorted(graph["services"], key=lambda item: str(item["name"])):
            radius = await store.blast_radius(str(row["entity_id"]))
            upstream = radius.get("upstream") or []
            print(f"    {row['name']:12s} 受影响上游 {radius['services_affected']} 个 "
                  f"{upstream if upstream else ''}")
        print("  ── 依赖解析（三种写法必须指向同一实体）──")
        # ★ 集群名走身份阶梯（不写死）：阶梯给不出集群时，第三种写法退化成 `svc:gateway`
        from ..common.identity import resolve as resolve_identity

        probe_cluster = resolve_identity(user={"cluster": args.cluster or None}).cluster
        probes = ["gateway", "svc:gateway"]
        if probe_cluster:
            probes.append(f"svc:gateway@{probe_cluster}")
        for key in probes:
            deps = await store.get_dependencies(key, direction="downstream", depth=1)
            print(f"    {key:32s} → {[f'{d.from_service}->{d.to_service}' for d in deps]}")
    except TopologyUnavailable as exc:
        print(f"  [X] {exc.message}", file=sys.stderr)
        return 2
    finally:
        await store.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.topology",
        description="拓扑层：建表 / 同步服务与依赖 / 体检")
    parser.add_argument("--migrate", action="store_true", help="建表（幂等）")
    parser.add_argument("--sync", action="store_true", help="取一次拓扑事实并灌库")
    parser.add_argument("--check", action="store_true", help="体检（默认动作）")
    parser.add_argument("--show", action="store_true", help="体检时打印整张图")
    parser.add_argument("--json", action="store_true", help="同步时以 JSON 打印快照")
    parser.add_argument("--cluster", default="", help="集群名（**留空即「未提供」**）")
    parser.add_argument("--env-name", dest="env", default="prod", help="环境（prod/staging/dev）")
    parser.add_argument("--no-repo-config", action="store_true",
                        help="不用同仓 app.yaml 作为来源（只看自报）")
    args = parser.parse_args(argv)

    envfile.load_env_file()
    env = dict(os.environ)
    if args.migrate:
        return asyncio.run(cmd_migrate(env))
    if args.sync:
        return asyncio.run(cmd_sync(env, args))
    return asyncio.run(cmd_check(env, args))

if __name__ == "__main__":
    sys.exit(main())
