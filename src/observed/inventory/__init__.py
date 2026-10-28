"""被观测系统 · inventory 服务（库存）。

对外端点：
    GET  /check     库存查询（query: sku, qty）
    POST /reserve   库存预占（query: order_id, sku, qty）

★ 本服务没有数据库，也没有任何人为延迟/错误率旋钮；它的价值在于
  让 gateway→order→inventory→payment 成为**三跳链路**，
  从而产生"哪一跳变慢"的真实归因题目。

★ 本文件刻意不 import ``main``（副作用隔离，理由见 order/__init__.py）。
"""

from __future__ import annotations
