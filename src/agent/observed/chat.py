"""Resolve authorized system once per run; both chat and SSE enter this boundary."""
import os,re
from dataclasses import replace
from .context import Scope,use,enabled,current,body_scope,stamp
from ..api.contract_body import empty_contract_body


async def resolve(service,request):
    catalog=getattr(service,'observed_catalog',None)
    if catalog is None:raise ValueError('注册中心未接入，无法确定被观测系统')
    tenant=os.getenv('AIOPS_TENANT','default');oid=request.context.observed_id;parent=None
    target=request.context.dialogue
    iid=(target.task_id if target else '') or next(iter(re.findall(r'\binc_[a-zA-Z0-9]+\b',request.message)),'')
    if iid.startswith('tsk_'):
        from ..adapters.task_runtime import reference_incident
        iid=await reference_incident(service,request,iid)
    if iid:
        parent=await service.get_incident(iid)
        if parent is None or parent.get('session_id')!=request.session_id:raise ValueError('任务不属于当前会话或已不可读')
        old=body_scope(parent)
        if not old:raise ValueError('旧任务没有可验证的系统归属；可查看历史，重新排查需选择系统')
        if oid and oid!=old:raise ValueError('该任务属于另一个系统；请切回原系统，或开始新任务')
        oid=old
    if not oid:
        if hasattr(getattr(getattr(service,'history',None),'store',None),'dsn'):
            from .workspace import store_of
            bound=await store_of(service).one('SELECT observed_id FROM agent_system_sessions WHERE tenant=$1 AND session_id=$2',tenant,request.session_id)
            if bound:oid=bound['observed_id']
    if not oid:
        page=await catalog.page(tenant,limit=50)
        registered=[s for s in page['systems'] if s['registered_at'] and s['active']]
        if len(registered)!=1 or page['has_more']:raise ValueError('请先选择要排查的被观测系统；同名服务不能跨系统匹配')
        oid=registered[0]['observed_id']
    row=await catalog.store.one('SELECT name FROM agent_observed_systems WHERE tenant=$1 AND observed_id=$2 AND active AND registered_at IS NOT NULL',tenant,oid)
    if not row:raise ValueError('系统未注册、已停用或不在当前授权范围')
    if hasattr(getattr(getattr(service,'history',None),'store',None),'dsn'):
        from .workspace import chat_binding
        await chat_binding(service,tenant,oid,request.session_id)
    env=request.context.env or ((parent or {}).get('slots') or {}).get('env')
    if not env:
        envs=await catalog.store.fetch('SELECT DISTINCT env FROM agent_observed_instances WHERE tenant=$1 AND observed_id=$2 AND active',tenant,oid)
        values=[r['env'] for r in envs];env=values[0] if len(values)==1 else 'prod'
    scope=Scope(catalog,tenant,oid,env,request.context.cluster,observed_name=row['name'])
    from ..alerts.context import CURRENT as ALERT_CONTEXT
    alert=ALERT_CONTEXT.get()
    if alert:
        labels=alert['labels']
        if labels.get('observed_id')!=oid or (labels.get('env') or 'prod')!=env:
            raise PermissionError('告警声明与已核实系统范围不一致')
        scope=replace(scope,services=(labels['service'],))
    names=await scope.registered();scope=replace(scope,names=tuple(sorted(names)))
    return scope


async def run(service,request,trace_id,incident_id,on_event):
    if not enabled() and not request.context.observed_id:
        return await service._run_recorded_unscoped(request,trace_id=trace_id,incident_id=incident_id,on_event=on_event)
    try:scope=await resolve(service,request)
    except Exception as exc:
        message=str(exc) if isinstance(exc,(ValueError,PermissionError)) else '注册范围未能核实；本轮停止查询，请核查注册中心'
        body=empty_contract_body(request.session_id,trace_id=trace_id,status='clarify',intent_name='INCIDENT_RCA',write_allowed=False)
        body['turn_id']=request.turn_id;body['report']=message
        body['clarification']={'question':message,'missing_slots':['observed_id'],'plugin_id':'observed_system_selection'}
        body['explanation']['answer']={'source':'observed_scope','summary':message,'details':[],'details_kind':'table'}
        from ..presentation.projection import finish
        finish(body)
        await service._store_incident(body);await service._save_history(request,body,{})
        return body
    with use(scope):
        effective=request.model_copy(update={'context':request.context.model_copy(update={'observed_id':scope.observed_id,'env':scope.env})})
        return await service._run_recorded_unscoped(effective,trace_id=trace_id,incident_id=incident_id,on_event=on_event)
