"""驱动适配器：猴补驱动工厂，把返回的对象换成自己的包装器。

★ 这是"零代码改动"的**技术核心**：猴补（monkeypatch）第三方库的**工厂函数**，
  让 SDK 在**工厂返回处**把对象换成自己的包装器。

  为什么"在工厂处替换"是关键：

    业务代码里写的是 `pool = await asyncpg.create_pool(dsn)`。
    SDK 不碰业务代码，而是把 `asyncpg.create_pool` 本身换掉 ——
    业务拿到的就是**被包装过的池**，于是"只有池的实现者才知道"的
    `in_use` / `waiting` / `acquire_seconds` 全都能测到。

    这与 OTel / Datadog 那一类 APM 的做法完全一致。

★ 已实现的适配器（**目前只有这 2 个**）：

    | 文件 | 猴补对象 | 产出指标 |
    |---|---|---|
    | `pool_asyncpg.py` | `asyncpg.create_pool` | `db_pool_*` 四条 |
    | `http_httpx.py` | `httpx.Client/AsyncClient.send` | `downstream_*` 两条 + gap |

  也就是说：HTTP 这一路只覆盖 `httpx`（`requests` / `aiohttp` 没有适配器），
  连接池这一路只覆盖 `asyncpg`。
  `config.KNOWN_POOL_DRIVERS` 里另外两个驱动名（`psycopg_pool` / `sqlalchemy`）
  **只是"允许这么写"**，它们**没有**适配器 —— 声明了它们时的行为见下。

★ 覆盖不到的怎么办（**必须吵，不能静默**）：

  自研连接池、私有 HTTP 客户端、以及上面那些"声明了但没有适配器"的驱动，
  一律 **告警一次**（"这个 driver 没有适配器"），并让 `db_pool_*` 以值 0 暴露 ——
  **"值为 0"与"指标不存在"必须可区分**。

  绝不允许的做法：悄悄不采集，让下游以为"这个服务没有池"。
"""

from __future__ import annotations

__all__: list[str] = []
