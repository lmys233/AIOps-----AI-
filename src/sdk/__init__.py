"""AIOps 数据采集 SDK。

★ 一句话定位：

    已适配的 HTTP/数据库/标准工作池优先使用配置 + 启动器采集；
    自研池用显式协议接入，外部数据库审计和退出事实由独立观察器提供。

    ```powershell
    pip install -e .                      # ① 装依赖
    # ② 写 aiops-sdk.toml（服务名 / 应用入口 / 下游映射 / 池驱动 / 依赖清单）
    python -m sdk run --config aiops-sdk.toml   # ③ 换启动命令
    ```

★ 它是怎么做到的（四个挂载点，业务全程不知道 SDK 存在）：

    | 手段 | 挂载点 | 拿到什么 |
    |---|---|---|
    | ASGI 包装 | 启动器导入业务 app 后包一层 | 三探针 / HTTP 指标 / 响应增强 |
    | import hook（`sitecustomize`） | 解释器启动时注入 PYTHONPATH | OTLP 链路、`traceparent` 透传 |
    | 驱动工厂猴补 | `httpx` / `asyncpg` 等**工厂函数** | `downstream_*` / `db_pool_*` |
    | 接管 `logging` 根 logger | 挂自己的 handler/formatter | 单行 JSON + 七字段 |

★ 边界（必须知道，否则会误以为"接上了"）：

    · **必须经 SDK 启动**。直接 `uvicorn myapp:app` 跑，三探针与指标一个都不会有。
    · **猴补只覆盖到已适配的驱动**。自研连接池/私有 HTTP 客户端不在覆盖范围，
      此时 SDK 报机器可读 unsupported；兼容零值不能作为健康证据。
    · **业务若自带 `_server_elapsed_ms`，SDK 保留它的值**，不覆盖。

★ 与另两个包的关系（三者互不依赖）：

    `agent/`    只读遥测后端，不 import 本包
    `observed/` 被观测业务系统，**不知道本包存在**
    `sdk/`      只认配置里的 `"module:attr"` 字符串，不 import 任何业务模块
"""

from __future__ import annotations

from .app import ObservedApp
from .config import ConfigError, SdkConfig, load_config
from .contract import CONTRACT_VERSION
from .state import Runtime, get_runtime, set_runtime
from .selfcheck import verify, verify_from_config

__version__ = "0.3.0"

__all__ = [
    "__version__",
    "CONTRACT_VERSION",
    "ObservedApp",
    "Runtime",
    "SdkConfig",
    "ConfigError",
    "load_config",
    "get_runtime",
    "set_runtime",
    "verify",
    "verify_from_config",
]
