"""Small lazy pool; feedback transaction includes the learning outbox."""
import asyncio,hashlib,json,secrets
from datetime import datetime,timezone
from ..observed.store import RegistryStore
from .schema import DDL


def raw(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)
def decoded(value):return json.loads(value) if isinstance(value,str) else value
def digest(value):return hashlib.sha256(raw(value).encode()).hexdigest()


class Conflict(Exception):
    def __init__(self,state):self.state=state


class PreferenceStore(RegistryStore):
    ddl=DDL

    async def secret(self):
        await self.fetch("INSERT INTO agent_preference_settings VALUES('subject-signing-key',$1) ON CONFLICT DO NOTHING RETURNING key",secrets.token_hex(32))
        return (await self.one("SELECT value FROM agent_preference_settings WHERE key='subject-signing-key'"))['value']

    async def turn(self,tenant,oid,sid,tid):
        row=await self.one('''SELECT payload FROM agent_chat_turns WHERE tenant_id=$1 AND session_id=$2 AND turn_id=$3
            AND payload#>>'{response,explanation,observed_system,observed_id}'=$4''',tenant,sid,tid,oid)
        if not row:raise KeyError('answer not found in system/session')
        return decoded(row['payload'])

    async def feedback(self,tenant,subject,oid,sid,tid,request,answer):
        fid='fb_'+digest([tenant,subject,oid,sid,tid])[:32]
        fingerprint=digest(request.model_dump());pool=await self.connect()
        async with pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',tenant+':feedback:'+subject)
            prior=await c.fetchrow('SELECT * FROM agent_feedback_events WHERE tenant=$1 AND subject=$2 AND request_id=$3',tenant,subject,request.request_id)
            if prior:
                if prior['fingerprint']!=fingerprint:raise ValueError('request_id reused with different content')
                return decoded(prior['data'])
            row=await c.fetchrow('SELECT * FROM agent_feedback_latest WHERE tenant=$1 AND subject=$2 AND feedback_id=$3',tenant,subject,fid)
            old=decoded(row['data']) if row else {'revision':0,'vote':None}
            if old['revision']!=request.expected_revision:raise Conflict(old)
            state={k:getattr(request,k) for k in ('vote','reason_codes','comment')}
            changed=any(state.get(k)!=old.get(k,[] if k=='reason_codes' else '') for k in state)
            if not row and state=={'vote':None,'reason_codes':[],'comment':''}:changed=False
            state.update(feedback_id=fid,revision=old['revision']+int(changed),answer_ref=request.answer_ref,
                         updated_at=datetime.now(timezone.utc).isoformat(),learning_status='queued' if changed else old.get('learning_status','none'))
            if changed:
                await c.execute('''INSERT INTO agent_feedback_latest(tenant,subject,feedback_id,observed_id,session_id,turn_id,
                    answer_ref,incident_id,revision,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
                    ON CONFLICT(tenant,subject,feedback_id) DO UPDATE SET revision=$9,data=$10::jsonb,updated_at=now()''',
                    tenant,subject,fid,oid,sid,tid,request.answer_ref,answer.get('incident_id',''),state['revision'],raw(state))
                await c.execute('INSERT INTO agent_preference_jobs(tenant,subject,feedback_id,revision) VALUES($1,$2,$3,$4)',tenant,subject,fid,state['revision'])
            await c.execute('INSERT INTO agent_feedback_events(tenant,subject,request_id,fingerprint,feedback_id,revision,data) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)',
                            tenant,subject,request.request_id,fingerprint,fid,state['revision'],raw(state))
        if changed:self.invalidate(tenant,subject)
        return state

    def invalidate(self,tenant,subject):
        reads=getattr(self,'_snapshot_reads',{})
        for key in list(reads):
            if key[:2]==(tenant,subject):reads.pop(key,None)

    async def overlay(self,tenant,subject,oid,sid,rows):
        keys=[str(r.get('turn_id','')) for r in rows]
        found=await self.fetch('''SELECT f.turn_id,f.data,j.status AS learning_status FROM agent_feedback_latest f
            LEFT JOIN agent_preference_jobs j ON (j.tenant,j.subject,j.feedback_id,j.revision)=(f.tenant,f.subject,f.feedback_id,f.revision)
            WHERE f.tenant=$1 AND f.subject=$2 AND f.observed_id=$3 AND f.session_id=$4 AND f.turn_id=ANY($5::text[])''',tenant,subject,oid,sid,keys) if keys else []
        states={r['turn_id']:{**decoded(r['data']),'learning_status':r['learning_status'] or 'none'} for r in found}
        return [dict(r,feedback=states.get(r.get('turn_id')),feedback_target=target(r)) for r in rows]

    async def current(self,tenant,subject):
        row=await self.one('''SELECT h.version,h.enabled,h.learning,v.profile,v.hash,v.cutoff,v.created_at FROM agent_preference_heads h
            LEFT JOIN agent_preference_versions v ON v.tenant=h.tenant AND v.subject=h.subject AND v.version=h.version
            WHERE h.tenant=$1 AND h.subject=$2''',tenant,subject)
        if not row:return {'version':0,'profile':{'preferences':[],'experiences':[]},'hash':'','enabled':True,'learning':True}
        out=dict(row);out['profile']=decoded(out['profile']) or {'preferences':[],'experiences':[]}
        return out

    async def supports(self,tenant,subject,feedback_ids=None):
        rows=await self.fetch('''SELECT feedback_id,revision,data FROM agent_feedback_latest WHERE tenant=$1 AND subject=$2
            AND ($3::text[] IS NULL OR feedback_id=ANY($3::text[]))''',tenant,subject,list(feedback_ids) if feedback_ids is not None else None)
        return {r['feedback_id']:{'revision':r['revision'],**decoded(r['data'])} for r in rows}

    async def snapshot(self,tenant,subject,support_ids=None):
        # Coalesce only overlapping reads, never reuse a completed/stale snapshot.
        reads=getattr(self,'_snapshot_reads',None)
        if reads is None:reads=self._snapshot_reads={}
        key=(tenant,subject,tuple(support_ids) if support_ids is not None else None);task=reads.get(key)
        if task is None or task.done():
            task=asyncio.create_task(self._snapshot(tenant,subject,support_ids));reads[key]=task
            def completed(done):
                if reads.get(key) is done:reads.pop(key,None)
                if not done.cancelled():done.exception()
            task.add_done_callback(completed)
        try:return await asyncio.shield(task)
        finally:
            if task.done() and reads.get(key) is task:reads.pop(key,None)

    async def _snapshot(self,tenant,subject,support_ids=None):
        row=await self.one('''SELECT coalesce(h.version,0) AS version,coalesce(h.enabled,true) AS enabled,
            v.profile,v.hash,coalesce((SELECT jsonb_object_agg(f.feedback_id,f.data) FROM agent_feedback_latest f
                WHERE f.tenant=$1 AND f.subject=$2 AND f.feedback_id=ANY(coalesce($3::text[],ARRAY(
                SELECT jsonb_array_elements_text(jsonb_path_query_array(v.profile,'$.*[*].support[*].feedback_id')))))), '{}'::jsonb) AS support
            FROM (SELECT 1) seed LEFT JOIN agent_preference_heads h ON h.tenant=$1 AND h.subject=$2
            LEFT JOIN agent_preference_versions v ON v.tenant=h.tenant AND v.subject=h.subject AND v.version=h.version''',tenant,subject,list(support_ids) if support_ids is not None else None)
        return dict(row,profile=decoded(row['profile']) or {'preferences':[],'experiences':[]},support=decoded(row['support']))


def answer_ref(row):
    response=row.get('response') or {};view=response.get('answer_view') or row.get('answer_view') or {}
    # Actual archive uses explanation.answer_view on older records.
    view=view or (response.get('explanation') or {}).get('answer') or {}
    if view.get('view_id'):return view['view_id']
    legacy=view or row.get('answer') or response.get('report')
    if not legacy:raise ValueError('no saved answer body')
    # Projection/paging metadata is not part of the immutable legacy answer.
    return 'legacy_'+digest([row.get('incident_id') or response.get('incident_id'),legacy])


def target(row):
    try:return {'turn_id':row.get('turn_id',''),'answer_ref':answer_ref(row)}
    except ValueError:return None
