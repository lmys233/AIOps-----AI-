"""Request-local durable execution session, with bounded replay and lease fencing."""
import asyncio, dataclasses, hashlib, json, time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from ..domain.tasks import Conflict
from ..common.contracts import ToolResult
from ..common.enums import ToolPermission
from ..observed.attempts import safe
from .checkpoint import snapshot, restore

CURRENT=ContextVar('aiops_execution',default=None)
STEP=ContextVar('aiops_execution_step',default=None)
ENTERED=ContextVar('aiops_execution_entered',default=False)
MODEL_STEP=ContextVar('aiops_model_step',default=None)

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,default=str).encode()).hexdigest()

class Execution:
    def __init__(self,store,scope,task_id,rid,owner,body,steps,usage=()):
        self.store=store;self.scope=scope;self.task_id=task_id;self.rid=rid;self.owner=owner
        self.body=body;self.steps=steps;self.usage=usage;self.lock=asyncio.Lock();self.fenced=False
        self.reused=0;self.state=None;self.tick=time.monotonic();self.base_ms=body.get('used',{}).get('wall_ms',0)
        self.tool_locks={}
        self.usage_start=0;self.token_base=body.get('used',{}).get('tokens',0)
        self.model_base=body.get('used',{}).get('llm_calls',0)
        self.reservation_base=body.get('reserved_model_tokens',0)
        self.model_records=[]
        self.checkpoint_payload=None

    def sync_models(self):
        from ..llm.accounting import summary
        records=list({r.call_id or str(id(r)):r for r in [*self.usage,*self.model_records]}.values())
        totals=summary(records)
        recorded={r.call_id for r in records if getattr(r,'call_id','')}
        pending=[a for s in self.steps.values() if s.get('kind')=='model' and s.get('call_id') not in recorded
                 and s.get('status')=='running' for a in s.get('attempt_history',[])]
        self.body['used']['tokens']=self.token_base+totals['reported_tokens']+sum(a['input_tokens']+a['output_tokens'] for a in pending)
        self.body['used']['llm_calls']=self.model_base+totals['physical_attempts']+len(pending)
        self.body['reserved_model_tokens']=self.reservation_base+totals['reserved_tokens']+sum(a['reserved_tokens'] for a in pending)

    @classmethod
    async def open(cls,store,task,owner,usage,resume=False):
        execution=await cls.prepare(store,task,owner,usage,resume=resume)
        await store.claim(task.scope,task.task_id,execution.rid,owner,
                          None if resume else execution.body)
        return execution

    @classmethod
    async def prepare(cls,store,task,owner,usage,resume=False):
        """Validate/assemble only; task repository claims with its start transaction."""
        rid=task.execution_id
        if resume:
            body=await store.read(task.scope,rid)
            if not body or body['task_id']!=task.task_id:raise Conflict('没有属于该任务的执行台账')
            if body['busy']:raise Conflict('上一轮仍有有效执行租约，请稍后继续')
            if body.get('version')!=1:raise Conflict('执行台账版本不兼容，请开始新任务')
            if (datetime.now(timezone.utc)-datetime.fromisoformat(body['started_at'])).total_seconds()>86400:
                raise Conflict('原执行已超过 24 小时恢复期限，请开始新排查')
            if set(body.get('services',[]))-set(task.scope.services):raise PermissionError('原执行包含已停用服务，不能恢复')
            payload=await store.checkpoint(task.scope,rid,body['memory']) if body.get('memory') else None
            steps=await store.steps(task.scope,rid)
            if any(s.get('kind')=='model' and not s.get('provider_fingerprint') for s in steps.values()):
                raise Conflict('旧模型台账缺少来源身份，不能证明可安全复用；请开始新任务')
            if body.get('preflight_usage_unknown'):
                raise Conflict('任务动作识别的模型费用未知，不能重置预算恢复')
            if any(s['kind']=='model' and (s['status'] in ('running','unknown') or not s.get('usage_known',False)) for s in steps.values()):
                raise Conflict('模型调用中断且费用/结果未知，保留台账；不能重置预算继续，请明确开始新任务')
            # A hard exit can lose the last occupied interval. Reserve its timeout ceiling,
            # separately from measured wall time, so recovery cannot replenish allowance.
            for step in steps.values():
                for attempt in step.get('attempt_history',[]):
                    if attempt['status']=='reserved':
                        body['reserved_wall_ms']=body.get('reserved_wall_ms',0)+1000*attempt.get('timeout_sec',0)
                        attempt['status']='unknown'
        else:
            body={'version':1,'started_at':datetime.now(timezone.utc).isoformat(),'services':list(task.scope.services),
                'task_id':task.task_id,'original':task.original,'node':'','memory':None,
                'used':{'tokens':0,'llm_calls':0,'tool_calls':0,'wall_ms':0},'recoveries':0}
            steps={}
        body['recoveries']=body.get('recoveries',0)+int(resume)
        execution=cls(store,task.scope,task.task_id,rid,owner,body,steps,usage)
        if resume:execution.checkpoint_payload=payload
        if not resume:
            from ..llm.accounting import summary
            execution.sync_models()
            body['preflight_usage_unknown']=not summary(usage)['usage_known']
        execution.resuming=resume
        return execution

    def check(self):
        if self.fenced:raise Conflict('执行台账/租约不可用，停止新增调用')

    async def save(self,step=None,status=None):
        self.check()
        self.body['used']['wall_ms']=self.base_ms+(time.monotonic()-self.tick)*1000
        try:
            ref=await asyncio.wait_for(self.store.write(self.scope,self.rid,self.owner,self.body,status,step),5)
            if ref is not None:self.body['memory']=ref
        except BaseException:
            self.fenced=True;raise

    async def heartbeat(self):
        while True:
            await asyncio.sleep(20)
            async with self.lock:
                try:await self.renew()
                except Exception:return

    async def renew(self):
        self.check()
        wall_ms=self.base_ms+(time.monotonic()-self.tick)*1000
        try:await asyncio.wait_for(self.store.renew(self.scope,self.rid,self.owner,wall_ms),5)
        except BaseException:self.fenced=True;raise

    @asynccontextmanager
    async def activate(self):
        token=CURRENT.set(self);heartbeat=asyncio.create_task(self.heartbeat())
        try:yield self
        finally:
            heartbeat.cancel()
            try:await heartbeat
            except asyncio.CancelledError:pass
            CURRENT.reset(token)

    async def release(self,status='interrupted'):
        if not self.fenced:
            async with self.lock:await self.save(status=status)

    async def enter_graph(self,state,entry):
        self.state=state
        if self.body['memory']:
            payload=self.checkpoint_payload or await self.store.checkpoint(self.scope,self.rid,self.body['memory'])
            restore(state,payload)
            board=state.flags.get('board_store')
            if board:
                state.board=await board.create(state.incident_id,state.slots)
                for outcome in state.outcomes:
                    await board.publish(state.incident_id,outcome.role,outcome.evidence)
                if state.hypotheses:await board.propose_hypotheses(state.incident_id,'rca',state.hypotheses)
                if state.open_questions:await board.raise_questions(state.incident_id,state.open_questions)
                if state.flags.get('attribution_v2'):await board.set_attribution(state.incident_id,state.flags['attribution_v2'])
            # Spend ledger totals, including calls completed inside the interrupted node.
            state.add_note('从执行台账恢复；保留原查询窗口、事实与候选，节点内已完成工具复用原结果')
        used=self.body['used'];state.budget.tokens=max(state.budget.tokens,used['tokens'])
        state.budget.llm_calls=max(state.budget.llm_calls,used['llm_calls'])
        state.budget.reserved_tokens=max(state.budget.reserved_tokens,self.body.get('reserved_model_tokens',0))
        state.budget.tool_calls=max(state.budget.tool_calls,used['tool_calls'])
        state.budget.elapsed_sec=max(state.budget.elapsed_sec,(used['wall_ms']+self.body.get('reserved_wall_ms',0))/1000)
        return self.body['node'] or entry

    async def checkpoint(self,state,node):
        async with self.lock:
            self.sync_models()
            # Physical receipts own model counters; planner placeholders/route traces
            # must not be charged again when a node finishes.
            state.budget.tokens=self.body['used']['tokens']
            state.budget.llm_calls=self.body['used']['llm_calls']
            state.budget.reserved_tokens=self.body.get('reserved_model_tokens',0)
            self.body.update(node=node,memory=snapshot(state),task_memory={
                'goal':state.flags.get('original_user_message') or state.user_message,
                'purpose':state.intent.value if state.intent else None,'slots':state.slots.model_dump(mode='json'),
                'known_facts':[{'evidence_id':e.evidence_id,'summary':e.summary[:240]} for e in state.evidence[:200]],
                'candidate_refs':[{'hypothesis_id':h.hypothesis_id,'statement':h.statement[:240]} for h in state.hypotheses[:100]],
                'problem_refs':[p.get('problem_id') for p in (state.flags.get('attribution_v2') or {}).get('problems',[])],
                'open_questions':list(state.open_questions)[:50],'gaps':list(state.degraded_detail)[:50],
                'execution_id':self.rid,'resume_node':node,'query_anchor':self.body['started_at']})
            self.checkpoint_payload=self.body['memory']
            # Ledger totals can exceed node counters while concurrent tools are in flight.
            used=self.body['used'];used['tool_calls']=max(used['tool_calls'],state.budget.tool_calls)
            await self.save()

    async def attempt(self,timeout=None):
        active=STEP.get()
        if not active:return None
        key,row=active
        async with self.lock:
            if row['attempts']>=2:return '跨恢复的工具尝试已达 2 次，停止重复请求'
            row['attempts']+=1;self.body['used']['tool_calls']+=1
            row.setdefault('attempt_history',[]).append({'number':row['attempts'],
                'started_at':datetime.now(timezone.utc).isoformat(),'status':'reserved','timeout_sec':timeout or 0})
            await self.save((key,row))
        return None

    async def tool(self,definition,params,operation):
        ENTERED.set(True)
        self.check();spec=definition.spec
        key='tool:'+digest([definition.to_dict(),spec.input_schema,params])
        async with self.tool_locks.setdefault(key,asyncio.Lock()):
            return await self._tool(key,definition,params,operation)

    async def _tool(self,key,definition,params,operation):
        spec=definition.spec
        async with self.lock:
            row=self.steps.get(key)
            if row and row['status']=='done':
                self.reused+=1;result=ToolResult.model_validate(row['result'])
                from ..common.tool_budget import CURRENT as TOOL_BUDGET
                reservation=TOOL_BUDGET.get()
                if reservation and reservation[1]:reservation[0].tool_calls=max(0,reservation[0].tool_calls-1)
                from ..observed.attempts import ACTIVE
                attempt=ACTIVE.get()
                if attempt is not None:attempt.replayed=True
                return result.model_copy(update={'summary':result.summary+'（复用本任务原执行结果；未重新请求上游）','elapsed_ms':0})
            if row and (spec.permission!=ToolPermission.READ_ONLY or not spec.idempotent or row['attempts']>=2):
                return ToolResult(ok=False,error_code='TOOL_UNAVAILABLE',error_type='RecoveryBlocked',degraded=True,
                    summary='原调用结果未知或已到总尝试上限，停止重放；保留原执行台账')
            row=row or {'kind':'tool','tool':spec.name,'params':safe(params),'attempts':0,'created_at':datetime.now(timezone.utc).isoformat()}
            from ..observed.attempts import ACTIVE
            call=ACTIVE.get()
            if call:
                ids=row.setdefault('call_ids',[])
                if call.cid not in ids:ids.append(call.cid)
            row['status']='running';self.steps[key]=row;await self.save((key,row))
        token=STEP.set((key,row))
        try:result=await operation()
        except BaseException:
            async with self.lock:
                row['status']='unknown';await self.save((key,row))
            raise
        finally:STEP.reset(token)
        async with self.lock:
            row.update(status='done',result=safe(result.model_dump(mode='json')),finished_at=datetime.now(timezone.utc).isoformat())
            await self.save((key,row))
        return result

    async def finish_attempt(self,error='none'):
        active=STEP.get()
        if not active:return
        key,row=active
        async with self.lock:
            if row.get('attempt_history'):
                row['attempt_history'][-1].update(status='succeeded' if error=='none' else 'failed',
                    error=error,finished_at=datetime.now(timezone.utc).isoformat())
            await self.save((key,row))

    def summary(self):
        return {'execution_id':self.rid,'resume_node':self.body['node'],'recoveries':self.body['recoveries'],
            'reused_calls':self.reused,'steps':len(self.steps),'used':dict(self.body['used']),
            'query_anchor':self.body['started_at'],'memory_saved':bool(self.body['memory']),
            'reservations':{'unknown_tool_wall_ms':self.body.get('reserved_wall_ms',0),
                            'unknown_model_tokens':self.body.get('reserved_model_tokens',0)},
            'budget_unknown':bool(self.body.get('preflight_usage_unknown')) or any(s['kind']=='model' and (s['status'] in ('running','unknown') or not s.get('usage_known',False)) for s in self.steps.values())}


async def model_attempt_event(attempt):
    ctx=CURRENT.get();active=MODEL_STEP.get()
    if ctx is None or active is None:return
    key,row=active
    async with ctx.lock:
        rows=row.setdefault('attempt_history',[])
        if attempt['number']==len(rows)+1:rows.append(dict(attempt))
        else:rows[attempt['number']-1]=dict(attempt)
        row['attempts']=len(rows);ctx.sync_models()
        await ctx.save((key,row))


def durable_model(method):
    @wraps(method)
    async def wrapped(gateway,prompt,schema,**kwargs):
        ctx=CURRENT.get()
        if ctx is None:return await method(gateway,prompt,schema,**kwargs)
        from ..llm.accounting import CALL_RECORDS,summary
        from ..common.model_budget import active,model_budget_scope,refund_placeholder
        from contextlib import nullcontext
        from uuid import uuid4
        ctx.check()
        identity=digest([type(gateway).__module__,type(gateway).__qualname__,
            getattr(gateway,'base_url',''),kwargs.get('model') or getattr(gateway,'default_model',''),
            getattr(gateway,'prompt_version','1'),'model-reuse-v2'])
        key='model:'+digest([prompt,schema.model_json_schema(),kwargs,identity])
        async with ctx.lock:
            row=ctx.steps.get(key)
            if row:
                if row['status']=='done':
                    ctx.reused+=1;refund_placeholder();return schema.model_validate(row['result'])
                refund_placeholder()
                raise Conflict('该模型调用结果/费用未确定或已失败，不重复请求')
            row={'kind':'model','role':kwargs.get('role'),'status':'running','attempts':0,
                 'call_id':'mdl_'+uuid4().hex,'provider_fingerprint':identity,
                 'prompt_hash':digest(prompt),'created_at':datetime.now(timezone.utc).isoformat()}
            row['context']=getattr(prompt,'context_audit',{})
            ctx.steps[key]=row;await ctx.save((key,row))
        before=len(ctx.usage);local=[]
        token=CALL_RECORDS.set(local);step_token=MODEL_STEP.set((key,row))
        scope=model_budget_scope(ctx.state.budget,pre_reserved=False) if ctx.state is not None and not active() else nullcontext()
        error=None
        try:
            with scope:result=await method(gateway,prompt,schema,**kwargs)
        except BaseException as exc:
            error=exc
            raise
        finally:
            CALL_RECORDS.reset(token);MODEL_STEP.reset(step_token)
            # Legacy injected gateways can report one explicit record. Real gateway
            # receipts use the call-local collector, never concurrent list slices.
            records=local or (ctx.usage[before:] if len(ctx.usage[before:])==1 else [])
            parent=CALL_RECORDS.get()
            if parent is not None:
                parent.extend(r for r in records if not any(old is r for old in parent))
            no_dispatch=not row.get('attempt_history') and not records and hasattr(gateway,'_client')
            known=(bool(records) and summary(records)['usage_known']) or no_dispatch
            if no_dispatch:refund_placeholder()
            async with ctx.lock:
                row.update(status=('failed' if known else 'unknown') if error else 'done',
                    usage=[dataclasses.asdict(r) for r in records],usage_known=known)
                if not error:row['result']=result.model_dump(mode='json')
                ctx.model_records.extend(records);ctx.sync_models()
                if not ctx.fenced:
                    try:await ctx.save((key,row))
                    except BaseException:
                        # Preserve the original cancellation/provider failure. save()
                        # already fenced new work; no recovery may assume the row committed.
                        if error is None:raise
        return result
    return wrapped


def record_denial(method):
    """Policy is always evaluated; retain pre-execution refusals without replay authority."""
    @wraps(method)
    async def wrapped(executor,role,tool,params):
        token=ENTERED.set(False)
        try:
            result=await method(executor,role,tool,params)
            executed=ENTERED.get()
        finally:ENTERED.reset(token)
        ctx=CURRENT.get()
        if ctx is not None and not executed and result.error_code in ('TOOL_FORBIDDEN','TOOL_INVALID_PARAMS'):
            key='denied:'+digest([role,tool,params])
            async with ctx.lock:
                row={'kind':'denial','role':role,'tool':tool,'status':'done','attempts':0,
                    'params':safe(params),'result':safe(result.model_dump(mode='json')),
                    'finished_at':datetime.now(timezone.utc).isoformat()}
                ctx.steps[key]=row;await ctx.save((key,row))
        return result
    return wrapped
