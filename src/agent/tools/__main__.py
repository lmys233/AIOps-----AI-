"""`python -m agent.tools` —— 查看**当前工具注册表**（谁在册、谁能调、要谁批）。

★ 为什么要有这个入口：注册表是**动态**的（内置 + manifest 扩展），
  "现在到底有哪些工具、某个角色能调哪些"必须能**被外部观察到** ——
  否则排查"模型为什么没用某个工具"只能靠猜（本项目的一贯口径：别猜，去看）。

用法::

    .\\run.ps1 tools                       # 全部在册工具 + 各角色可见集合
    .\\run.ps1 tools --role metrics_agent  # 只看某个角色的候选（模型看到的）
    .\\run.ps1 tools --json                # 机器可读（给脚本/CI 用）
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..common.enums import AGENT_ROLES
from .policy import ROLE_CAPABILITIES
from .registry import build_default_registry


def _collect(registry: Any, roles: list[str]) -> dict[str, Any]:
    return {
        "allow_write": registry.policy.allow_write,
        "revision": registry.revision,
        "snapshot_hash": registry.snapshot_hash(),
        "tools": [registry.get(name).to_dict() for name in registry.names()],
        "visibility": {
            role: [d.name for d in registry.visible_to(role)]
            for role in roles
        },
        "changes": [c.to_dict() for c in registry.changes],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.tools",
        description="查看工具注册表：在册工具、能力、风险等级、各角色可见集合",
    )
    parser.add_argument("--role", action="append", default=[],
                       help="只显示指定角色的可见集合（可重复）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--changes", action="store_true", help="同时打印注册表变更留痕")
    args = parser.parse_args(argv)

    registry = build_default_registry()
    roles = list(args.role) or list(AGENT_ROLES)
    payload = _collect(registry, roles)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("  工具注册表（内置 = 9 个契约工具；扩展来自 extensions.yaml）")
    print("=" * 78)
    print(f"  写能力开关 : {'**已启用**' if payload['allow_write'] else '未启用（写工具不可调用）'}")
    print(f"  注册表版本 : revision={payload['revision']}  snapshot={payload['snapshot_hash']}")
    print()
    print(f"  {'工具':<18}{'能力':<34}{'风险':<6}{'版本':<6}{'幂等':<6}来源")
    print("  " + "-" * 74)
    for tool in payload["tools"]:
        caps = ",".join(tool["capabilities"])
        print(f"  {tool['name']:<18}{caps:<34}{tool['risk_level']:<6}"
              f"v{tool['version']:<5}{str(tool['idempotent']):<6}{tool['source']}")
    print()
    print("  ── 各角色可见（= 模型在 function calling 里能看到的）──")
    for role in roles:
        caps = ",".join(sorted(ROLE_CAPABILITIES.get(role, frozenset()))) or "（无）"
        names = payload["visibility"][role]
        print(f"  {role:<20} 能力[{caps:<32}] → {names or '（空）'}")
    if args.changes:
        print()
        print("  ── 变更留痕 ──")
        for change in payload["changes"]:
            print(f"  {change['action']:<11}{change['tool']:<18}v{change['version']} "
                  f"rev={change['revision']:<4}actor={change['actor']:<16}"
                  f"effective={change['effective_roles']} revoked={change['revoked']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
