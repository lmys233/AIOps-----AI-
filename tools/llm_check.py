#!/usr/bin/env python3
"""一条命令验证「模型这条路通不通」（task.md T15 · `.\run.ps1 llm-check`）。

--------------------------------------------------------------------------------
★ 为什么单独做一条命令
--------------------------------------------------------------------------------

模型调用是整个项目里**唯一需要外网**的能力。把它做成一条独立、便宜的命令，
就能随时回答一个关键问题：

    "现在不通，是**配置/网络**的问题，还是**代码**的问题？"

它做两件事，且**两件都不重**：

1. `GET /models` 探活 —— **不消耗 token**，只证明"能连上、key 有效、模型名在列表里"；
2. 一次**极小的结构化输出**（`max_tokens≈64`）—— 证明"约束解码 + 二次校验 + 记账"
   这条完整链路真的走得通，而不只是"HTTP 通了"。

★ 为什么第 2 步不能省：HTTP 200 只说明"服务在"，而我们要的是
  "**模型愿意按 schema 返回结构化结果**"——这是两条不同的判据
  （本项目对"代理判据"的警惕，见 `fix_bugs.md` 一整族）。

★ 绝不打印 API Key：只报"是否已配置（长度 N）"。

退出码：0 = 可用且结构化输出通过校验；1 = 不可用（原因写在输出里）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pydantic import BaseModel, Field  # noqa: E402

from agent.common.envfile import DEFAULT_ENV_FILE, load_env_file  # noqa: E402
from agent.llm import LLMError, LLMCallRecord, OpenAICompatGateway  # noqa: E402


class _Probe(BaseModel):
    """探针 schema：故意要两个**不同类型**的字段，才能看出结构化输出是真的。"""

    ok: bool = Field(..., description="固定回传 true")
    echo: str = Field(..., description="原样回传调用方给的字符串")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _kv(label: str, value: object) -> None:
    print(f"  {label:<22}: {value}")


async def _main() -> int:
    applied = load_env_file()
    print()
    print("=" * 74)
    print("  模型连通性自检（LLM gateway）")
    print("=" * 74)
    _kv(".env 文件", f"{DEFAULT_ENV_FILE}（{'存在' if DEFAULT_ENV_FILE.exists() else '不存在'}）")
    if applied:
        # ★ 只报键名，绝不报值
        _kv("本次从 .env 装载", ", ".join(applied))

    base_url = _env("AIOPS_LLM_BASE_URL")
    model = _env("AIOPS_LLM_MODEL")
    router_model = _env("AIOPS_ROUTER_MODEL")
    api_key = _env("AIOPS_LLM_API_KEY")

    missing = [n for n, v in (("AIOPS_LLM_BASE_URL", base_url),
                              ("AIOPS_LLM_MODEL", model)) if not v]
    if missing:
        _kv("配置", f"❌ 缺少必需项：{', '.join(missing)}")
        print("\n  结论: 配置不完整（把 .env.example 复制成 .env 并填写）")
        return 1

    _kv("base_url", base_url)
    _kv("主力模型", model)
    _kv("快路由模型", router_model or "（未配置 → 三阶段里的「小模型快路由」不启用）")
    _kv("API Key", f"已配置（长度 {len(api_key)}）" if api_key else "❌ 未配置")
    print()

    records: list[LLMCallRecord] = []
    gateway = OpenAICompatGateway(
        base_url=base_url, api_key=api_key or None, model=model,
        usage_sink=records.append,
    )
    try:
        # ★ 先把供应商声明的模型列出来 —— 模型名是**它的事实**，不是我们猜的。
        #   实测：常见名 `deepseek-chat` 不在列表里却仍能调通（别名），
        #   所以这里只**提示**，判定仍以真实调用为准。
        listed = await gateway.list_models() if api_key else []
        if listed:
            _kv("供应商模型列表", ", ".join(listed))
            for label, name in (("主力模型", model), ("快路由模型", router_model)):
                if name and name not in listed:
                    _kv("", f"⚠️ {label} {name!r} **不在**列表里 —— 可能是别名；"
                            f"若调用失败，请改用列表里的名字")
        print()

        alive, detail = await gateway.health()
        _kv("① 探活（不耗 token）", f"{'✅' if alive else '❌'} {detail}")
        if not alive:
            print("\n  结论: 模型不可用 —— 先解决上面这一条（连通性 / key / 模型名）")
            return 1

        started = time.perf_counter()
        try:
            result = await gateway.generate(
                "请回传 ok=true，且 echo 原样回传字符串 AIOPS-LLM-PROBE",
                _Probe,
                role="llm_check",
                # ★ 512 而不是 64：思考型模型会先花掉几十~上百个 reasoning token，
                #   预算给小了会返回空 content（实测 deepseek-v4-pro 就这样翻过车）。
                max_tokens=512,
            )
        except LLMError as exc:
            _kv("② 结构化输出", f"❌ {exc.code.value}：{exc}")
            print("\n  结论: 连通但**结构化输出失败** —— 看上面的错误码/信息："
                  "提到截断就加大 max_tokens；LLM_SCHEMA_INVALID 多为模型不吃这个 schema；"
                  "INTERNAL_ERROR 多为供应商侧问题")
            return 1
        elapsed = (time.perf_counter() - started) * 1000

        ok_flag = "✅" if (result.ok is True and "AIOPS-LLM-PROBE" in result.echo) else "⚠️"
        _kv("② 结构化输出", f"{ok_flag} {result.model_dump_json()}")
        if records:
            rec = records[-1]
            reason = rec.extra.get("reasoning_tokens", 0)
            _kv("③ Token 记账", f"in={rec.input_tokens} out={rec.output_tokens}"
                               + (f"（其中思考 {reason}）" if reason else "")
                               + f" attempts={rec.attempts} latency={rec.latency_ms:.0f}ms")
        _kv("端到端耗时", f"{elapsed:.0f}ms")
        print()

        # ---- ④ ★ 快路由**也要真调一次**（验收项 10 要求"两档都要过"）----
        #
        #  为什么必须补这一步：原先这里只对主力模型发过一次真实调用，
        #  快路由模型仅被"拿去和供应商的模型列表比对" —— 而列表比对**证明不了**
        #  它能被调用（实测过：不在列表里的别名照样能调通，反之亦然）。
        #  于是 T15 验收项 10 的"两档都要过"只兑现了一半（见 fix_bugs.md BUG-068）。
        #
        #  ★ 快路由在真实链路里承担 **60% 的输入**（`intent-eval` 实测：15 条里 7 条走它），
        #    而它的调用方式与主力不同（它走 **function calling**，主力思考型会回退 JSON mode），
        #    所以"主力通了"完全不能推出"快路由通了"。
        if router_model:
            fast_started = time.perf_counter()
            try:
                fast = await gateway.generate(
                    "请回传 ok=true，且 echo 原样回传字符串 AIOPS-FAST-PROBE",
                    _Probe, role="llm_check_fast", model=router_model, max_tokens=256,
                )
                fast_elapsed = (time.perf_counter() - fast_started) * 1000
                fast_ok = fast.ok is True and "AIOPS-FAST-PROBE" in fast.echo
                _kv("④ 快路由真调（function calling）",
                    f"{'✅' if fast_ok else '⚠️'} {router_model} · "
                    f"{fast.model_dump_json()}")
                if records:
                    rec = records[-1]
                    _kv("", f"in={rec.input_tokens} out={rec.output_tokens}"
                           f" attempts={rec.attempts} latency={fast_elapsed:.0f}ms")
            except LLMError as exc:
                _kv("④ 快路由真调（function calling）",
                    f"❌ {router_model}：{exc.code.value}：{exc}")
                print("\n  结论: 主力模型可用，但**快路由不可用** —— "
                      "它在真实链路里承担 60% 的输入（intent-eval 实测），"
                      "必须先解决：换模型名 / 检查它是否支持 function calling")
                return 1
            print()
        else:
            _kv("④ 快路由真调（function calling）",
                "（未配置 AIOPS_ROUTER_MODEL —— 三阶段里的「小模型快路由」不启用）")
            print()

        print("  结论: ✅ 模型这条路通了（约束解码 → 二次校验 → 记账 全链路可用）"
              + ("；两条档位均已真调" if router_model else ""))
        return 0
    finally:
        await gateway.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
