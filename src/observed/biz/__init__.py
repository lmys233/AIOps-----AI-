"""被观测业务系统的**业务侧共享代码**。

★ 边界（这是本包存在的全部意义）：

   本包里**只有业务**：契约表、配置、错误分类、连接池、下游调用、应用装配。
   本包里**没有**任何采集逻辑 —— 指标 / 日志 / 链路 / 响应增强 / 变更留痕
   全部属于数据采集 SDK（`src/aiops_observed/`）。

   模块清单：

   | 模块 | 职责 | 说明 |
   |---|---|---|
   | `api_contract.py` | 端点与方法的唯一副本 | 纯契约 |
   | `settings.py` | 配置装载（**无故障旋钮**） | 纯配置 |
   | `errors.py` | 错误分类与 HTTP 状态映射 | 业务只分类，不推导状态码 |
   | `pool.py` | 连接池 = **`asyncpg.create_pool` 的薄封装**（**不上报指标**） | ★ 用标准驱动池，SDK 才能猴补工厂 |
   | `downstream.py` | 下游 HTTP 调用（用 `httpx`，**不算 gap**） | ★ 标准客户端，SDK 才能猴补 |
   | `app.py` | 应用装配 / 关停 / 错误响应 | 纯业务壳 |

   ★ **池与 HTTP 客户端都必须是"标准驱动/标准库"的产物** ——
   这是零代码改动接入 SDK 的硬前提：SDK 只能猴补**驱动工厂**，
   自研的连接池或私有 HTTP 客户端它挂不上。

   验证方式是机械可查的：`tests/verify_phase1.py --task t6` 会断言
   `src/observed/*/main.py` 里**不得出现** `_server_elapsed_ms` / `prometheus_client`
   / `structlog` / `opentelemetry` / `change_events` 这些字样。

★ 为什么业务侧还要有"共享包"：

   4 个服务共享的是**契约与基础设施**（端口表、错误分类、连接池实现），
   不是业务逻辑 —— 服务间仍然只通过 HTTP 交互。
   它和"服务间不共享代码"并不冲突：共享的是"大家都要遵守的那份约定"。

★ 本目录就是**业务代码的全部**：

   `src/observed/` 下除启动器（`native_run.py`）与压测客户端（`loadgen.py`）
   两个工具外，所有 Python 代码都在本包里。
   采集 SDK 是**独立的包** `src/aiops_observed/`，不从这里的旧代码迁移。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
