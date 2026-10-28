"""被观测系统 · payment 服务（支付）。

对外端点：
    POST /pay             支付（query: order_id, amount）
    GET  /refund-policy   退款政策 —— ★ 对照端点：**不访问数据库**

★ 本服务没有数据库，也没有任何人为延迟/错误率旋钮。

★ 本文件刻意不 import ``main``（副作用隔离，理由见 order/__init__.py）。
"""

from __future__ import annotations
