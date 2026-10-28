"""One real HTTP cancellation in a temporary payment replica; archive retained."""
import asyncio
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import httpx
import uvicorn


def prepare(folder):
    from sdk.deployment import apply_profile
    from sdk.config import load_config
    from sdk.launcher import apply_env_bridge,import_app
    from sdk.state import Runtime,set_runtime
    from sdk.app import ObservedApp
    from sdk.diagnostic_export import start_exporter
    apply_profile(ROOT/'.run/observed-sdk-deployment.json','payment')
    os.environ.update(AIOPS_SDK_INSTANCE=folder.name,AIOPS_SDK_BOOT_ID=uuid4().hex,
        AIOPS_SDK_DIAGNOSTICS='on',AIOPS_SDK_SUCCESS_SAMPLE_EVERY='1',
        AIOPS_SDK_DIAGNOSTICS_BUFFER=str(folder/'outbox'),AIOPS_SDK_SNAPSHOT_INTERVAL='0')
    cfg=replace(load_config(ROOT/'src/observed/app.yaml',service='payment'),log_dir=str(folder/'logs'))
    apply_env_bridge(cfg)
    runtime=Runtime('payment',config=cfg);set_runtime(runtime)
    assert runtime.diagnostics.identity['env']=='prod'
    assert runtime.diagnostics.identity['tenant']=='default'
    business=import_app(cfg.app_ref)
    route=next(r for r in business.routes if getattr(r,'path',None)=='/refund-policy')
    entered=asyncio.Event();holder={};pause={'once':True}
    async def drill(scope,receive,send):
        if scope.get('type')=='http' and scope.get('path')=='/refund-policy' and pause['once']:
            pause['once']=False;scope['route']=route
            holder['task']=asyncio.current_task();entered.set()
            await asyncio.sleep(10)  # Simulated stalled request entry; no payment SQL.
        return await business(scope,receive,send)
    wrapped=ObservedApp(drill,config=cfg,runtime=runtime)
    exporter=start_exporter(runtime)
    assert exporter is not None,'payment diagnostic exporter not configured'
    return wrapped,runtime,exporter,entered,holder


async def interrupt(wrapped,runtime,entered,holder):
    from sdk.cancellation import observe_cancel_result
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(wrapped,lifespan='off',log_config=None,access_log=False))
    serving=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:break
            await asyncio.sleep(.01)
        assert server.started,'temporary replica did not start'
        async with httpx.AsyncClient(timeout=5) as client:
            pending=asyncio.create_task(client.get(f'http://127.0.0.1:{port}/refund-policy'))
            try:
                await asyncio.wait_for(entered.wait(),3)
                accepted=holder['task'].cancel('operator drill: interrupted payment HTTP handling before response')
                observe_cancel_result(holder['task'],accepted,origin='unknown',source_record=runtime.diagnostics.identity['instance'])
                try:response=await pending;status=response.status_code
                except httpx.HTTPError as error:status=type(error).__name__
                assert accepted
                control=await client.get(f'http://127.0.0.1:{port}/refund-policy')
                assert control.status_code==200 and control.json()['policy_version']
                original=await client.get('http://127.0.0.1:8082/health')
                assert original.status_code==200
            finally:
                if not pending.done():pending.cancel()
                await asyncio.gather(pending,return_exceptions=True)
        row=next(r for r in runtime.diagnostics.events if r['kind']=='http_request' and r['payload'].get('outcome')=='cancelled')
        assert row['payload']['completed'] is False
        return row,{'interrupted_http_status':status,'control_http_status':200,'original_8082_health':200}
    finally:
        server.should_exit=True
        await asyncio.wait_for(serving,5);sock.close()


async def verify(row,folder):
    from agent.common.envfile import load_env_file
    from agent.diagnostics.store import DiagnosticStore
    from agent.diagnostics.reader import DiagnosticReader
    from agent.diagnostics.ingestion import from_env
    from agent.diagnostics.tools import register
    from agent.tools.registry import build_builtin_registry
    from agent.tools.executor import ToolExecutor
    from agent.agents.diagnostic_agents import RuntimeAgent
    from agent.agents.runner import ForensicsRunner
    from agent.board.store import InMemoryBoardStore
    from agent.common.contracts import Slots
    from agent.attribution.v2.facts import build_problems
    load_env_file();store=DiagnosticStore(os.getenv('AIOPS_DIAGNOSTIC_DATABASE_URL') or os.environ['AIOPS_DATABASE_URL'])
    try:
        end=datetime.now(timezone.utc);start=end-timedelta(minutes=30)
        params=dict(service='payment',env='prod',start=start.isoformat(),end=end.isoformat(),request_id=row['correlation']['request_id'])
        registry=build_builtin_registry();register(registry,DiagnosticReader(store,'default',from_env().bindings))
        executor=ToolExecutor(registry=registry)
        for _ in range(15):
            found=await store.query('default','payment',params['start'],datetime.now(timezone.utc).isoformat(),env='prod',request_id=params['request_id'])
            if any(r['event_id']==row['event_id'] for r in found['events']):break
            await asyncio.sleep(.5)
        assert any(r['event_id']==row['event_id'] for r in found['events']),'SDK export did not reach real platform archive'
        result=await executor.call('runtime_agent','request_cancellation_read',params)
        assert result.ok and any(r['event_id']==row['event_id'] for r in result.data['events'])
        assert not result.data['records'],'unknown manual trigger must not be confirmed as client/deadline/shutdown'
        board=InMemoryBoardStore();slots=Slots(service_names=['payment'],env='prod',time_window='30m')
        completed=await ForensicsRunner(executor,board,agents=[RuntimeAgent()]).run(folder.name,slots,tenant_id='default')
        evidence=[e for o in completed.outcomes for e in o.evidence]
        problems=build_problems(evidence,env='prod')
        target=[p for p in problems if p.request_id==params['request_id']]
        assert target and target[0].error_type=='HTTP_REQUEST_CANCELLED'
        return dict(window='30m',named_tool='passed',runtime_agent='passed',confirmed_trigger=False,
            archived_events=len(found['events']),problem_id=target[0].problem_id)
    finally:await store.close()


async def main():
    folder=ROOT/'.run'/('payment-http-drill-'+uuid4().hex[:12]);folder.mkdir()
    before=hashlib.sha256((ROOT/'src/observed/payment/main.py').read_bytes()).hexdigest()
    wrapped,runtime,exporter,entered,holder=prepare(folder)
    try:
        row,checks=await interrupt(wrapped,runtime,entered,holder)
        checks.update(await verify(row,folder))
        assert before==hashlib.sha256((ROOT/'src/observed/payment/main.py').read_bytes()).hexdigest()
        at=datetime.fromisoformat(row['occurred_at']);local=at.astimezone(timezone(timedelta(hours=8)))
        report=dict(service='payment',env='prod',tenant='default',instance=row['identity']['instance'],
            request_id=row['correlation']['request_id'],occurred_at=row['occurred_at'],local_time=local.isoformat(),
            rolling_30m_until=(local+timedelta(minutes=30)).isoformat(),endpoint='GET /refund-policy',
            simulation=True,replica='temporary instance of actual payment ASGI app; original 8082 unchanged',
            event_id=row['event_id'],business_source_unchanged=True,payment_transaction_executed=False,
            checks=checks,artifacts=str(folder))
        (folder/'sdk-events.json').write_text(json.dumps(list(runtime.diagnostics.events),ensure_ascii=False,indent=2),encoding='utf-8')
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (ROOT/'.run/payment-http-interruption-latest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:exporter.close()


if __name__=='__main__':asyncio.run(main())
