"""`python -m agent` —— 起 Agent 服务壳。

★ 为什么入口是 `python -m agent` 而不是 `uvicorn agent.server:app`：
  后者要先 import 模块再让 uvicorn 拼配置，**`.env` 的装载时机会变得隐晦**
  （读环境变量发生在 import 期还是启动期？）。这里显式建 app 再交给 uvicorn，
  顺序是确定的：**先装 .env → 再读配置 → 再建 app**。

★ 与业务系统的 `python -m observed.native_run` 对称：都在宿主上前台运行，
  `Ctrl+C` 就是停止（不放后台、不需要 PID 记账）。

用法::

    .\\run.ps1 agent-up                 # 默认监听 AIOPS_AGENT 里的端口（默认 8000）
    .\\run.ps1 agent-up --port 8010     # 换端口
"""

from __future__ import annotations

import argparse
import logging
import sys

from .common import envfile
from .server import DEFAULT_AGENT_PORT, create_app, resolve_port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description="AIOps Agent（注册系统工作台、任务诊断、SSE与知识问答）",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认 127.0.0.1；★ 不要用 localhost）")
    parser.add_argument("--port", type=int, default=None,
                        help=f"监听端口（默认取 AIOPS_AGENT，再不行 {DEFAULT_AGENT_PORT}）")
    parser.add_argument("--log-level", default=None, help="日志级别（默认取 AIOPS_LOG_LEVEL）")
    args = parser.parse_args(argv)

    envfile.load_env_file()
    import os

    port = args.port or resolve_port()
    level = (args.log_level or os.environ.get("AIOPS_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # ★★ **非回环绑定必须配共享密钥**，否则拒绝启动（fail-closed）。
    #   理由：`/v1/alerts` 是设计上要被 Alertmanager 从网络调用的端点，
    #   "这个端口没人知道"不是安全措施；而一次误绑定（`--host 0.0.0.0`）就意味着
    #   任何人都能触发**付费**排查。宁可起不来，也不要静默裸奔。
    loopback = {"127.0.0.1", "::1", "localhost"}
    if args.host.strip().lower() not in loopback and not (
            os.environ.get("AIOPS_AGENT_TOKEN") or "").strip():
        print("=" * 72)
        print(f"  拒绝启动：--host {args.host} 不是回环地址，但没有配置 AIOPS_AGENT_TOKEN")
        print("  说明：该端点会被 Alertmanager 从网络调用，无鉴权等于对外开放付费排查")
        print("  两种改法：① 绑回环 --host 127.0.0.1；② 配 AIOPS_AGENT_TOKEN=<共享密钥>")
        print("=" * 72)
        return 2

    app = create_app(
        # ★★ 没配密钥时，**由 CLI 替本机开发显式声明**"这次不要鉴权" ——
        #   而 `create_app` 自己在没人声明时是 fail-closed（403）的。
        #   判据仍然跟着能力走：绕过 CLI 直接 `uvicorn --factory` 起，就会是 403 而不是裸奔。
        allow_unauthenticated=(args.host.strip().lower() in loopback
                               and not (os.environ.get("AIOPS_AGENT_TOKEN") or "").strip()))
    router = app.state.chat_service.router
    print("=" * 72)
    print("  AIOps Agent · 受控任务与注册系统工作台")
    print("=" * 72)
    print(f"  监听     : http://{args.host}:{port}")
    print(f"  接口     : /webui/ · POST /v1/chat 与 /v1/chat/stream · GET /health")
    print(f"  鉴权     : {'已启用（AIOPS_AGENT_TOKEN）' if app.state.chat_service.auth_token else '**未启用**（本机开发，已显式声明；仅回环绑定）'}")
    print(f"  模型     : {'已启用' if router.enabled else '未配置 → 规则/已配置向量；低分或缺知识能力时降级'}")
    print(f"  旧槽位配置（查询目标以注册中心为准）: {', '.join(router.slot_config.service_names) or '（空）'}")
    print("  停止     : Ctrl+C")
    print("=" * 72)

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=port, log_level=level.lower())
    except KeyboardInterrupt:                      # pragma: no cover
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
