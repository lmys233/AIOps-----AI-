"""AIOps 项目根包（源码根，src-layout）。

目录结构：

    src/
      agent/           AIOps Agent 主程序
        common/        共享设施（配置、枚举、契约、接口）
      observed/        被观测业务系统（4 个微服务）
      sdk/             ★ 数据采集 SDK（可分发；**零代码改动**接入任意业务后端）

三者互不依赖：

    agent  ──只读遥测后端（Prometheus/Loki/Tempo/Postgres）──▶ 不 import observed / sdk
    sdk    ──包住业务 app（ASGI 中间件 + 驱动猴补）──────────▶ 不 import observed / agent
    observed ──纯业务，**不知道 sdk 存在**──────────────────▶ 不 import sdk / agent

★ 验收时需把 src/ 加入 sys.path：

    import sys; sys.path.insert(0, "src")
    import agent, observed, sdk

或使用 PYTHONPATH：

    set PYTHONPATH=src          （Windows cmd）
    $env:PYTHONPATH = "src"     （PowerShell）
"""

__version__ = "0.1.0"
