"""Single entry shared by synchronous chat and SSE, before query weaving."""
from copy import deepcopy
import os
import re
import time
from .contracts import snapshot,metadata,cost,Decision
from .state import candidates,bind
from .routing import quick,classify,UNSAFE,UNRELATED
from .limits import budget_for
from .gaps import gaps,explain
from .submissions import receive
from .view import response,compatible
from .progress import verify,plans
from .candidate_review import prepare,retain,rows as candidate_rows
from .relations import review as relation_rows
from .investigation_summary import summarize
from .pending import resume,ask_target
from ..common.steps import emit_step
from ..observed.names import service_names_of

async def handle(service,request,trace_id,usage,on_event=None,incident_id=None):
    if os.getenv('AIOPS_TASK_DIALOGUE','on').lower() in ('off','0','false'):return None
    hint=quick(request.message);target=request.context.dialogue
    if hint and hint.action=='new_task':return None
    if hint is None and (len(request.message)>160 or UNSAFE.search(request.message) or UNRELATED.search(request.message)):return None
    explicit=(target.task_id if target else '') or next(iter(re.findall(r'\binc_[a-zA-Z0-9]+\b',request.message)),'')
    try:rows=await candidates(service,request.session_id,explicit)
    except Exception:
        if not hint:return None
        return clarification(request,trace_id,'历史档案不可读，无法安全接续；请指定要继续的任务。',budget_for(service,request))
    from ..intent.slots import extract_slots
    mentioned=extract_slots(request.message,config=getattr(service.router,'slot_config',None)).slots.service_names or []
    available={p['service'] for b in rows for p in snapshot(b).get('problems',[])}
    if mentioned and not set(mentioned)<=available and not explicit and not target:return None
    if re.search(r'(切换|改查|换环境).{0,10}(prod|dev|staging)',request.message,re.I):return None
    if not rows and hint and hint.action in ('request_plan','submit_evidence','verify_target'):return None
    strict=max(rows,key=lambda b:metadata(b).get('task_used',cost(b)).get('tokens',0)) if rows else None
    budget=budget_for(service,request,strict)
    decision=await classify(service,request,rows,budget)
    if not decision or decision.action=='new_task':return None
    if not rows:return clarification(request,trace_id,'没有可恢复的诊断，请先指定服务/问题或发起一次排查。',budget)
    effective,rows,decision=await resume(service,request,rows,decision)
    source,p,reason=bind(rows,request,decision,service_names_of(service.router))
    if reason:return ask_target(request,trace_id,reason,budget,rows,decision)
    if re.search(r'(同时|顺便|然后|并且).{0,20}(查|给.{0,6}方案|解决)',request.message):
        return clarification(request,trace_id,'这句话含多个推进目的，请先指定解释、定向核查或求方案。',budget)
    if decision.action=='explain_result' and metadata(source).get('action')=='explain_gaps' and re.search(r'(为什么|为何|用途|why).{0,16}(需要|提供|补充|这些资料|这些证据)|(需要|提供|补充).{0,12}(原因|用途|why)',request.message):
        decision.action='explain_gaps';decision.reason='解释上一轮资料要求的用途'
    budget=budget_for(service,request,source)
    budget.tokens=sum(r.input_tokens+r.output_tokens for r in usage)
    budget.llm_calls=sum(max(1,getattr(r,'attempts',1)) for r in usage)
    from ..llm.accounting import summary
    budget.reserved_tokens=summary(usage)['reserved_tokens']
    started=time.monotonic()
    emit_step(on_event,'dialogue','识别任务内对话动作',decision.model_dump(),result=decision.action)
    try:
        import asyncio
        body=await asyncio.wait_for(perform(service,effective,source,p,decision,budget,trace_id,usage,on_event),
            timeout=max(.05,budget.max_wall_time_sec))
    except Exception as exc:
        body=response(request,trace_id,source,p,decision,'本次推进未完成：'+type(exc).__name__+'；保留原判断和资料。',[],budget,reason='请核对来源或选择其他核查方向。')
    body['cost'].update(tool_calls=budget.tool_calls,input_tokens=sum(r.input_tokens for r in usage),output_tokens=sum(r.output_tokens for r in usage))
    body['explanation']['model_calls']=[{k:getattr(r,k,None) for k in ('role','model','attempts','input_tokens','output_tokens','latency_ms','ok')} for r in usage]
    body['explanation']['dialogue']['duration_ms']=round((time.monotonic()-started)*1000,1)
    if service.state_note:body['degraded'].append(service.state_note)
    emit_step(on_event,'answer','回答任务推进问题',{'action':decision.action},result=body['explanation']['answer']['summary'])
    body=compatible(body)
    await service.task_ports.store_incident(body)
    return body

async def perform(service,request,source,p,d,budget,trace_id,usage,on_event,task_control=None):
    reviewed,entries=prepare(source,p,d)
    items=gaps(service,reviewed,d);materials=metadata(source).get('submissions',[])
    head,rows=explain(reviewed,items,d.action)
    if d.action=='explain_gaps':
        related=relation_rows(source,p,request.message)
        head=summarize(reviewed,items,related,head)
        rows=candidate_rows(reviewed,d)+[{'label':'目标与原因边界','value':
            '这个编号是待解释的异常诊断记录；选定目标不会选中任何原因，也不代表形成原因已确认。'}]+rows+related
    body=response(request,trace_id,source,p,d,head,rows,budget,materials)
    if d.action=='compare_candidates' and re.search('新的候选|新的假设|新假设',request.message):
        from .alternatives import propose
        updated,note=await propose(service,request,source,p,budget,trace_id,on_event,body['incident_id'])
        p=next(x for x in snapshot(updated)['problems'] if x['problem_id']==p['problem_id'])
        reviewed,entries=prepare(updated,p,d)
        head,rows=explain(reviewed,gaps(service,reviewed,d),d.action)
        body=response(request,trace_id,updated,p,d,head+' '+note,rows,budget,materials)
    if d.action=='clarify_target':return response(request,trace_id,source,p,d,d.reason,[],budget,materials,reason=d.reason)
    if d.action=='submit_evidence':
        previous_material_count=len(materials)
        materials,head=receive(request.message,p,materials)
        if materials:
            last=materials[-1];content=last['content']
            if isinstance(content,dict) and content.get('evidence_id'):
                last['archive_reference_status']='belongs_to_problem' if content['evidence_id'] in p['evidence_ids'] else 'not_in_problem'
                head+=' 所引用证据'+('属于本问题。' if last['archive_reference_status']=='belongs_to_problem' else '不在本问题留存引用中，未采用。')
        body=response(request,trace_id,source,p,d,head,[{'label':'下一步','value':'请提供可核验来源，或指定要核查的候选；来源未知时不能确认原因。'}],budget,materials)
        from .submission_followup import choose,refresh_request
        followup=choose(materials[-1],reviewed) if len(materials)>previous_material_count else None
        if followup is None and len(materials)>previous_material_count:
            followup=refresh_request(materials[-1],reviewed,request.message)
        if followup and budget.max_tool_calls>0 and not budget.exhausted():
            updated,calls,queries,note=await verify(service,request,source,p,followup,budget,trace_id,on_event,body['incident_id'],task_control=task_control)
            nextp=next(x for x in snapshot(updated)['problems'] if x['problem_id']==p['problem_id'])
            from ..attribution.v2.confidence import assessment_rows,headline
            head+=' 已沿登记的只读来源核查相关条件；用户资料仍为未核验说明。'+note
            if nextp['cause']['status']=='confirmed':head+=' 已确认原因：'+nextp['cause']['statement']+'。'
            else:head+=' '+headline(snapshot(updated))
            body=response(request,trace_id,updated,nextp,d,head,assessment_rows(snapshot(updated)),budget,materials)
            body['explanation']['dialogue']['submission_followup']={'candidate_id':followup.candidate_id,'queries':queries,'tool_calls':calls,
                'source':'registered_read_only','user_claim_used_as_proof':False}
            body['explanation']['dialogue']['query_history']=list(dict.fromkeys(metadata(source).get('query_history',[])+queries))[-64:]
    if d.action in ('verify_target','request_plan') and (budget.max_tool_calls<=0 or budget.max_tokens<=0 or budget.max_wall_time_sec<=0):
        body=response(request,trace_id,source,p,d,'取证/模型任务预算已耗尽；仍可解释已有证据和缺口。',rows,budget,materials)
    elif d.action=='verify_target':
        updated,calls,queries,note=await verify(service,request,source,p,d,budget,trace_id,on_event,body['incident_id'],task_control=task_control)
        nextp=next(x for x in snapshot(updated)['problems'] if x['problem_id']==p['problem_id'])
        text='形成原因：'+(nextp['cause']['statement'] or '仍待核查')+'。'+note
        rows=[{'label':c['statement'],'value':c['status']+'；'+c['reason']} for c in nextp['candidates']]
        body=response(request,trace_id,updated,nextp,d,text,rows,budget,materials)
        body['explanation']['dialogue']['based_on_incident']=source['incident_id']
        body['explanation']['dialogue']['task_used']=metadata(source).get('task_used') or cost(source)
        body['explanation']['dialogue']['query_history']=list(dict.fromkeys(metadata(source).get('query_history',[])+queries))[-64:]
    elif d.action=='request_plan':
        generated=await plans(service,request,source,p,budget,trace_id,usage,on_event,body['incident_id'],task_control=task_control)
        generated['explanation']['dialogue']=body['explanation']['dialogue'];body=generated
    body['explanation']['dialogue']['gaps']=items
    retain(body,entries)
    return body

def clarification(request,trace_id,message,budget):
    return response(request,trace_id,None,None,Decision(action='clarify_target'),message,[],budget,reason=message)
