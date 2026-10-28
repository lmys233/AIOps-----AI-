"""反问插件注册表：**显式清单**才加载，注册即校验（与工具层同一口径）。"""

from __future__ import annotations

from .contract import PATCHABLE_GAPS, AskContext, ClarificationPlugin, ClarificationRequest

__all__ = ["ClarifyRegistryError", "ClarifyRegistry"]


class ClarifyRegistryError(RuntimeError):
    """插件清单不合法 —— **加载期 fail-fast**（别等用户提问时才发现问不出来）。"""


class ClarifyRegistry:
    """插件表：`register` 只登记，`validate` 做三件事。"""

    def __init__(self) -> None:
        self._plugins: dict[str, ClarificationPlugin] = {}

    # ------------------------------------------------------------------
    def register(self, plugin: ClarificationPlugin) -> None:
        plugin_id = str(getattr(plugin, "id", "") or "").strip()
        if not plugin_id:
            raise ClarifyRegistryError("插件必须有非空 id")
        if plugin_id in self._plugins:
            raise ClarifyRegistryError(
                f"插件 id 重复：{plugin_id!r}（同一场景两个实现 = 两套口径）")
        gaps = tuple(str(gap) for gap in (getattr(plugin, "applies_to", ()) or ()))
        if not gaps:
            raise ClarifyRegistryError(
                f"插件 {plugin_id!r} 的 applies_to 为空 —— 它永远不会被触发")
        unknown = [gap for gap in gaps if gap not in PATCHABLE_GAPS]
        if unknown:
            raise ClarifyRegistryError(
                f"插件 {plugin_id!r} 声明了白名单外的 gap：{unknown}")
        self._plugins[plugin_id] = plugin

    # ------------------------------------------------------------------
    def all(self) -> tuple[ClarificationPlugin, ...]:
        return tuple(sorted(self._plugins.values(),
                            key=lambda item: (-int(getattr(item, "priority", 0)),
                                              str(getattr(item, "id", "")))))

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))

    def for_gap(self, gap: str) -> tuple[ClarificationPlugin, ...]:
        return tuple(plugin for plugin in self.all()
                     if gap in tuple(getattr(plugin, "applies_to", ()) or ()))

    def by_id(self, plugin_id: str) -> ClarificationPlugin | None:
        return self._plugins.get(str(plugin_id))

    # ------------------------------------------------------------------
    def validate(self, *, ctx: AskContext | None = None) -> tuple[str, ...]:
        """加载期自检：**三件事必须成立**。

        | # | 判据 | 为什么 |
        |---|---|---|
        | ① | id 不重复、非空 | 重复 id = 两套口径，`plugin_id` 回填会认错 |
        | ② | `applies_to` 非空且在白名单内 | 空声明 = 这个插件永远不会被触发 |
        | ③ | `question()` **返回带选项的问句**、`apply()` 返回 `SlotPatch` | 没选项的问句客户端渲染不出来（"信息不足"式废话） |

        Returns 校验通过的插件 id（顺序按优先级）。有任何一条不成立就抛错。
        """
        sample_ctx = ctx or AskContext(intent="INCIDENT_RCA",
                                       unresolved=("env", "service"),
                                       service_options=("order",),
                                       env_options=("prod",))
        for plugin in self.all():
            plugin_id = str(plugin.id)
            for gap in tuple(plugin.applies_to):
                request = ClarificationRequest(
                    gap=str(gap), why=f"注册期自检（{plugin_id}）",
                    fallback="自检用；真实调用必须给出兜底")
                try:
                    question = plugin.question(request, sample_ctx)
                except Exception as exc:  # noqa: BLE001 —— 出题就炸的插件不许上线
                    raise ClarifyRegistryError(
                        f"插件 {plugin_id} 在 gap={gap} 上出题即失败："
                        f"{type(exc).__name__}: {exc}") from exc
                options = tuple(getattr(question, "options", ()) or ())
                if not str(getattr(question, "question", "") or "").strip():
                    raise ClarifyRegistryError(f"插件 {plugin_id} 没给出问句文本")
                if not options:
                    raise ClarifyRegistryError(
                        f"插件 {plugin_id} 的问句没有选项（客户端渲染不出来，"
                        f"也违反「注册即校验」）")
                if not str(getattr(question, "plugin_id", "") or "").strip():
                    raise ClarifyRegistryError(
                        f"插件 {plugin_id} 的问句没带 plugin_id（回填会认不出来）")
            patch = plugin.apply("__probe__")
            if not hasattr(patch, "values"):
                raise ClarifyRegistryError(f"插件 {plugin_id} 的 apply() 必须返回 SlotPatch")
        return self.ids()
