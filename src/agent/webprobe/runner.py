"""Bounded async load; actual request results, no retries or redirect following."""
import asyncio, hashlib, hmac, time
from datetime import datetime, timezone
import httpx
from ..observed.attempts import ACTIVE
from .targets import resolve

def now():return datetime.now(timezone.utc).isoformat()


async def receipt(result):
    """Physical request intent/result survives interruption of the whole batch."""
    from ..execution.runtime import CURRENT,STEP
    execution,step=CURRENT.get(),STEP.get()
    if execution is None or step is None:return
    key,row=step
    async with execution.lock:
        records=row.setdefault('http_requests',{})
        records[result['request_id']]=dict(result)
        await execution.save((key,row))


async def run(params, transport=None):
    if params.get('target_kind','business')=='sdk':
        from .sdk import run as check_sdk
        return await check_sdk(params,transport=transport)
    row,endpoint,url=await resolve(params)
    if endpoint['policy']['effect']=='write':
        from ..execution.runtime import CURRENT
        if CURRENT.get() is None:raise PermissionError('写请求必须先进入持久执行台账')
    call=ACTIVE.get()
    if call:
        call.transport='business_probe';call.interface=endpoint['id']
        call.target={k:row[k] for k in ('tenant','observed_id','service','env','cluster','instance','boot_id')}
    tick=time.monotonic();started=now();results=[];stop='request_limit';sent=0
    timeout=httpx.Timeout(params['timeout_sec']);run_id=params['run_id']
    async with httpx.AsyncClient(timeout=timeout,follow_redirects=False,trust_env=False,transport=transport,
        limits=httpx.Limits(max_connections=params['concurrency'],max_keepalive_connections=params['concurrency'])) as client:
        sem=asyncio.Semaphore(params['concurrency'])
        async def one(number):
            nonlocal sent
            async with sem:
                # Recheck revocation immediately before each physical request.
                _,fresh,fresh_url=await resolve(params)
                if fresh!=endpoint or fresh_url!=url:raise PermissionError('registered target changed')
                request_id=hashlib.sha256((run_id+':'+str(number)).encode()).hexdigest()[:32]
                begin=time.monotonic();at=now();sent+=1
                result=dict(number=number,request_id=request_id,started_at=at,origin='active_probe',status=None,error=None)
                result['state']='in_flight'
                await receipt(result)  # No physical request before its durable intent.
                try:
                    from ..observed.context import current
                    from ..observed.credentials import Credentials,pull_token
                    credentials=getattr(current().catalog,'credentials',None) or Credentials()
                    identity={k:row[k] for k in ('tenant','observed_id','service','env','cluster','instance','boot_id')}
                    token=pull_token(credentials.token(row['grant_id']),identity)
                    proof=hmac.new(token.encode(),(run_id+':'+request_id).encode(),hashlib.sha256).hexdigest()
                    async with client.stream(endpoint['method'],url,params=params['query'],json=params.get('body'),
                        headers={'X-AIOps-Probe-Id':run_id,'X-AIOps-Request-Id':request_id,'X-AIOps-Probe-Proof':proof}) as response:
                        result['status']=response.status_code;size=0;digest=hashlib.sha256()
                        async for chunk in response.aiter_bytes():
                            part=chunk[:max(0,16384-size)];digest.update(part);size+=len(part)
                            if size>=16384:break
                        result.update(response_bytes=size,response_prefix_hash=digest.hexdigest(),response_capped=size>=16384)
                except httpx.TransportError as exc:
                    result['error']=type(exc).__name__
                except asyncio.CancelledError:
                    result.update(error='CancelledError',state='unknown')
                    raise
                finally:
                    result.update(elapsed_ms=round((time.monotonic()-begin)*1000,3),finished_at=now())
                    result['delivery_state']='response_received' if result['status'] is not None else 'server_result_unknown'
                    if result['state']!='unknown':result['state']='completed'
                    results.append(result)
                    await receipt(result)
        jobs=set()
        try:
            for n in range(params['max_requests']):
                deadline=tick+params['duration_sec']
                due=tick+n/params['rate_per_sec']
                if due>=deadline:stop='duration_limit';break
                await asyncio.sleep(max(0,due-time.monotonic()))
                if time.monotonic()>=deadline:stop='duration_limit';break
                # Bound pending work too; do not accumulate a queue behind concurrency.
                while len(jobs)>=params['concurrency']:
                    done,jobs=await asyncio.wait(jobs,return_when=asyncio.FIRST_COMPLETED)
                    for j in done:j.result()
                if time.monotonic()>=deadline:stop='duration_limit';break
                jobs.add(asyncio.create_task(one(n+1)))
            if jobs:await asyncio.gather(*jobs)
        finally:
            for j in jobs:
                if not j.done():j.cancel()
            if jobs:await asyncio.gather(*jobs,return_exceptions=True)
    elapsed=time.monotonic()-tick;ordered=sorted(results,key=lambda r:r['number'])
    latencies=sorted(r['elapsed_ms'] for r in ordered)
    errors=sum(bool(r['error']) or (r['status'] or 0)>=500 or r['status']==429 for r in ordered)
    status_counts={str(code):sum(r['status']==code for r in ordered) for code in {r['status'] for r in ordered} if code is not None}
    return dict(run_id=run_id,origin='active_probe',scope={k:row[k] for k in ('tenant','observed_id','service','env','cluster','instance','boot_id')},
        endpoint_id=endpoint['id'],endpoint_revision=params['endpoint_revision'],method=endpoint['method'],route=endpoint['path'],
        mode=params['mode'],started_at=started,finished_at=now(),sent=sent,completed=len(ordered),errors=errors,
        http_status_counts=status_counts,non_success_responses=sum(r['status'] is not None and not 200<=r['status']<300 for r in ordered),
        elapsed_ms=round(elapsed*1000,3),achieved_qps=round(len(ordered)/max(.001,elapsed),3),
        p95_ms=latencies[min(len(latencies)-1,int(len(latencies)*.95))] if latencies else None,
        stop_reason=stop,results=ordered,
        limitations='主动生成的流量；实际QPS为本次客户端完成率；单实例样本不证明全量健康/系统容量，超时不等于业务宕机')
