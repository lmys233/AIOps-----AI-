"""被观测系统 · order 服务（下单主链路枢纽）。

对外端点：
    POST /orders              下单（query: sku, qty）—— 占数据库连接 + 调下游
    GET  /orders/{order_id}   查询 —— ★ 对照端点：**不访问数据库**
    GET  /pool-stats          连接池自省

★ 连接池参数走正常配置项（``DB_POOL_SIZE`` / ``DB_POOL_TIMEOUT_SEC``），
  没有任何人为延迟/错误率旋钮。

★ 本文件刻意不 import ``main``：
  应用对象在 ``main`` 模块导入时构造（含配置装载与指标注册），
  由 ASGI 服务器显式导入 ``observed.order.main:app`` 触发。
  在包的 ``__init__`` 里顺带导入会让"仅仅 import 包"也产生副作用
  （注册 Prometheus 指标、装载配置），给测试和工具脚本埋坑。
"""

from __future__ import annotations
