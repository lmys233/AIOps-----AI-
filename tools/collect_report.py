#!/usr/bin/env python3
"""AIOps 采集结果 · **四通道证据清单**（task.md T14 · `.\run.ps1 collect-report`）。

--------------------------------------------------------------------------------
★ 这个脚本的定位（先说清楚，免得被误用）
--------------------------------------------------------------------------------

**它只负责把证据摆出来，"采集是否成功"由人判断。**

为什么要这样切：如果脚本自己下"采集成功"的结论，就会出现
"脚本说绿了、其实没数据"这类最危险的失败 —— 本项目的口径一贯是
"SKIP 不等于 PASS"、"假 PASS 比假 FAIL 危险"（spec §8.3 P20 / 见 fix_bugs.md）。
所以这里**只做只读查询 + 原样展示**：

| 每一路都要给出 | 为什么 |
|---|---|
| 数据源与传输方式 | 让看的人知道"这条数据是怎么来的"（pull / 读文件 / push） |
| **可复制的原始查询命令** | 结论可以自己复核，不必相信本脚本 |
| 查到了多少（条数 / 流数 / 目标数） | "有没有"是一眼能看的事实 |
| 时间范围 | 与指标拐点、变更时刻对得上才有意义 |
| 1~3 条样例 | 让人能看出字段是否真的对（而不是只看计数） |

★ 唯一的例外是 `--json` 与 `--check`：给**验收脚本**（tests/verify_phase1.py 的 T14）
  用的机器可读出口。那时退出码有意义：
    · 四路全部有数据 → 0
    · 有一路没有数据 → 1（**不是** 0：否则 CI 会把"空"当"通过"）
    · 读不到（存储不可达等）→ 也按"没数据"处理，但**在输出里区分原因**

--------------------------------------------------------------------------------
★ 四路数据分别是"怎么进来"的（这三条传输方式完全不同，是本项目的核心事实）
--------------------------------------------------------------------------------

| 通道 | 传输 | 谁主动 | 落到哪 |
|---|---|---|---|
| 指标 | **pull**：Prometheus 定时抓宿主上的 `/metrics` | Prometheus | Prometheus TSDB |
| 日志 | **读文件**：promtail 读宿主 `.run/logs/*.jsonl` 再推给 Loki | promtail | Loki |
| 链路 | **push**：SDK 把 span 推给 OTel Collector，再由它导出 | 业务进程 | Tempo |
| 变更 | **写库**：SDK 检测到业务参数变化时写 `change_events` | 业务进程 | Postgres |

★ 所以"四路都通"不能只验一条：它们的失败原因彼此独立
  （抓取目标配错 / 挂载目录空 / 端点不可达 / 没有凭据）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / ".run" / "logs"

# ============================================================
#  目标地址（环境变量可覆盖，与 tests/http_client.py 用同一组变量名）
#
#  ★ 一律 127.0.0.1 而不是 localhost：本机 localhost 解析 `::1` 在前，
#    而服务只监听 IPv4 —— 用 localhost 会先被拒一次（fix_bugs.md BUG-033）。
# ============================================================
PROMETHEUS = os.getenv("AIOPS_PROMETHEUS", "http://127.0.0.1:9090")
LOKI = os.getenv("AIOPS_LOKI", "http://127.0.0.1:3100")
TEMPO = os.getenv("AIOPS_TEMPO", "http://127.0.0.1:3200")

# 变更记录：走 TCP 直连（**不**依赖 docker CLI，见 fix_bugs.md BUG-027）
RUNNER_DSN = os.getenv(
    "RUNNER_DATABASE_URL",
    "postgresql://aiops_runner:runner_dev_pw@127.0.0.1:5432/aiops",
)

SERVICES = ("gateway", "order", "payment", "inventory")
TIMEOUT = 8.0
WINDOW_MIN = 30          # 默认看最近 30 分钟

# 四路各自的"有没有数据"判定结果，供 --json / --check 使用
CHANNELS: dict[str, dict[str, Any]] = {}

# --json 模式：**只**输出 JSON（机器可读就该是机器可读的 ——
# 混着人读的段落会让消费方（tests/verify_phase1.py 的 T14）必须做文本切分）
QUIET = False


# ============================================================
#  小工具（纯标准库；报告工具不该有"先装框架"的前置条件）
# ============================================================
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def _http_json(url: str, params: Optional[dict] = None,
               timeout: float = TIMEOUT) -> tuple[int, Any]:
    """GET 一个 JSON 接口。返回 ``(状态码, 解析后的对象)``；连不上返回 ``(0, 错误串)``。"""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _rule(title: str) -> None:
    if QUIET:
        return
    _blank()
    print("─" * 78)
    print(f" {title}")
    print("─" * 78)


def _width(text: str) -> int:
    """按**终端显示宽度**算长度（中日韩字符占两列）。

    ★ 为什么值得写这四行：这是一份给人看的清单，标签里大量中文。
      直接 `f"{label:<22}"` 会按字符数补齐，于是冒号位置参差不齐 ——
      读的人要多花力气对齐眼睛，而"证据清单"的全部价值就在于好读。
    """
    import unicodedata  # noqa: PLC0415

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _kv(label: str, value: Any, width: int = 22) -> None:
    if QUIET:
        return
    print(f"  {label}{' ' * max(1, width - _width(label))}: {value}")


def _note(text: str) -> None:
    if QUIET:
        return
    print(f"      {text}")


def _blank() -> None:
    """空行也要受 `--json` 约束（否则 JSON 前面会多出几个空行）。"""
    if QUIET:
        return
    print()


def _local_line_count(service: str) -> int:
    """本地 JSONL 的行数 —— 日志通道的**源头参照物**。

    ★ 为什么值得打印：promtail 采的就是这个目录。
      本地有、Loki 没有 → 采集管道断了；
      本地有、Loki 有但按 `{service=...}` 查不到 → service 标签没生效
      （BUG-039 就是这么暴露出来的：`service` 被打成了 `*`，
       而"查不到"看起来像"没有错误日志"）。
    """
    path = LOG_DIR / f"{service}.jsonl"
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return -1


# ============================================================
#  ① 指标（Prometheus · pull）
# ============================================================
def collect_metrics(start: datetime, end: datetime, minutes: int) -> dict[str, Any]:
    _rule("① 指标（Prometheus · **pull**：Prometheus 抓宿主上的 /metrics）")
    _kv("数据源", PROMETHEUS)

    def q(expr: str) -> Any:
        status, body = _http_json(f"{PROMETHEUS}/api/v1/query", {"query": expr})
        if status != 200 or not isinstance(body, dict):
            return None
        return (body.get("data") or {}).get("result") or []

    _kv("原始查询", f"{PROMETHEUS}/api/v1/query?query="
                    f"{urllib.parse.quote('up{job=\"aiops-services\"}')}")
    _blank()

    targets = q('up{job="aiops-services"}')
    if targets is None:
        _note("✗ Prometheus 不可达或查询失败 —— 本路无法判定")
        return {"ok": False, "reason": "Prometheus 不可达", "targets": 0}

    up = [r["metric"].get("service", "?") for r in targets
          if (r.get("value") or [None, "0"])[1] == "1"]
    _kv("抓取目标", f"{len(up)}/{len(targets)} 个 up=1：{sorted(up)}")

    # 请求总量 + 时间范围（query_range 才能给出"时间范围"）
    rng = _http_json(
        f"{PROMETHEUS}/api/v1/query_range",
        {"query": "sum(http_request_duration_seconds_count)",
         "start": start.timestamp(), "end": end.timestamp(), "step": "30s"},
    )
    series = ((rng[1] or {}).get("data") or {}).get("result") if rng[0] == 200 else None
    total_series = len(series or [])
    if series:
        values = series[0].get("values") or []
        first = float(values[0][1]) if values else 0.0
        last = float(values[-1][1]) if values else 0.0
        _kv("HTTP 请求计数", f"窗口内 {first:.0f} → {last:.0f}（增量 {last - first:.0f}）")
        _kv("时间范围", f"{_iso(start)} ~ {_iso(end)}（{minutes} 分钟，步长 30s）")
    else:
        _kv("HTTP 请求计数", "查不到 sum(http_request_duration_seconds_count)")

    pool = q("db_pool_waiting_connections")
    if pool:
        shown = ", ".join(
            f"{r['metric'].get('service', '?')}={(r.get('value') or ['', '?'])[1]}"
            for r in pool
        )
        _kv("连接池等待", shown + "   ← §8.5 硬性约束 2：没有池的服务也必须报 0")

    ok = bool(up)
    CHANNELS["metrics"] = {"ok": ok, "targets_up": len(up), "series": total_series}
    return CHANNELS["metrics"]


# ============================================================
#  ② 日志（Loki · 读文件后推送）
# ============================================================
def collect_logs(start: datetime, end: datetime) -> dict[str, Any]:
    _rule("② 日志（Loki · **读文件**：promtail 读 .run/logs/*.jsonl 再推给 Loki）")
    _kv("数据源", LOKI)
    _kv("采集目录", f"{LOG_DIR}  →  容器内 /var/log/observed")

    status, labels = _http_json(f"{LOKI}/loki/api/v1/label/service/values")
    values = labels.get("data") if isinstance(labels, dict) else None
    _kv("service 标签取值", values if values is not None else f"查询失败（HTTP {status}）")
    _blank()

    per_service: dict[str, dict[str, Any]] = {}
    total_lines = 0
    for service in SERVICES:
        local = _local_line_count(service)
        status, body = _http_json(
            f"{LOKI}/loki/api/v1/query_range",
            {"query": f'{{service="{service}"}}', "limit": 3,
             "start": f"{start.timestamp():.0f}", "end": f"{end.timestamp():.0f}"},
        )
        streams = ((body or {}).get("data") or {}).get("result") if status == 200 else None
        if streams is None:
            _kv(f"  {service}", f"✗ 查询失败（HTTP {status}）")
            per_service[service] = {"ok": False, "loki_lines": 0, "local_lines": local}
            continue
        lines = sum(len(st.get("values") or []) for st in streams)
        total_lines += lines
        flag = "✓" if lines else "✗"
        _kv(f"  {service}", f"{flag} Loki {lines} 条 / 本地 JSONL {local} 行 "
                           f"（{len(streams)} 条流）")
        for st in streams[:1]:
            for _, line in (st.get("values") or [])[:1]:
                _note(f"样例：{line[:150]}")
        per_service[service] = {"ok": lines > 0, "loki_lines": lines,
                                "local_lines": local, "streams": len(streams)}

    _kv("原始命令", f"{LOKI}/loki/api/v1/query_range?query="
                    f"{urllib.parse.quote('{service=\"order\"}')}&limit=3")
    _kv("时间范围", f"{_iso(start)} ~ {_iso(end)}")

    # ★ 结论只报事实，不下"采集成功"的判断；但把**两者不一致**的形态点出来，
    #   否则看的人会把"Loki 查不到"读成"这段时间没有日志"（spec §8.3 P20）。
    local_any = any(v.get("local_lines", 0) > 0 for v in per_service.values())
    if local_any and total_lines == 0:
        _blank()
        _note("⚠ 本地 JSONL 里有日志，Loki 却一条都查不到 —— 采集管道或 service 标签有问题")
        _note("  （不是「这段时间没有日志」。先看上面的 service 标签取值对不对）")
        _note("  排查顺序：")
        _note("    ① 容器里看到的是不是新配置（单文件 bind mount 可能仍指向旧文件）：")
        _note("       docker exec aiops-promtail grep -n __path__ "
              "/etc/promtail/promtail-config.yml")
        _note("    ② 让 promtail 从头重读（positions 存在容器内，**重建**容器即清空）：")
        _note("       docker compose -f deploy/docker-compose.yml "
              "up -d --force-recreate promtail")
        _note("       · 只 `restart` 往往不够：它不重建挂载，也不清 positions")
        _note("       · 而且**成功的业务请求不写日志**（access_log 关着），"
              "所以「再造点流量」不会产生新日志；重启业务或打一个 404 才有")

    ok = total_lines > 0
    CHANNELS["logs"] = {"ok": ok, "lines": total_lines, "per_service": per_service}
    return CHANNELS["logs"]


# ============================================================
#  ③ 链路（Tempo · push：SDK → Collector → Tempo）
# ============================================================
def collect_traces(start: datetime, end: datetime) -> dict[str, Any]:
    _rule("③ 链路（Tempo · **push**：SDK → OTel Collector → Tempo）")
    _kv("数据源", TEMPO)
    status, body = _http_json(f"{TEMPO}/api/search",
                              {"limit": 5, "start": int(start.timestamp()),
                               "end": int(end.timestamp())})
    traces = (body or {}).get("traces") if isinstance(body, dict) else None
    if status != 200 or traces is None:
        _note(f"✗ 查询失败（HTTP {status}）：{str(body)[:120]}")
        CHANNELS["traces"] = {"ok": False, "count": 0}
        return CHANNELS["traces"]

    _kv("trace 条数", len(traces))
    _kv("原始命令", f"{TEMPO}/api/search?limit=5")
    for tr in traces[:3]:
        _note(f"{tr.get('traceID')}  root={tr.get('rootServiceName')}"
              f"/{tr.get('rootTraceName')}  {tr.get('durationMs')}ms")

    # ★ 挑一条把 span 展开：这一路的价值在于"**跨服务**串起来"，
    #   只看 trace 条数看不出到底串了几个服务。
    if traces:
        tid = traces[0].get("traceID")
        st, tb = _http_json(f"{TEMPO}/api/traces/{tid}")
        batches = (tb or {}).get("batches") if isinstance(tb, dict) else None
        if st == 200 and batches:
            _blank()
            _note(f"展开 {tid} 的 span（证明是**跨服务**的一条链）：")
            for batch in batches:
                attrs = {a.get("key"): a.get("value", {}).get("stringValue")
                         for a in ((batch.get("resource") or {}).get("attributes") or [])}
                for scope in (batch.get("scopeSpans") or []):
                    for span in (scope.get("spans") or []):
                        _note(f"  · {attrs.get('service.name', '?'):<10} {span.get('name')}"
                              f"  ({int(span.get('duration', {}).get('nanos', 0) or 0) / 1e6:.1f}ms)")

    CHANNELS["traces"] = {"ok": bool(traces), "count": len(traces)}
    return CHANNELS["traces"]


# ============================================================
#  ④ 变更（Postgres change_events · 业务进程写库）
# ============================================================
async def _fetch_changes(limit: int) -> list[dict[str, Any]]:
    import asyncpg  # 与业务用同一个驱动（TCP 直连，不依赖 docker CLI）

    conn = await asyncpg.connect(RUNNER_DSN, timeout=6)
    try:
        rows = await conn.fetch(
            "SELECT change_id, changed_at, entity_id, change_type, operator, "
            "       source, diff, created_at "
            "  FROM change_events ORDER BY changed_at DESC LIMIT $1",
            limit,
        )
    finally:
        await conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("diff"), str):
            try:
                item["diff"] = json.loads(item["diff"])
            except ValueError:
                pass
        out.append(item)
    return out


def collect_changes(limit: int = 5) -> dict[str, Any]:
    _rule("④ 变更（Postgres `change_events` · 业务进程**写库**）")
    _kv("数据源", RUNNER_DSN.replace("runner_dev_pw", "***"))
    _kv("原始命令", "SELECT change_id, changed_at, entity_id, change_type, operator, "
                    "source, diff FROM change_events ORDER BY changed_at DESC LIMIT 5")
    try:
        rows = asyncio.run(_fetch_changes(limit))
    except Exception as exc:  # noqa: BLE001 —— 报告工具绝不因一路失败而中断
        _note(f"✗ 读不到 change_events：{type(exc).__name__}: {exc}")
        _note("  （本路缺数据的后果：只能描述现象，无法解释「为什么突然变了」"
              "—— spec §8.4 第 6 条）")
        CHANNELS["changes"] = {"ok": False, "count": 0, "reason": f"{type(exc).__name__}"}
        return CHANNELS["changes"]

    _kv("记录条数", len(rows))
    if not rows:
        _note("（空表：说明本次运行期间**业务参数没有变过** —— 首次启动只落快照，不写变更）")
    for row in rows:
        diff = row.get("diff") or {}
        _note(f"{row['changed_at']}  {row.get('entity_id')}  {row.get('change_type')}"
              f"  改了什么: {diff.get('field')} {diff.get('old_value')} → {diff.get('new_value')}")
    CHANNELS["changes"] = {"ok": bool(rows), "count": len(rows)}
    return CHANNELS["changes"]


# ============================================================
#  人工核对清单（脚本不替人下结论）
# ============================================================
CHECKLIST = """
══════════════════════════════════════════════════════════════════════════════
  人工核对清单 —— 脚本只**摆证据**，"采集成功"由你判断
══════════════════════════════════════════════════════════════════════════════
  [ ] 指标：4 个抓取目标全部 up=1，且 HTTP 请求计数在窗口内有增长
  [ ] 日志：Loki 里**每个用到的服务**都能按 {service="..."} 查到（不是只有部分）
      · 本地 JSONL 行数与 Loki 条数是同一个量级（差太多说明采集有丢失）
  [ ] 链路：trace 的 root 服务是 gateway，展开后能看到 order/inventory/payment
      （只有单服务 span 说明埋点没串起来）
  [ ] 变更：改了业务参数（例如 app.yaml 里 db.pool.size）后重启 → 这里应出现一条新记录
      · 记录时间必须**早于**你观察到的指标拐点，否则因果方向说不通
  [ ] 互跳：日志样例里的 trace_id 能在链路那一段的一个 traceID 里找到
      · ★ 前提：该进程**启动时** Collector 可达。否则按 BUG-024 的取舍它不导出 span，
        日志里照样有 trace_id（span 上下文是真的），但 Tempo 里没有 —— 见 BUG-041
  [ ] 零代码改动：`git status --porcelain src/observed` 里没有任何 .py 改动
══════════════════════════════════════════════════════════════════════════════
"""


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="collect-report",
        description="AIOps 采集结果 · 四通道证据清单（只读；是否采集成功由人判断）",
    )
    parser.add_argument("--minutes", type=int, default=WINDOW_MIN,
                        help=f"回看多少分钟（默认 {WINDOW_MIN}）")
    parser.add_argument("--json", action="store_true",
                        help="只输出机器可读的汇总（给验收脚本用）")
    parser.add_argument("--expect-change", action="store_true",
                        help="要求变更通道也有记录（你刚改过业务参数并重启时才用它）")
    args = parser.parse_args(argv)

    global QUIET
    QUIET = bool(args.json)

    end = _now()
    start = end - timedelta(minutes=args.minutes)

    if not args.json:
        print()
        print("=" * 78)
        print("  AIOps 采集结果 · 四通道证据清单（task.md T14）")
        print("=" * 78)
        _kv("生成时间", _iso(end))
        _kv("回看窗口", f"{args.minutes} 分钟（{_iso(start)} ~ {_iso(end)}）")
        _kv("用途", "把四路数据各取样摆出来 —— **是否采集成功由人判断**")
        _kv("副作用", "无：全部是只读查询")

    collect_metrics(start, end, args.minutes)
    collect_logs(start, end)
    collect_traces(start, end)
    collect_changes()

    if args.json:
        print(json.dumps({"channels": CHANNELS, "window_minutes": args.minutes},
                         ensure_ascii=False, indent=2))
    else:
        print(CHECKLIST)
        counts = {k: v.get("count", v.get("lines", v.get("targets_up", 0)))
                  for k, v in CHANNELS.items()}
        _kv("机器可读汇总", counts)

    # ★ 退出码的语义（可被脚本消费）：
    #   指标 / 日志 / 链路 是**连续信号** —— 有流量却没有数据，就是采集有问题 → 1
    #   变更 是**事件**：窗口内没改过参数就是空表，空是正常的 → 除非显式 --expect-change
    required = ["metrics", "logs", "traces"]
    if args.expect_change:
        required.append("changes")
    empty = [name for name in required if not CHANNELS.get(name, {}).get("ok")]
    if empty:
        _note(f"[汇总] 这些通道**没有数据**：{empty}"
              f"（退出码 1 —— 但「采集是否成功」仍请按上面的清单人工确认）")
        return 1
    if not args.expect_change and not CHANNELS.get("changes", {}).get("ok"):
        _note("[汇总] 变更通道为空 —— 本次窗口内业务参数没有变过（这不代表异常）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
