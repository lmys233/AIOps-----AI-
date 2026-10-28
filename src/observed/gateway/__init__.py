"""被观测系统 · gateway 服务（唯一对外入口，无数据库）。

对外端点：
    POST /api/orders              下单（query: sku, qty）
    GET  /api/orders/{order_id}   查询订单

★ 本服务**没有数据库**：它是纯转发层，因此 ``/ready`` 只探下游，
  指标里也不会出现 ``db_pool_*`` 系列。这本身就是一条可区分性证据。

★ 本文件刻意不 import ``main``（副作用隔离，理由见 order/__init__.py）。
"""

from __future__ import annotations
