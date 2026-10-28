"""反问（澄清）体系。

**为什么单独立一个包**：反问会在**很多场景**发生（缺槽位、工具失败、指代歧义、
意图不明、下游不可用……），而且**任何时候都可能进入**。散在各节点里 = 各写各的问法、
各写各的回填，漏一个场景就静默猜一个答案 —— 与"AI 不能全权代办"直接冲突。

| 文件 | 职责 |
|---|---|
| `contract.py` | 契约：`ClarificationRequest` / `SlotPatch` / `AskContext` / 插件协议 |
| `registry.py` | 注册表 + **注册即校验**（重复 id / 空 applies_to / 无选项问句 → 加载期 fail-fast） |
| `plugins.py` | 内置插件（缺环境 / 缺服务 / 健康询问缺目标 / 意图不明 / 下游不可用） |
| `arbiter.py` | `ClarifyHub`：要不要问（代价/预算/去重/用户禁问）、问哪个（优先级）、答案怎么回填 |
"""

from __future__ import annotations

from .arbiter import CLARIFY_BUDGET, ENV_SENSITIVE_INTENTS, ClarifyHub
from .contract import (PATCHABLE_GAPS, AskContext, ClarificationPlugin,
                       ClarificationRequest, SlotPatch)
from .plugins import build_default_registry
from .registry import ClarifyRegistry, ClarifyRegistryError

__all__ = [
    "CLARIFY_BUDGET", "ENV_SENSITIVE_INTENTS", "ClarifyHub",
    "PATCHABLE_GAPS", "AskContext", "ClarificationPlugin", "ClarificationRequest",
    "SlotPatch", "build_default_registry", "ClarifyRegistry", "ClarifyRegistryError",
]
