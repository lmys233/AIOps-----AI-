"""流量发生器（评测与故障复现用）。

★ 为什么要有它：连接池饱和的定义是"同时有请求在等连接"，串行发请求永远不会让
  ``waiting > 0``；所以需要一个能**并发打流、并同时采样 `/pool-stats`** 的工具 ——
  只打流量不采样，就抓不到 ``waiting`` 的峰值（它随请求结束立刻回落）。

★ 只用标准库（urllib + ThreadPoolExecutor）：它可能跑在还没建好 venv 的宿主机上，
  引第三方库会给"造流量"多加一个安装前置条件。

用法::

    # 默认打网关，200 个请求、并发 40
    python -m observed.loadgen --target gateway --requests 200 --concurrency 40

    # 直接压 order（看连接池），并打印池状态峰值
    python -m observed.loadgen --target order --requests 300 --concurrency 60

    # 只用 GET 对照端点（不打数据库）—— 用于证明"库侧问题不影响对照端点"
    python -m observed.loadgen --target order --operation get --requests 100
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Final

from observed.biz import api_contract
from observed.biz.settings import SERVICE_PORTS

__all__ = ["DEFAULT_TARGETS", "OPERATIONS", "run_load", "LoadReport"]

# 默认目标地址。端口从端口契约派生，本文件不写字面量端口。
DEFAULT_TARGETS: dict[str, str] = {
    service: f"http://127.0.0.1:{port}" for service, port in SERVICE_PORTS.items()
}

# 这个文件用标准库发 HTTP，端口表是本包（业务侧）里唯一的一份 ——
# 派生之后仍然对一次账：两边不一致说明端口契约换了名或换了值，直接炸掉。
assert set(DEFAULT_TARGETS) == set(SERVICE_PORTS), "压测器目标表与端口契约不一致"

#: service → operation → 契约表里的路径。
#: 这里只登记"打哪个端点"，方法一律到 `api_contract` 里去查。
_OPERATIONS_BY_SERVICE: Final[dict[str, dict[str, str]]] = {
    "gateway": {"create": "/api/orders", "get": "/api/orders/{order_id}"},
    "order": {"create": "/orders", "get": "/orders/{order_id}", "pool": "/pool-stats"},
    "inventory": {"check": "/check", "reserve": "/reserve"},
    "payment": {"pay": "/pay", "refund": "/refund-policy"},
}

#: operation → 能打这个端点的服务（同名 operation 的归属，也用于提示）
OPERATIONS: Final[dict[str, str]] = {
    operation: service
    for service, paths in _OPERATIONS_BY_SERVICE.items()
    for operation in paths
}


@dataclass
class LoadReport:
    """一次压测的结果。"""

    total: int = 0
    ok: int = 0
    failed: int = 0
    status_hist: dict[str, int] = field(default_factory=dict)
    client_ms: list[float] = field(default_factory=list)
    server_ms: list[float] = field(default_factory=list)
    gap_ms: list[float] = field(default_factory=list)
    max_waiting: int = 0
    pool_samples: int = 0
    error_types: dict[str, int] = field(default_factory=dict)

    def _pct(self, values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return round(ordered[idx], 3)

    def summary(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "success_rate": round(self.ok / self.total, 4) if self.total else 0.0,
            "status": dict(sorted(self.status_hist.items())),
            "client_ms": {
                "p50": self._pct(self.client_ms, 0.50),
                "p95": self._pct(self.client_ms, 0.95),
                "p99": self._pct(self.client_ms, 0.99),
                "max": round(max(self.client_ms), 3) if self.client_ms else 0.0,
            },
            "server_self_reported_ms": {
                "p50": self._pct(self.server_ms, 0.50),
                "p95": self._pct(self.server_ms, 0.95),
                "p99": self._pct(self.server_ms, 0.99),
            },
            "client_server_gap_ms": {
                "p50": self._pct(self.gap_ms, 0.50),
                "p95": self._pct(self.gap_ms, 0.95),
                "p99": self._pct(self.gap_ms, 0.99),
            },
            "db_pool_waiting_max": self.max_waiting,
            "db_pool_samples": self.pool_samples,
            "error_types": dict(sorted(self.error_types.items())),
        }


def _request(
    method: str, url: str, params: dict[str, Any] | None, timeout: float
) -> tuple[int, dict[str, Any] | None, float]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            elapsed = (time.perf_counter() - started) * 1000
            try:
                body = json.loads(raw) if raw.strip() else None
            except json.JSONDecodeError:
                body = None
            return response.status, body, elapsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        elapsed = (time.perf_counter() - started) * 1000
        try:
            body = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            body = None
        return exc.code, body, elapsed
    except Exception:  # noqa: BLE001 —— 网络错误也要计入失败率
        return 0, None, (time.perf_counter() - started) * 1000


def _endpoint(target: str, operation: str) -> tuple[str, str]:
    """把 ``operation`` 解析成 ``(method, path)``，两者都来自契约表。

    打不了这个端点（目标上没有它）就报错退出，绝不去猜一个方法或路径 ——
    方法写错会被上游包装成"下游宕机"，那正是要避免的误判。
    """
    if operation not in OPERATIONS:
        raise SystemExit(f"未知 operation={operation!r}，可选：{sorted(OPERATIONS)}")
    owner = OPERATIONS[operation]
    declared = _OPERATIONS_BY_SERVICE.get(target, {}).get(
        operation, _OPERATIONS_BY_SERVICE[owner][operation]
    )
    try:
        method = api_contract.method_for(target, declared)
    except KeyError as exc:
        raise SystemExit(
            f"目标 {target!r} 上没有 {operation!r} 端点（它属于 {owner}）：{exc}"
        ) from exc
    return method, api_contract.path_for(target, declared)


def _build_operation(
    target: str, base: str, operation: str, *, sku: str, qty: int, amount: float
) -> Callable[[int], tuple[str, str, dict[str, Any] | None]]:
    """返回 ``(method, url, params)`` 的构造器。

    方法只从契约表取（查不到即报错）；这里拼的是 URL 与 query 参数。
    ``i`` 是每个构造器都收的序号参数，只有 create/get/pay/refund/check 会用到。
    """
    method, declared = _endpoint(target, operation)
    # 只有真带占位符的路径才传 path_params：render_path 会拒绝多余的参数
    needs_id = "{order_id}" in declared

    def once(i: int) -> tuple[str, str, dict[str, Any] | None]:
        # 用**不存在**的 id 渲染：对照端点会返回 404，但**不碰数据库** ——
        # 因此它的延迟可以当作"服务自身处理开销"的基线。
        path = api_contract.render_path(
            declared, {"order_id": f"loadgen_{i}"} if needs_id else None
        )
        body: dict[str, Any] | None = None
        if operation == "create":
            body = {"sku": sku, "qty": qty}
        elif operation == "pay":
            body = {"order_id": f"loadgen_{i}", "amount": amount}
        elif operation == "refund":
            body = {"order_id": f"loadgen_{i}"}
        elif operation == "check":
            body = {"sku": sku, "qty": qty}
        return method, base + path, body

    return once


def run_load(
    base: str,
    *,
    target: str = "order",
    operation: str = "create",
    requests: int = 200,
    concurrency: int = 40,
    sku: str = "SKU-1001",
    qty: int = 1,
    amount: float = 19.9,
    timeout: float = 30.0,
    sample_pool: bool = True,
    progress: bool = True,
) -> LoadReport:
    """并发打流，并在过程中采样连接池状态。"""
    report = LoadReport()
    builder = _build_operation(target, base, operation, sku=sku, qty=qty, amount=amount)
    pool_url = f"{base}/pool-stats"
    stop = False
    # 采样线程与 N 个工作线程都会写 report：`+=` 不是原子操作，
    # 并发累加会丢更新（计数偏小、峰值偏低），而"证池饱和"正靠这个峰值。
    lock = threading.Lock()

    def sampler() -> None:
        while not stop:
            status, body, _ = _request("GET", pool_url, None, 5.0)
            if status == 200 and isinstance(body, dict):
                waiting = body.get("waiting")
                with lock:
                    report.pool_samples += 1
                    if isinstance(waiting, int) and waiting > report.max_waiting:
                        report.max_waiting = waiting
            time.sleep(0.02)

    def worker(i: int) -> None:
        method, url, params = builder(i)
        status, body, client_ms = _request(method, url, params, timeout)
        server_ms = body.get("_server_elapsed_ms") if isinstance(body, dict) else None
        with lock:
            report.status_hist[str(status)] = report.status_hist.get(str(status), 0) + 1
            report.client_ms.append(client_ms)
            if 200 <= status < 300:
                report.ok += 1
            else:
                report.failed += 1
                if isinstance(body, dict) and body.get("error_type"):
                    et = str(body["error_type"])
                    report.error_types[et] = report.error_types.get(et, 0) + 1
            if isinstance(server_ms, (int, float)):
                report.server_ms.append(float(server_ms))
                report.gap_ms.append(round(client_ms - float(server_ms), 3))

    report.total = requests
    sampler_thread: ThreadPoolExecutor | None = None
    sampler_future = None
    if sample_pool:
        sampler_thread = ThreadPoolExecutor(max_workers=1)
        sampler_future = sampler_thread.submit(sampler)

    try:
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [pool.submit(worker, i) for i in range(requests)]
            for _ in as_completed(futures):
                done += 1
                if progress and done % max(1, requests // 10) == 0:
                    print(f"    ... {done}/{requests}", file=sys.stderr)
    finally:
        stop = True
        if sampler_thread is not None:
            if sampler_future is not None:
                sampler_future.result()
            sampler_thread.shutdown(wait=True)

    return report


def _parse_targets(pairs: list[str]) -> dict[str, str]:
    """默认目标表 + 命令行 ``NAME=URL`` 覆盖（新名字也可加进来）。"""
    targets = dict(DEFAULT_TARGETS)
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--set 需要 name=url 形式，收到 {pair!r}")
        name, url = pair.split("=", 1)
        targets[name.strip()] = url.strip().rstrip("/")
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AIOps 被观测系统 · 并发流量发生器（纯标准库）"
    )
    # 不写 choices：--set 可以新增服务名，choices 是解析参数时就求值的，
    # 会把新增的名字挡在外面（永远选不到）。
    parser.add_argument("--target", default="order",
                        help="打哪个服务（默认 order；可用 --set 新增）")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="NAME=URL", help="覆盖或新增服务地址，可重复")
    parser.add_argument("--operation", default="create",
                        choices=sorted(OPERATIONS),
                        help="打哪个端点（默认 create=下单）")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--sku", default="SKU-1001")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--amount", type=float, default=19.9)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--no-sample-pool", action="store_true",
                        help="不采样 /pool-stats（目标无该端点时使用）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    targets = _parse_targets(args.overrides)
    if args.target not in targets:
        raise SystemExit(
            f"未知目标 {args.target!r}，可选：{sorted(targets)}"
            f"（新增目标用 --set {args.target}=http://host:port）"
        )
    base = targets[args.target]
    print(f"  目标      : {args.target} → {base}")
    print(f"  操作      : {args.operation}")
    print(f"  请求数    : {args.requests}  并发: {args.concurrency}")
    print("  进行中 ...", file=sys.stderr)

    report = run_load(
        base,
        target=args.target,
        operation=args.operation,
        requests=args.requests,
        concurrency=args.concurrency,
        sku=args.sku,
        qty=args.qty,
        amount=args.amount,
        timeout=args.timeout,
        sample_pool=not args.no_sample_pool,
    )
    summary = report.summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.json:
        samples = int(summary["db_pool_samples"])
        if summary["db_pool_waiting_max"] > 0:
            print(f"  ★ 观察到连接池等待峰值 = {summary['db_pool_waiting_max']}"
                  f"（共 {samples} 次采样）")
        elif samples == 0:
            # ★ 0 次采样**推不出**"没饱和"：
            #   为了少一次查询而把"根本没测"说成"没问题"，正是本项目最忌讳的结论
            #   （同族口径：SKIP 不等于 PASS、假 PASS 比假 FAIL 危险）。
            print(f"  [!] 一次都没采样到 {args.target} 的 /pool-stats —— "
                  f"**不能**据此判断连接池是否饱和")
            print("      常见原因：目标服务本身没有连接池（gateway 就没有）。")
            print("      要观测池就换目标，例如：.\\run.ps1 loadgen --target order")
        else:
            print(f"  连接池等待始终为 0（共 {samples} 次采样，未出现池饱和）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
