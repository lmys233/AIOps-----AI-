"""被观测业务系统 · 启动器（前台运行；Ctrl+C 停止）。

一条命令把 4 个微服务起在本机上：

    终端 A:  .\\run.ps1 business-up        # 起 4 个服务，占住这个终端
    终端 B:  .\\run.ps1 verify-observed     # 在另一个终端验收
    结束:    回到终端 A 按 Ctrl+C

也可以直接 ``python -m observed.native_run``（不带参数即全部服务；
只起个别服务就写成 ``python -m observed.native_run order``）。

★ 终端 B 请统一用 ``.\\run.ps1 <命令>``，**不要直接敲 `python …`**：
  PATH 上的 python 未必是项目的 `.venv`（本机实测：它会报
  `ModuleNotFoundError: prometheus_client`，因为那是另一个解释器）。
  `run.ps1` 自己会挑 `.venv\\Scripts\\python.exe` 并把 `src/` 加进 PYTHONPATH。

★ 服务经 SDK 启动，就绪判定是 SDK 提供的 `/health` 返回 200：
  "端口通了"只说明"有东西在听"，`/health` 才说明"业务可用"。

★ 本启动器**不注入任何业务参数**：`DB_POOL_SIZE` / `*_URL` / `LOG_LEVEL`
  都写在项目配置 `src/observed/app.yaml` 的业务段里，由 SDK 按 `env_bridge`
  落到环境变量。启动器只负责两件它**必须**负责的事：

  1. **数据库凭据**（`RUNNER_DATABASE_URL`）—— 它不能进配置文件（那要进仓库）
  2. **PYTHONPATH** —— 让子进程能 `import sdk` / `import observed`

  端口也只在启动器里出现一次，用于拉起与就绪探测。

★ 停止不做跨进程记账：
  4 个子进程与启动器同属一个控制台进程组，Ctrl+C 会一起发给他们，
  uvicorn 收到后优雅退出；子进程是启动器的直系子进程，``proc.poll()``
  就能知道它死没死，不需要 PID 文件。

★ 唯一的例外是"直系子进程"这条在 Windows 本机不成立：
  `.venv\\Scripts\\python.exe` 是 venv 的**启动垫片**，它会再拉起 base 解释器。
  也就是说服务实际是"垫片 + 真解释器"两个进程，而 ``Popen`` 拿到的只是垫片 ——
  只 ``terminate()`` 垫片，真正监听 8080~8083 的 uvicorn 会变成**孤儿**，
  下一次 `business-up` 报"端口已被占用"。
  所以停止走 :func:`_stop_process`：Windows 上用 ``taskkill /T`` 杀**整棵树**。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from observed.biz.settings import SERVICE_PORTS

__all__ = ["main", "SERVICES", "START_ORDER", "build_command", "service_env"]

#   __file__ = <repo>/src/observed/native_run.py  →  parent.parent.parent = <repo>
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_ROOT / "src"
RUN_DIR = REPO_ROOT / ".run"
LOG_DIR = RUN_DIR / "logs"          # 供 promtail 采集的 JSONL 目录（SDK 拉起后才会真的有文件）

# 宿主视角的连接串：Postgres 在 Docker 里，端口已发布到宿主。
# 用受限角色 aiops_runner —— 故障注入脚本与 Agent 运行时都用它，权限被刻意收窄。
# ★ 这是**凭据**，所以它不进项目配置文件（那要进仓库），由启动器/环境提供。
RUNNER_DSN = "postgresql://aiops_runner:runner_dev_pw@127.0.0.1:5432/aiops"

# ★ 项目级配置（唯一一份）：业务参数 + SDK 接入清单
SDK_CONFIG = SRC_DIR / "observed" / "app.yaml"

# 4 个服务：端口 8080~8083，对外契约以这份表为准。
#
# ★ 这里**只有端口**，别的一律不写：
#   · 下游地址 / 池大小 / 日志级别 —— 它们现在是**项目配置里的业务参数**
#     （`app.yaml` 的 `services.*` / `db.pool.*`），由 SDK 按 `env_bridge`
#     落到环境变量。启动器再写一份就等于**两个来源**，迟早漂移。
SERVICES: dict[str, dict[str, Any]] = {
    "payment":   {"port": 8082},
    "inventory": {"port": 8083},
    "order":     {"port": 8081},
    "gateway":   {"port": 8080},
}

# 启动顺序：被调用方先起。gateway → order → (inventory, payment)。
START_ORDER = ("payment", "inventory", "order", "gateway")

# ★ 端口只有一份：`observed.biz.settings.SERVICE_PORTS`。
#   本文件里的字面量必须与它逐条一致 —— 不一致就在**导入时**炸掉，
#   而不是等到运行期表现为"服务起在 8081 但契约说 8083"。
assert {name: int(cfg["port"]) for name, cfg in SERVICES.items()} == dict(SERVICE_PORTS), (
    "启动器端口表与 observed.biz.settings.SERVICE_PORTS 不一致"
)
assert set(START_ORDER) == set(SERVICES), "启动顺序与服务名单不一致"


# ------------------------------------------------------------------
#  命令与环境变量
# ------------------------------------------------------------------
def build_command(service: str, profile: Optional[str] = None) -> list[str]:
    """``python -m sdk run --config <项目配置> --service <服务>``（**经 SDK 启动**）。

    ★ 为什么不直接 ``uvicorn observed.<svc>.main:app``：

      三路采集全靠 SDK 挂上去：

      | 谁需要什么 | 少了 SDK 会怎样 |
      |---|---|
      | Prometheus 抓 `/metrics` | 404 → 抓取目标 DOWN，指标一条都没有 |
      | promtail 读 `.run/logs/*.jsonl` | 目录是空的，日志一条都没有 |
      | Collector 收 OTLP span | 没有链路 |

      也就是说：**"不起 SDK"不再是"少采一点"，而是"三路全空"**。

    ★ 仍然用 ``sys.executable -m sdk`` 而不是 ``aiops-sdk`` 脚本：
      不依赖 PATH，也不会起错解释器（`-m` 用的是当前解释器的 site-packages）。
    """
    command = [
        sys.executable, "-m", "sdk", "run",
        "--config", str(SDK_CONFIG),
        "--service", service,
    ]
    return command + (["--profile", profile] if profile else [])


def service_env(service: str, base_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """构造**启动器**要传给子进程的环境变量。

    ★ 项目级配置 `src/observed/app.yaml` 是**唯一一份配置**，
      业务参数由 SDK 按 `env_bridge` 落到环境变量。
      所以这里**不再**逐服务构造 `DB_POOL_SIZE` / `*_URL` / `LOG_LEVEL` ——
      那些键的**值**已经写在配置里，重复构造只会有两个来源、迟早漂移。

      只留两件启动器**必须**负责的事：

      1. **数据库凭据**（`RUNNER_DATABASE_URL`）—— 它**不能进配置文件**
         （配置文件要进仓库），所以只能由启动器/环境提供
      2. **PYTHONPATH** —— 让子进程能 `import sdk` 与 `import observed`
         （不依赖"恰好已经 pip install -e ."）
    """
    env = dict(base_env if base_env is not None else os.environ)

    # 数据库凭据：gateway 是纯转发层，没有连接池，也不需要它
    if service != "gateway":
        env["RUNNER_DATABASE_URL"] = env.get("RUNNER_DATABASE_URL") or RUNNER_DSN

    # PYTHONPATH：src-layout，让 `import sdk` / `import observed` 在任意工作目录下都成立
    parts: list[str] = [str(SRC_DIR)]
    existing = env.get("PYTHONPATH")
    if existing:
        for piece in existing.split(os.pathsep):
            if piece and piece not in parts:
                parts.append(piece)
    env["PYTHONPATH"] = os.pathsep.join(parts)

    return env


# ------------------------------------------------------------------
#  小工具
# ------------------------------------------------------------------
def port_in_use(port: int, timeout: float = 0.4) -> bool:
    """TCP 能否连上。

    用途有两个：① 启动前的"端口被占"前置检查；② `/health` 就绪判定的**兜底**。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def health_ok(port: int, timeout: float = 2.0) -> bool:
    """`/health` 是否返回 2xx。

    ★ 用 `/health` 而不是只看 TCP：SDK 会注册探针，所以"端口通了"已经不等于
      "服务可用了" —— `/health` 才是真正的就绪证据。
    """
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError):
        return False


def _stop_process(proc: subprocess.Popen, *, grace: float = 6.0) -> None:
    """停掉一个子进程 —— **连同它的子进程**。

    ★ 为什么不能只 ``proc.terminate()``：

      `.venv\\Scripts\\python.exe` 是 venv 的启动垫片，它会再拉起 base 解释器。
      也就是说服务实际是"垫片 + 真解释器"两个进程，而 ``Popen`` 拿到的只是垫片 ——
      只杀垫片，真正监听 8080~8083 的 uvicorn 会变成**孤儿**，
      下一次 `business-up` 就会报"端口已被占用"。`taskkill /T` 杀的是整棵树。

    ★ 只在 Windows 上走 taskkill；POSIX 上 `terminate()` 本来就够
      （且本项目未在 POSIX 上部署）。杀不掉时保持安静：
      调用方已经在收尾，这里再抛异常只会把真正的失败原因盖掉。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()


# ------------------------------------------------------------------
#  up（唯一子命令）
# ------------------------------------------------------------------
def cmd_up(services: list[str], *, wait_sec: float = 45.0, profile: Optional[str] = None) -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 便宜的前置检查：端口被占时先说清楚，别起一半 ----
    busy = [n for n in services if port_in_use(int(SERVICES[n]["port"]))]
    if busy:
        print("  以下端口已被占用，无法启动：")
        for name in busy:
            print(f"    · {name} 需要 {SERVICES[name]['port']}，但它已在监听")
        print("  提示：若占用者是上一次没退干净的 uvicorn，先回它的终端按 Ctrl+C；")
        print("        实在找不到就用 `netstat -ano | findstr :8080` 查出 PID，")
        print("        再 `taskkill /PID <pid> /T /F`（★ 不要 `taskkill /IM python.exe`：")
        print("        那会连别的项目的 python 一起杀掉）")
        return 2

    print(f"  仓库根 : {REPO_ROOT}")
    print(f"  项目配置: {SDK_CONFIG}  （**唯一一份**：业务段 + aiops 接入段）")
    print(f"  日志目录: {LOG_DIR}  （promtail 采这个目录）")
    print(f"  数据库 : {RUNNER_DSN}（凭据，gateway 不用）")
    print("  启动方式: 经 SDK（python -m sdk run）—— 探针/指标/日志/链路全靠它")
    print("  就绪判定: SDK 提供的 /health 返回 200")
    print("  前端 : 本命令会**占住这个终端**；验收请在另一个终端跑。Ctrl+C 全部停止。")
    print()

    procs: dict[str, subprocess.Popen] = {}
    # 输出**继承**当前终端：4 个服务的日志会交错打在一起，
    # 但每行都带服务名，可读也可筛。
    # ★ 不重定向到管道（管道缓冲区写满会让子进程阻塞在写 stdout 上，
    #   表现为"服务渐进式无响应"—— 实测过）。
    try:
        for name in START_ORDER:
            if name not in services:
                continue
            procs[name] = subprocess.Popen(
                build_command(name,profile),
                cwd=str(REPO_ROOT),
                env=service_env(name),
            )
            print(f"  → {name:<10} port={SERVICES[name]['port']} 已拉起（经 SDK）")

        # ---- 等 /health（HTTP 200）----
        #   ★ 判定用"/health 200"而不是"TCP 连通"："端口通了"只能说明
        #     "有东西在听"，不能说明"业务可用"。
        failed: list[str] = []
        for name, proc in procs.items():
            port = int(SERVICES[name]["port"])
            started = time.time()
            ok = False
            while time.time() - started < wait_sec:
                if proc.poll() is not None:      # 子进程死了，立刻知道（直系子进程）
                    break
                if health_ok(port):
                    ok = True
                    break
                time.sleep(0.3)
            if ok:
                print(f"  [OK] {name:<10} /health 就绪（{time.time() - started:.1f}s）")
            else:
                code = proc.poll()
                reason = f"进程已退出（exit={code}）" if code is not None else f"{wait_sec:.0f}s 内 /health 未就绪"
                failed.append(name)
                print(f"  [X]  {name:<10} {reason}（它的输出就在上面）")

        if failed:
            print(f"\n  结论: {failed} 未能就绪 —— 停止其余服务，请先修。")
            return 1

        print("\n  结论: 4 个服务已就绪。")
        print("      另一个终端（用 .\\run.ps1 调 —— 它自带正确的 venv 解释器）：")
        print("        .\\run.ps1 verify-t13                 # 采集链路行为判据")
        print("        .\\run.ps1 verify-observed            # 契约符合性（需 infra-up）")
        print("        .\\run.ps1 loadgen --target gateway    # 造流量")
        print("        .\\run.ps1 sdk-verify order           # SDK 接入自检")
        print("      ⚠ 不要直接敲 `python …`：PATH 上的 python 不是本项目的 .venv，")
        print("        会报 ModuleNotFoundError: prometheus_client")
        print("      结束：在本终端按 Ctrl+C")

        # ---- 前台守着：任一子进程退出就报出来 ----
        while True:
            time.sleep(1.0)
            for name, proc in procs.items():
                code = proc.poll()
                if code is not None:
                    print(f"\n  [X] {name} 已退出（exit={code}）→ 停止其余服务")
                    return 1
    except KeyboardInterrupt:
        print("\n  收到 Ctrl+C，正在停止 4 个服务 …")
        return 0
    finally:
        for proc in procs.values():
            _stop_process(proc)
        print("  已全部停止。")


# ------------------------------------------------------------------
#  CLI（只有 up —— down 就是 Ctrl+C，不需要子命令）
# ------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m observed.native_run",
        description="被观测业务系统 · 启动器（前台运行；Ctrl+C 停止）",
    )
    parser.add_argument("services", nargs="*", default=[],
                        help=f"只启动指定服务，默认全部；可选：{sorted(SERVICES)}")
    parser.add_argument("--wait", type=float, default=45.0,
                        help="等待每个服务端口就绪的秒数（默认 45）")
    parser.add_argument("--sdk-profile", default=os.getenv('AIOPS_SDK_DEPLOYMENT_PROFILE') or None,
                        help="可选 SDK 部署环境 JSON；默认 AIOPS_SDK_DEPLOYMENT_PROFILE")
    args = parser.parse_args(argv)

    unknown = [s for s in args.services if s not in SERVICES]
    if unknown:
        print(f"  未知服务 {unknown}，可选：{sorted(SERVICES)}")
        return 2
    selected = list(args.services) if args.services else list(START_ORDER)
    return cmd_up(selected, wait_sec=args.wait, profile=args.sdk_profile)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
