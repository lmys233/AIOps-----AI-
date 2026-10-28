"""扩展工具（**动态注册**的落点）。

★ 这里的模块**不会**被自动发现：只有 `src/agent/tools/extensions.yaml` 里
  **显式列出**且 `enabled: true` 的模块才会被 `loader.py` 加载。
  理由见 `loader.py` 的模块说明：目录扫描 = 谁能写文件谁就能给自己加工具。

★ 写一个扩展只需要**一个文件**：

```python
from ..definition import define_tool
from ..policy import CAP_METRICS
from ..sources import RawOutcome

@define_tool(name="my_tool", description="...", parameters={...},
             capabilities=(CAP_METRICS,), granted_roles=("metrics_agent",))
async def my_tool(ctx, params) -> RawOutcome:
    return RawOutcome(data=[...], raw_ref="...")
```

★ 权限上要记住：`granted_roles` 只是**申请**。最终能不能调由能力策略裁定 ——
  声明 `capabilities=(CAP_WRITE,)` 而写能力没开，注册就会被拒。
"""

__all__: list[str] = []
