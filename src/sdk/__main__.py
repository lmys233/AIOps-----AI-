"""`python -m sdk <子命令>` —— SDK 的命令行入口。

    python -m sdk run    --config aiops-sdk.toml   # 经 SDK 启动业务（零代码改动的落点）
    python -m sdk verify --config aiops-sdk.toml   # 接入自检

★ 为什么要有 `run` 这个子命令而不是让 SDK 提供一个被 import 的函数：

  "零代码改动"要求业务侧**没有地方**可以调用 SDK。启动动作只能来自**命令**，
  而不是来自业务代码里的一行 `ObservedApp(...)`。
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import ConfigError
from .launcher import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sdk",
        description="AIOps 数据采集 SDK（零代码改动接入被观测业务后端）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="经 SDK 启动业务应用（挂上采集能力）")
    p_run.add_argument("--config", default=None,
                       help="项目级配置文件（默认在当前目录找 app.yaml，或环境变量 AIOPS_SDK_CONFIG）")
    p_run.add_argument("--service", default=None,
                       help="跑哪个服务（项目配置里有多个 aiops.services 段时必须给）")
    p_run.add_argument("--host", default=None, help="覆盖配置里的监听地址")
    p_run.add_argument("--port", type=int, default=None, help="覆盖配置里的监听端口")
    p_run.add_argument("--reload", action="store_true", help="开发期热重载（生产不要开）")
    p_run.add_argument("--profile", default=os.getenv('AIOPS_SDK_DEPLOYMENT_PROFILE') or None,
                       help="可选部署环境 JSON；默认 AIOPS_SDK_DEPLOYMENT_PROFILE")

    p_verify = sub.add_parser("verify", help="接入自检（T12 交付）")
    p_verify.add_argument("--config", default=None, help="项目级配置文件")
    p_verify.add_argument("--service", default=None, help="跑哪个服务")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "run":
            if args.profile:
                from .deployment import apply_profile
                apply_profile(args.profile,args.service)
            return run(args.config, service=args.service,
                       host=args.host, port=args.port, reload=args.reload)
        if args.command == "verify":
            from .selfcheck import verify_from_config

            return verify_from_config(args.config, args.service)
    except ConfigError as exc:
        # ★ 配置错误要**一眼看出是配置问题**，而不是被埋在 traceback 里
        print(f"\n[SDK 配置错误] {exc}\n", file=sys.stderr)
        return 2
    except ImportError as exc:
        print(f"\n[SDK 接入错误] {exc}\n", file=sys.stderr)
        return 2

    print(f"未知子命令：{args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
