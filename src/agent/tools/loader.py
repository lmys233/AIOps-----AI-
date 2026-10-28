"""工具**加载器** —— 把"动态注册"变成一条**标准化、可审计、可重载**的流程。

--------------------------------------------------------------------------------
★ 设计立场：加载**不是**扫描，而是**对账**
--------------------------------------------------------------------------------

参考 dsh 的插件加载（`dsh plugin` 按**已安装状态**对账 profile 的层列表，
包必须在自己的清单里声明 `dsh.bundle.patch` 才被认作 bundle）：

| 我们的做法 | 为什么 |
|---|---|
| 只加载 `extensions.yaml` 里**显式列出**的模块 | 目录扫描 = "谁能写文件谁就能给自己加工具" = **提权** |
| 模块里用 `@define_tool` 声明，加载器只负责"收走并注册" | 声明与注册分离：**模块不能自我授权**（`granted_roles` 只是申请） |
| 每个模块加载失败**不影响**已注册的工具 | 一个坏插件不该让整个工具层不可用（降级继续，而不是整体失败） |
| 注册/注销/重载都留痕（`RegistryChange`） | "谁在什么时候加了什么工具"必须可审计 |
| `reload()` = 注销旧定义 → 重新导入 → 注册新定义 | 升级一个工具不需要重启进程；`revision` 递增，老审计仍可解释 |

--------------------------------------------------------------------------------
★ manifest 的形态（`src/agent/tools/extensions.yaml`）
--------------------------------------------------------------------------------

```yaml
version: 1
extensions:
  - module: agent.tools.extensions.latency_probe   # 显式 Python 导入路径
    enabled: true                                  # 默认策略：写 false 才加载
    actor: human:yc                                # 谁引入的（进变更留痕）
    note: 读 99 分位延迟的只读探针
```

★ `enabled: false` 的条目**只是不加载**（不是"加载后禁用"）——
  "没启用的代码不跑"比"跑起来再拦"少一个失败面。
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Optional

import yaml

from .definition import definitions_of
from .registry import RegistrationError, ToolRegistry

__all__ = [
    "ManifestError",
    "ManifestEntry",
    "LoadOutcome",
    "ToolLoader",
    "MANIFEST_FILE",
    "ENV_MANIFEST",
    "load_from_manifest",
]

logger = logging.getLogger("agent.tools.loader")

#: 默认 manifest 位置（与本模块同目录，随代码走）
MANIFEST_FILE: Final[Path] = Path(__file__).with_name("extensions.yaml")
#: 环境变量覆盖（便于部署时指向另一份清单）
ENV_MANIFEST: Final[str] = "AIOPS_TOOL_MANIFEST"

_SUPPORTED_VERSION: Final[int] = 1


class ManifestError(ValueError):
    """manifest 自身不合法 —— fail-fast（它是**授权入口**，写错了不能猜着读）。"""


@dataclass(frozen=True)
class ManifestEntry:
    module: str
    enabled: bool = False
    actor: str = "system"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"module": self.module, "enabled": self.enabled,
                "actor": self.actor, "note": self.note}


@dataclass(frozen=True)
class LoadOutcome:
    """一个模块的加载结果（成功/跳过/失败都如实记下来）。"""

    module: str
    loaded: tuple[str, ...] = ()
    skipped: bool = False
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"module": self.module, "loaded": list(self.loaded),
                "skipped": self.skipped, "errors": list(self.errors)}


def parse_manifest(text: str, *, source: str = "<string>") -> list[ManifestEntry]:
    """解析 manifest。**任何含糊都不接受**（未知字段、缺 module、enabled 非布尔）。"""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{source} 不是合法 YAML：{exc}") from exc
    if doc is None:
        return []
    if not isinstance(doc, dict):
        raise ManifestError(f"{source} 顶层必须是映射")
    if doc.get("version") != _SUPPORTED_VERSION:
        raise ManifestError(
            f"{source} 的 version 必须是 {_SUPPORTED_VERSION}，实际 {doc.get('version')!r}"
        )
    raw = doc.get("extensions") or []
    if not isinstance(raw, list):
        raise ManifestError(f"{source} 的 extensions 必须是列表")

    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{source} extensions 第 {index + 1} 项"
        if not isinstance(item, dict):
            raise ManifestError(f"{where} 必须是映射")
        unknown = set(item) - {"module", "enabled", "actor", "note"}
        if unknown:
            raise ManifestError(
                f"{where} 含未支持字段 {sorted(unknown)} —— 不许静默忽略"
                f"（这类字段常被误以为「已经生效了」）"
            )
        module = str(item.get("module") or "").strip()
        if not module:
            raise ManifestError(f"{where} 缺 module（必须是 Python 导入路径）")
        if module in seen:
            raise ManifestError(f"{where} 的 module {module!r} 重复了")
        seen.add(module)
        enabled = item.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ManifestError(f"{where} 的 enabled 必须是布尔（实际 {enabled!r}）")
        entries.append(ManifestEntry(
            module=module, enabled=enabled,
            actor=str(item.get("actor") or "system"),
            note=str(item.get("note") or ""),
        ))
    return entries


@dataclass
class ToolLoader:
    """按 manifest 把扩展工具装进注册表。"""

    registry: ToolRegistry
    manifest_path: Optional[Path] = None
    _loaded_modules: list[str] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------
    def resolve_manifest(self) -> Optional[Path]:
        """manifest 路径：显式参数 > 环境变量 > 默认文件（不存在 → `None` = 只用内置）。"""
        if self.manifest_path is not None:
            return Path(self.manifest_path) if Path(self.manifest_path).exists() else None
        from_env = (os.environ.get(ENV_MANIFEST) or "").strip()
        if from_env:
            path = Path(from_env)
            if not path.exists():
                raise ManifestError(f"{ENV_MANIFEST}={from_env} 指向的文件不存在")
            return path
        return MANIFEST_FILE if MANIFEST_FILE.exists() else None

    # ------------------------------------------------------------------
    def load(self, *, actor: str = "system") -> tuple[LoadOutcome, ...]:
        """加载 manifest 里**启用**的模块。返回逐模块结果（失败不抛，如实记录）。

        ★ 审计署名的优先顺序：**manifest 里声明的 `entry.actor` 优先**，
          没有才退回调用方给的 `actor`。理由：`entry.actor` 是"**这个工具是谁加进来的**"
          的权威来源（写在清单里、可被 review），而 `actor` 默认值是 `"system"`
          （真值）—— 以前写成 `actor or entry.actor` 时它把清单里的署名**永远短路掉**，
          于是每条注册审计都记成了 `system`，**记错了人**。
        """
        path = self.resolve_manifest()
        if path is None:
            logger.info("没有工具扩展清单（%s 不存在）→ 只用内置工具", MANIFEST_FILE.name)
            return ()
        entries = parse_manifest(path.read_text(encoding="utf-8-sig"), source=str(path))
        outcomes: list[LoadOutcome] = []
        for entry in entries:
            if not entry.enabled:
                outcomes.append(LoadOutcome(module=entry.module, skipped=True))
                continue
            outcomes.append(self._load_module(entry, manifest=path,
                                              actor=entry.actor or actor))
        return tuple(outcomes)

    def reload(self, *, actor: str = "system") -> tuple[LoadOutcome, ...]:
        """重载：**先注销旧定义，再重新导入并注册**（`revision` 递增，变更留痕）。"""
        for module_name in list(self._loaded_modules):
            victims = self.registry.unregister_module(module_name, actor=actor, reason="reload")
            if victims:
                logger.info("重载前注销 %s 的旧定义：%s", module_name, list(victims))
            importlib.invalidate_caches()
            module = importlib.import_module(module_name)
            # ★ 模块级 `TOOL_DEFINITIONS` 是"导入时声明"的，重载会重新执行装饰器；
            #   但旧列表还在同一模块对象上累积 → 先清空，避免重复定义
            if hasattr(module, "TOOL_DEFINITIONS"):
                delattr(module, "TOOL_DEFINITIONS")
            importlib.reload(module)
        self._loaded_modules = []
        return self.load(actor=actor)

    # ------------------------------------------------------------------
    def _load_module(self, entry: ManifestEntry, *, manifest: Path,
                     actor: str) -> LoadOutcome:
        try:
            module = importlib.import_module(entry.module)
        except Exception as exc:  # noqa: BLE001 —— 坏插件不许拖垮工具层
            logger.exception("加载扩展模块失败：%s", entry.module)
            return LoadOutcome(module=entry.module,
                               errors=(f"导入失败（{type(exc).__name__}: {exc}）",))
        try:
            definitions = definitions_of(module)
        except Exception as exc:  # noqa: BLE001
            return LoadOutcome(module=entry.module,
                               errors=(f"声明不合法（{type(exc).__name__}: {exc}）",))

        if not definitions:
            return LoadOutcome(
                module=entry.module,
                errors=(f"模块 {entry.module} 里没有任何 `@define_tool` 声明"
                        f"（清单列了它，说明本意是要装工具）",),
            )

        loaded: list[str] = []
        errors: list[str] = []
        for definition in definitions:
            stamped = definition.with_metadata(
                source=f"manifest:{manifest.name}", origin_module=entry.module)
            try:
                decision = self.registry.register(
                    stamped, actor=actor, reason=entry.note or f"来自 {manifest.name}")
            except RegistrationError as exc:
                errors.append(str(exc))
                continue
            loaded.append(definition.name)
            if decision.revoked:
                logger.warning("扩展 %s 的以下申请被能力策略压掉：%s",
                               definition.name, list(decision.revoked))
        if loaded:
            self._loaded_modules.append(entry.module)
        return LoadOutcome(module=entry.module, loaded=tuple(loaded), errors=tuple(errors))


def load_from_manifest(registry: ToolRegistry, path: Optional[Path] = None, *,
                       actor: str = "system") -> tuple[LoadOutcome, ...]:
    """一行式入口（`build_default_registry(load_extensions=True)` 用它）。"""
    return ToolLoader(registry, manifest_path=path).load(actor=actor)
