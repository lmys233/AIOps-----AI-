"""`python -m agent.agents` —— 取证层的运维入口（跑一轮真取证 / 看取证计划）。

用法::

    .\\run.ps1 forensics                       # 对 order 跑一轮真取证（30m 窗口）
    .\\run.ps1 forensics --service payment --window 2h
    .\\run.ps1 forensics --service order --window 24h --json
    .\\run.ps1 forensics --plan-only            # 只看计划（不发任何工具调用）

★ 为什么要有这个入口：取证层的产物是"证据"，而证据**必须能被外部看到** ——
  否则"这一路没查成"和"这一路查了没异常"只能靠读日志分辨。
  黑板（内存版）在这一轮结束时打印成清单，人工核对。

★ 这里**不接** `BoardStore` 的 Redis 实现：那是编排层的会话状态。
  本入口只用来**跑一轮、看一轮**（不落任何长期状态）。
"""

from __future__ import annotations

from ..tools.executor import ToolExecutor

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Optional

from ..board.store import InMemoryBoardStore
from ..common import envfile
from ..common.contracts import EntityRef, Slots
from .runner import ForensicsRunner, resolve_window


def _env(env: dict[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _source(factory: Any, url: str) -> Any:
    """地址为空就**不装配**这个数据源（那一类信号会如实降级，而不是连到空地址上）。"""
    return factory(url) if url else None


def inject_topology(env: dict[str, str], *, enabled: bool = True) -> Any:
    """★ 实现已提到 `wiring.py`（服务壳也要用同一份装配，见那里的模块头）。

    这里保留同名函数是为了不破坏既有导入（`__main__` 是本任务的入口脚本）。
    """
    from .wiring import inject_topology as _inject

    return _inject(env, enabled=enabled)


def build_executor(env: dict[str, str], *, topology: bool = True) -> ToolExecutor:
    """★ 实现已提到 `wiring.py`（同一份装配被 CLI 与服务壳共用）。"""
    from .wiring import build_executor as _build

    return _build(env, topology=topology)


def build_slots(args: argparse.Namespace) -> Slots:
    """按**身份阶梯**决定这次观测的集群/环境/命名空间。

    ★ 阶梯（`common/identity.py`）：用户明说（`--cluster`）→ 被观测系统自报
      （`observed_info.yaml`）→ 环境变量（`AIOPS_*`，取值处是 `deploy/.env`）→ **空**。
      ★ 空是合法结果：契约允许 `EntityRef.id` 省略 `@cluster`，不发明占位值。
    """
    from ..common.identity import resolve as resolve_identity
    from ..discovery.probe import load_observed_info

    try:
        observed = load_observed_info().services
    except Exception as exc:  # noqa: BLE001 —— 没探测过就用不了这一级，不是错误
        print(f"  [i] 读不到 observed_info.yaml（{type(exc).__name__}）—— "
              f"身份阶梯跳过「自报」这一级", file=sys.stderr)
        observed = None

    conflicts: list[str] = []
    identity = resolve_identity(
        user={"cluster": args.cluster or None, "env": args.env or None},
        observed=observed, observed_conflicts=conflicts)
    for note in conflicts:
        print(f"  [!] {note}", file=sys.stderr)

    names = [name.strip() for name in (args.service or "order").split(",") if name.strip()]
    slots = Slots(env=identity.env or "prod", cluster=identity.cluster,
                  time_window=args.window, symptom=args.symptom or None)
    if names:
        slots.service_names = names
        slots.service = EntityRef.make("service", names[0], identity.cluster,
                                       identity.namespace)
    if args.change_id:
        slots.change_id = args.change_id
    print(f"  部署身份：{identity.render()}")
    return slots


async def cmd_run(args: argparse.Namespace, env: dict[str, str]) -> int:
    executor = build_executor(env, topology=not args.no_topology)
    board = InMemoryBoardStore()
    runner = ForensicsRunner(executor, board)
    slots = build_slots(args)
    window = resolve_window(slots)
    incident_id = args.incident or "inc-local"

    if args.plan_only:
        from .base import AgentContext

        ctx = AgentContext(incident_id=incident_id, slots=slots,
                           window_start=window[0], window_end=window[1])
        plan = runner.planner.plan(ctx, runner.agents)
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)
              if args.json else _render_plan(plan))
        return 0

    run = await runner.run(incident_id, slots, window=window)
    snapshot = await board.snapshot(incident_id)

    if args.json:
        print(json.dumps({"run": run.to_dict(), "board": snapshot},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    _render_plan(run.plan)
    print()
    for outcome in run.outcomes:
        print(f"-- {outcome.role}：证据 {len(outcome.evidence)} 条 / "
              f"查了没异常 {len(outcome.empty)} 路 / 没查成 {len(outcome.degraded)} 路")
        for item in outcome.evidence:
            print(f"   [证据] {item.signal:22s} {item.direction.value:5s} "
                  f"onset={item.onset:%H:%M:%S} 幅度={item.magnitude} "
                  f"{item.magnitude_unit or ''} 置信={item.confidence:.2f}")
            print(f"          {item.summary}")
            print(f"          回查: {item.raw_ref}")
        for signal in outcome.empty:
            print(f"   [查了没异常] {signal}（= 排除了一个假设，**不是**没有线索）")
        for line in outcome.degraded:
            print(f"   [没查成] {line}")
        for line in outcome.aux:
            print(f"   [辅助] {line}")

    print()
    print("=" * 72)
    print(f"  黑板 · incident={incident_id}")
    print(f"    证据         : {len(snapshot['evidence'])} 条")
    print(f"    待决缺口     : {snapshot['open_questions'] or '（无）'}")
    print(f"    QPS 估算     : {snapshot['qps_estimates'] or '（无）'}")
    print(f"    没查成的信号 : "
          f"{[item['signal'] for item in snapshot['degraded']] or '（无）'}")
    kinds: dict[str, int] = {}
    for entry in snapshot["timeline"]:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    print(f"    时间线       : {kinds}")
    print("=" * 72)
    if not snapshot["evidence"]:
        print("  注意：这一轮**一条证据都没有** —— 请对照上面的「查了没异常」与"
              "「没查成」两栏判断，是「确实正常」还是「根本没查成」。")
    return 0


def _render_plan(plan: Any) -> None:
    print("=" * 72)
    print("  取证计划（EvidencePlan）")
    print("=" * 72)
    print(f"  目标实体 : {plan.plan.targets or '（未指定）'}")
    print(f"  理由     : {plan.plan.rationale}")
    print(f"  取哪几路 : {plan.plan.signals}")
    for text in plan.plan.expected_exclusions:
        print(f"  可排除   : {text}")
    print(f"  可执行调用（{len(plan.calls)} 次）：")
    for call in plan.calls:
        print(f"    · {call.role:16s} {call.tool:14s} signal={call.signal:22s} "
              f"purpose={call.purpose}")
    if plan.gaps:
        print("  待决缺口（只能由用户提供）：")
        for gap in plan.gaps:
            print(f"    · {gap.field}: {gap.question}")
    if plan.degraded:
        print("  计划期判定跳过：")
        for signal, reason in plan.degraded:
            print(f"    · {signal}: {reason}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.agents",
        description="取证 Agent 层：跑一轮真取证 / 打印取证计划",
    )
    parser.add_argument("--service", default="order",
                        help="服务名，逗号分隔（默认 order）")
    parser.add_argument("--cluster", default="", help="集群名（留空则走身份阶梯：自报 → 环境 → 空）")
    parser.add_argument("--env", default="", help="环境（留空则走身份阶梯：自报 → 环境 → prod）")
    parser.add_argument("--window", default="30m", help="观测窗口（如 30m / 2h / 24h）")
    parser.add_argument("--symptom", default="", help="现象描述（可选，进计划）")
    parser.add_argument("--change-id", default="", help="已知的变更单号（可选）")
    parser.add_argument("--incident", default="", help="incident_id（默认 inc-local）")
    parser.add_argument("--plan-only", action="store_true",
                        help="只打印计划，**不发任何工具调用**")
    parser.add_argument("--no-topology", action="store_true",
                        help="不注入拓扑存储（用来看「没查成」那条路长什么样）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（给脚本用）")
    args = parser.parse_args(argv)

    envfile.load_env_file()
    env = dict(os.environ)
    if not _env(env, "AIOPS_PROMETHEUS_URL"):
        print("  [!] 未配置 AIOPS_PROMETHEUS_URL —— 指标这一路会如实降级"
              "（见 .env.example）", file=sys.stderr)
    from ..tools.semantics import parse_duration

    if parse_duration(args.window) is None:
        print(f"  [!] --window {args.window!r} 解析不了（应为 30m / 2h / 1d 这类写法）"
              f"—— 将退回默认 30m", file=sys.stderr)
    return asyncio.run(cmd_run(args, env))


if __name__ == "__main__":
    sys.exit(main())
