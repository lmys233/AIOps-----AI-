"""启动器 · **零代码改动接入的落点**。

     python -m sdk run --config aiops-sdk.toml

★ 为什么"零改动"必须靠一个启动器来实现：

  业务后端的 `main.py` 里没有（也不该有）SDK 的任何东西。
  那么"谁来把 ASGI 中间件挂上去"？只能是**启动的人**。

  于是分工变成：

  | 谁 | 做什么 | 是否需要改业务代码 |
  |---|---|---|
  | 业务开发 | 照常写 FastAPI，照常 `uvicorn` 调试 | —— |
  | 部署/运维 | 加一份 TOML，把启动命令换成 `python -m sdk run` | **否** |

★ 三条"绝不做"的自我约束：

  1. **绝不 import 业务的私有符号**：只认配置里的 `"module:attribute"` 字符串。
     这是"业务不需要知道 SDK 存在"的实现方式 ——
     SDK 里出现 `import myapp` 就等于把两个项目焊死了。
  2. **导入失败要炸得响**：模块名写错、属性不存在、app 不是 ASGI 可调用对象，
     一律在**启动时**带真实原因退出。静默退化成"没采集"是最坏的结果。
  3. **不改 uvicorn 的日志配置**（`log_config=None`）：日志格式归 SDK 的日志层管，
     两个地方同时配会互相覆盖（promtail 上踩过同类问题）。
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, Optional

from .tracing import resource_attributes
from . import instrument
from .app import ObservedApp
from .config import SdkConfig, load_config
from .state import Runtime, set_runtime

__all__ = ["run", "import_app", "prepare_environment", "banner"]

# 本包所在目录（`<repo>/src`）—— 启动子进程时要用它拼 PYTHONPATH
_SRC_DIR = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------
#  导入业务应用
# ------------------------------------------------------------------
def import_app(app_ref: str) -> Any:
    """把 `"module.path:attribute"` 变成真正的 ASGI 应用对象。

    ★ 失败信息必须包含**真实原因**：
      "找不到模块"和"模块里没有这个属性"是两件事，排查动作完全不同。
    """
    if ":" not in app_ref:
        raise ImportError(
            f"[service].app 取值 {app_ref!r} 格式不对 —— "
            "应为 '模块路径:属性名'，例如 'myapp.main:app'"
        )
    module_name, _, attr = app_ref.partition(":")
    module_name, attr = module_name.strip(), attr.strip()
    if not module_name or not attr:
        raise ImportError(f"[service].app 取值 {app_ref!r} 的模块名或属性名为空")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"导入业务应用 {module_name!r} 失败：{exc} —— "
            "检查模块路径是否正确、依赖是否装齐、以及运行时的工作目录/`PYTHONPATH` 是否包含它"
        ) from exc

    if not hasattr(module, attr):
        raise ImportError(
            f"模块 {module_name!r} 里没有属性 {attr!r} —— "
            f"该模块实际导出：{sorted(n for n in vars(module) if not n.startswith('_'))}"
        )
    app = getattr(module, attr)
    if not callable(app):
        raise ImportError(
            f"{app_ref} 取到的对象不可调用（{type(app).__name__}）—— 它不是一个 ASGI 应用"
        )
    return app


# ------------------------------------------------------------------
#  环境准备
# ------------------------------------------------------------------
def apply_env_bridge(cfg: SdkConfig) -> list[str]:
    """把项目配置**业务段**里声明过的参数落到进程环境变量。

    ★ 这就是"方案 A：env_bridge"——业务代码零改动。

      业务怎么读配置是**业务自己的事**：
        · 像我们的被观测系统（Python，`os.environ[...]`）→ 需要有人把值落进环境
        · 像 Spring 那类自己读 `application.yaml` 的 → **不写 `env_bridge`**，
          SDK 只管 diff 配置、不管取值

    ★ 优先级沿用项目既有约定：**shell 环境变量 > 配置文件**。
      所以"临时覆盖一个参数做演练"依然有效，而且那种覆盖**也会被 diff 抓到**
      （因为留痕 diff 的是"最终生效值"，见 `changes.py`）。
    """
    notes: list[str] = []
    for key, env_name in sorted(cfg.env_bridge.items()):
        if key not in cfg.business:
            continue
        value = cfg.business[key]
        existing = os.environ.get(env_name)
        if existing is not None and existing.strip():
            notes.append(f"{env_name}: 由 shell 提供（保持），配置里的 {key}={value} 未生效")
            continue
        os.environ[env_name] = str(value)
        notes.append(f"{env_name} <- 配置 {key} = {value}")
    return notes


def prepare_environment(cfg: SdkConfig) -> list[str]:
    """把链路与自动埋点需要的环境变量设好，返回"做了什么"的说明列表。

    ★ `OTEL_METRICS_EXPORTER=none` 是**必须的**：
      指标走 `/metrics` 被抓取，若同时经 OTLP 再导出，
      同一份指标会出现**两套 series**，`sum()` 类查询直接翻倍。

    ★ 这里设置的是**进程环境变量**，因此对子进程也成立；
      但 `sitecustomize` 只在解释器启动时执行 ——
      当前进程的自动埋点由 `initialize_in_process()` 显式调用完成。
    """
    notes: list[str] = []
    env = os.environ
    env["OTEL_SERVICE_NAME"] = cfg.service_name
    # ★★ 与 `tracing.setup_tracing` 同一口径（从 cfg 取，取不到就省略），
    #   以前这里也写死了 `deployment.environment=prod,cluster=local-1`。
    env["OTEL_RESOURCE_ATTRIBUTES"] = resource_attributes(cfg.service_name, cfg)
    notes.append(f"OTEL_SERVICE_NAME={cfg.service_name}")

    if cfg.otlp_endpoint:
        env["OTEL_TRACES_EXPORTER"] = "otlp"
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = cfg.otlp_endpoint
        env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"
        notes.append(f"OTEL_TRACES_EXPORTER=otlp -> {cfg.otlp_endpoint}")
    else:
        notes.append("未配置 [tracing].otlp_endpoint —— 不出链路")
    env["OTEL_METRICS_EXPORTER"] = "none"
    env["OTEL_LOGS_EXPORTER"] = "none"
    notes.append("OTEL_METRICS_EXPORTER=none（指标只走 /metrics 被抓取，避免两套 series）")

    # PYTHONPATH：让子进程/重载场景也能 import 到 sdk 与自动埋点目录
    parts: list[str] = [str(_SRC_DIR)]
    # ★ SDK 自带的 sitecustomize 目录放在最前：解释器启动时会自动导入它，
    #   于是"不经启动器直接跑业务"也能被埋点（见 src/sdk/auto/sitecustomize.py）
    auto_dir = Path(__file__).resolve().parent / "auto"
    if (auto_dir / "sitecustomize.py").exists():
        parts.insert(0, str(auto_dir))
        notes.append(f"SDK sitecustomize 目录已加入 PYTHONPATH：{auto_dir}")
    env["AIOPS_SDK_AUTOTRACE"] = "1"
    otel_dir = _otel_auto_instrumentation_dir()
    if otel_dir is not None:
        parts.append(str(otel_dir))
        notes.append(f"OTel 官方自动埋点目录已加入 PYTHONPATH：{otel_dir}")
    else:
        notes.append("[!] 未找到 opentelemetry 的 auto_instrumentation 目录 —— 当前环境装不了链路")
    existing = env.get("PYTHONPATH")
    if existing:
        for piece in existing.split(os.pathsep):
            if piece and piece not in parts:
                parts.append(piece)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return notes


def _otel_auto_instrumentation_dir() -> Optional[Path]:
    """OTel 官方自动埋点目录（内含 `sitecustomize.py`）。

    ★ 为什么不调用 `opentelemetry-instrument` 命令：
      它内部是 `os.execl(...)`，**Windows 上 exec 会换 PID**
      （已实测）。多一层进程跳转没有任何收益。
    """
    try:
        import opentelemetry.instrumentation.auto_instrumentation as auto  # noqa: PLC0415
    except ImportError:
        return None
    directory = Path(auto.__file__).resolve().parent
    return directory if (directory / "sitecustomize.py").exists() else None


# ------------------------------------------------------------------
#  启动
# ------------------------------------------------------------------
def banner(cfg: SdkConfig, notes: list[str], bridge: Optional[list[str]] = None) -> None:
    from . import CONTRACT_VERSION, __version__

    print("=" * 68)
    print(f"  AIOps 数据采集 SDK {__version__}（接入契约 {CONTRACT_VERSION}）")
    print("=" * 68)
    print(f"  服务名   : {cfg.service_name}")
    print(f"  业务应用 : {cfg.app_ref}   ← SDK 不认识它，只按字符串导入")
    print(f"  监听     : http://{cfg.host}:{cfg.port}")
    print(f"  项目配置 : {cfg.config_path}")
    print(f"  日志目录 : {cfg.log_dir or '（未配置，仅 stdout）'}")
    print(f"  下游映射 : {dict(cfg.downstream) or '（无）'}")
    print(f"  池驱动   : {cfg.pool_driver or '（本服务无数据库池）'}")
    print(f"  /ready   : {dict(cfg.ready) or '（无依赖声明）'}")
    print("  ── 业务参数 → 环境变量（env_bridge，方案 A）──")
    for note in (bridge or []):
        print(f"    · {note}")
    print("  ── SDK 环境准备 ──")
    for note in notes:
        print(f"    · {note}")
    print("=" * 68)


def _print_activation(report: dict[str, Any]) -> None:
    """打印接管结果：池 / HTTP 客户端 / 链路 endpoint 与 instrumentor。"""
    print(f"  自动埋点 : 池={report.get('pool') or '（未声明池驱动）'}；"
          f"HTTP 客户端={report.get('http') or '（无）'}")
    tracing = report.get("tracing") or {}
    if tracing:
        installed = tracing.get("instrumented") or []
        reach = tracing.get("endpoint_reachable")
        print(f"  链路     : endpoint={tracing.get('endpoint') or '（未配置）'} "
              f"可达={reach} instrumentor={installed or '（无）'}")


def run(config_path: Optional[str] = None, *, service: Optional[str] = None,
        host: Optional[str] = None, port: Optional[int] = None,
        reload: bool = False) -> int:
    """装载项目配置 → 落到环境变量 → 准备环境 → 导入业务 app → 包一层 → uvicorn。"""
    import uvicorn  # 局部导入：让 `sdk.verify()` 之类的用法不必拖上 uvicorn

    cfg = load_config(config_path, service=service)
    # ★ 顺序：先把配置落到环境变量，**再**导入业务代码 ——
    #   业务模块导入期就会读 os.environ（`biz/settings.py` 的 load_settings 是
    #   fail-fast 的，缺 RUNNER_DATABASE_URL 会直接抛）。
    bridge = apply_env_bridge(cfg)
    notes = prepare_environment(cfg)
    banner(cfg, notes, bridge)

    # ★ 顺序不能换：先把 Runtime 建好、把猴补挂上，**再**导入业务代码。
    #   业务的连接池往往在导入期或 lifespan 里创建 —— 猴补晚一步就拿不到对象，
    #   `db_pool_*` 会永远报 0（而且看起来像"池一直空闲"，极具误导性）。
    runtime = Runtime(service_name=cfg.service_name, config=cfg)
    set_runtime(runtime)
    report = instrument.activate(runtime, cfg)
    _print_activation(report)

    # ★ 变更留痕：后台线程跑，**绝不拖住启动**（DB 不可达时要等连接超时）
    from .changes import record_startup_change  # 局部导入：只在需要时加载 asyncpg

    record_startup_change(runtime, cfg)
    print("  变更留痕 : 已在后台写入 change_events（配置未变则不写）")
    print()

    app = import_app(cfg.app_ref)
    # ★ 构造**不再**做全局安装（`ObservedApp.activate` 才是那一步）——本函数的
    #   上面已经 `set_runtime` + `instrument.activate` 过了，这里只把接管结果带给
    #   自省用的 `describe()`。
    wrapped = ObservedApp(app, config=cfg, runtime=runtime)
    wrapped.activation = dict(report)

    uvicorn.run(
        wrapped,
        host=host or cfg.host,
        port=port or cfg.port,
        # ★ 不接管 logging 配置：格式归 SDK 的日志层，两处同配会互相覆盖
        log_config=None,
        # 访问日志由 SDK 的日志层统一产出（纯文本访问日志会污染 JSONL）
        access_log=False,
        reload=reload,
    )
    return 0
