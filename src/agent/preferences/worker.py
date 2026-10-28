"""PG outbox/leases: model failures never prevent feedback persistence or reads."""
import asyncio,json,os,secrets,time
from datetime import datetime,timezone
from pydantic import BaseModel,ConfigDict,Field
from ..summary import SummaryAgent,SummaryRequest
from ..common.tokens import estimate_tokens
from ..llm.accounting import summary
from .config import Config
from .store import decoded,raw,digest
from .learning import episode_source,abstract,partitions,STYLE
from .models import Episode
from .budget import reservation


class Selection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    preference_keys:list[str]=Field(default_factory=list,max_length=16)
    experience_keys:list[str]=Field(default_factory=list,max_length=8)


def redacted(source):
    from ..observed.attempts import safe
    from ..memory.gates import redact_text
    def visit(value):
        if isinstance(value,str):return redact_text(value)[0]
        if isinstance(value,dict):return {key:visit(item) for key,item in value.items()}
        if isinstance(value,list):return [visit(item) for item in value]
        return value
    return visit(safe(source))


class PreferenceAgent:
    def __init__(self,store,gateway,config=None,capability_hash='',*,tool_names=()):
        self.store=store;self.gateway=gateway;self.config=config or Config.env()
        self.summary=SummaryAgent(gateway);self.capability_hash=capability_hash
        self.tenant=os.getenv('AIOPS_TENANT','default')
        self.tool_names=tuple(tool_names)
        self.task=None;self.last_error=''

    async def start(self):
        if self.task is None:self.task=asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True);self.task=None
        if self.gateway and hasattr(self.gateway,'aclose'):await self.gateway.aclose()

    async def loop(self):
        while True:
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception as exc:self.last_error=type(exc).__name__
            await asyncio.sleep(self.config.poll_sec)

    async def claim_job(self):
        token=secrets.token_hex(16)
        await self.store.fetch("UPDATE agent_preference_jobs SET status='unknown',error='dispatch_interrupted_usage_unknown' WHERE tenant=$1 AND status='dispatching' AND lease_until<now() RETURNING feedback_id",self.tenant)
        rows=await self.store.fetch('''WITH ready AS (
            SELECT j.tenant,j.subject,j.feedback_id,j.revision FROM agent_preference_jobs j
            LEFT JOIN agent_preference_heads h ON h.tenant=j.tenant AND h.subject=j.subject
            WHERE j.tenant=$2 AND coalesce(h.learning,true) AND j.attempts<3 AND (j.status='queued' OR (j.status='running' AND j.lease_until<now()))
            ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1)
            UPDATE agent_preference_jobs j SET status='running',attempts=j.attempts+1,lease_until=now()+interval '3 minutes',token=$1
            FROM ready r WHERE (j.tenant,j.subject,j.feedback_id,j.revision)=(r.tenant,r.subject,r.feedback_id,r.revision) RETURNING j.*''',token,self.tenant)
        return rows[0] if rows else None

    async def job(self,job):
        s=self.store;t,u,fid,revision=(job[k] for k in ('tenant','subject','feedback_id','revision'))
        dispatched=False;records=[]
        try:
            feedback=await s.one('SELECT * FROM agent_feedback_latest WHERE tenant=$1 AND subject=$2 AND feedback_id=$3',t,u,fid)
            current=decoded(feedback['data'])
            if feedback['revision']!=revision or current.get('vote') is None:
                await self.finish_job(job,'superseded');return
            row=await s.turn(t,feedback['observed_id'],feedback['session_id'],feedback['turn_id'])
            # Recheck session visibility; archived bodies are retained but deleted chats cannot train.
            hidden=await s.one('SELECT 1 FROM agent_chat_deleted_sessions WHERE tenant_id=$1 AND session_id=$2',t,feedback['session_id'])
            if hidden:await self.finish_job(job,'skipped');return
            hidden=await s.one('SELECT 1 FROM agent_system_session_views WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 AND deleted',t,feedback['observed_id'],feedback['session_id'])
            if hidden:await self.finish_job(job,'skipped');return
            source=episode_source(row,current,self.capability_hash,self.tool_names)
            source=redacted(source)
            req=SummaryRequest(purpose='feedback_episode',source=source,source_refs=[fid+':'+str(revision)],
                input_tokens=self.config.context_tokens,requirements='summary保留本次诉求/实际结果/反馈，preferences只允许'+','.join(STYLE)+'；无明确偏好则空数组。witnesses给出每个偏好在用户comment中的逐字原话，不得从答案风格或单纯点赞猜偏好。process_effective只有用户明确评价流程有效才为true。')
            async with reservation(s,t,u,self.config,min(self.config.context_tokens,10000)) as records:
                await s.fetch("UPDATE agent_preference_jobs SET status='dispatching' WHERE tenant=$1 AND subject=$2 AND feedback_id=$3 AND revision=$4 AND token=$5 RETURNING status",t,u,fid,revision,job['token'])
                dispatched=True
                result=await self.summary.summarize(req,Episode)
            source['learned']=result.content
            # Raw bounded episode is reconstructible; the independent journal stores the compressed learning signal.
            source['question']=source['question'][:500];source['answer']=source['answer'][:500]
            source['tools']=[{'tool':x['tool'],'status':x['status']} for x in source['tools'][:12]]
            source['feedback']['comment']=source['feedback'].get('comment','')[:300]
            content=raw(source);count=estimate_tokens(content)
            if count>2048:raise ValueError('feedback episode envelope too large')
            pool=await s.connect()
            async with pool.acquire() as c,c.transaction():
                # Allocation and cutoff use the same per-subject commit barrier.
                await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',t+':preference-journal:'+u)
                owned=await c.fetchval('SELECT 1 FROM agent_preference_jobs WHERE tenant=$1 AND subject=$2 AND feedback_id=$3 AND revision=$4 AND token=$5 AND lease_until>now() FOR UPDATE',t,u,fid,revision,job['token'])
                latest=await c.fetchval('SELECT revision FROM agent_feedback_latest WHERE tenant=$1 AND subject=$2 AND feedback_id=$3 FOR SHARE',t,u,fid)
                if not owned:return
                if latest==revision:
                    await c.execute('INSERT INTO agent_preference_journal(tenant,subject,feedback_id,revision,content,tokens) VALUES($1,$2,$3,$4,$5::jsonb,$6) ON CONFLICT DO NOTHING',t,u,fid,revision,content,count)
                    await c.execute('INSERT INTO agent_summary_records VALUES($1,$2,$3,$4,$5::jsonb,now()) ON CONFLICT DO NOTHING','sum_'+digest([fid,revision]),t,u,'feedback_episode',raw(result.model_dump()))
                await c.execute("UPDATE agent_preference_jobs SET status=$6 WHERE tenant=$1 AND subject=$2 AND feedback_id=$3 AND revision=$4 AND token=$5",t,u,fid,revision,job['token'],'done' if latest==revision else 'superseded')
            await self.backpressure(t,u)
        except asyncio.CancelledError:raise
        except Exception as exc:
            unknown=dispatched and (not records or not summary(records)['usage_known'])
            await self.finish_job(job,'unknown' if unknown else 'failed' if job['attempts']>=3 else 'queued',type(exc).__name__)
            if dispatched:
                await s.fetch('INSERT INTO agent_summary_records VALUES($1,$2,$3,$4,$5::jsonb,now()) ON CONFLICT DO NOTHING RETURNING id',
                    'sum_failed_'+digest([fid,revision,job['attempts']]),t,u,'feedback_episode',
                    raw({'status':'unknown' if unknown else 'failed','error':type(exc).__name__,'usage':summary(records),'reservation_retained':unknown}))

    async def finish_job(self,j,status,error=''):
        await self.store.fetch('UPDATE agent_preference_jobs SET status=$6,error=$7 WHERE tenant=$1 AND subject=$2 AND feedback_id=$3 AND revision=$4 AND token=$5 RETURNING status',
            j['tenant'],j['subject'],j['feedback_id'],j['revision'],j['token'],status,error)

    async def backpressure(self,t,u):
        await self.store.fetch('''WITH pending AS (SELECT seq,sum(tokens) OVER(ORDER BY seq DESC) AS total FROM agent_preference_journal
            WHERE tenant=$1 AND subject=$2 AND skipped IS NULL AND seq>coalesce((SELECT cursor FROM agent_preference_heads WHERE tenant=$1 AND subject=$2),0))
            UPDATE agent_preference_journal j SET skipped='backlog_limit',content=jsonb_build_object('feedback_id',j.feedback_id,'revision',j.revision),tokens=0
            FROM pending p WHERE j.seq=p.seq AND p.total>$3 RETURNING j.seq''',t,u,self.config.backlog_tokens)

    async def candidates(self):
        return await self.store.fetch('''SELECT j.tenant,j.subject,sum(j.tokens)::bigint AS tokens,count(*) AS count,min(j.created_at) AS oldest,
            coalesce(h.version,0) AS version,h.last_success,h.lease_until
            FROM agent_preference_journal j LEFT JOIN agent_preference_heads h ON h.tenant=j.tenant AND h.subject=j.subject
            WHERE j.tenant=$1 AND j.seq>coalesce(h.cursor,0) AND coalesce(h.learning,true) AND (h.lease_until IS NULL OR h.lease_until<now())
            GROUP BY j.tenant,j.subject,h.version,h.last_success,h.lease_until LIMIT 20''',self.tenant)

    def due(self,row):
        now=datetime.now(timezone.utc)
        last=datetime.fromisoformat(row['last_success']) if isinstance(row.get('last_success'),str) else row.get('last_success')
        if last and (now-last).total_seconds()<self.config.cooldown_sec:return False
        oldest=datetime.fromisoformat(row['oldest']) if isinstance(row['oldest'],str) else row['oldest']
        return row['tokens']>=self.config.trigger_tokens or not row['version'] or (
            row['count']>=8 and last and (now-last).total_seconds()>=86400) or (now-oldest).total_seconds()>=7*86400

    async def claim_build(self,t,u):
        pool=await self.store.connect();bid='pb_'+secrets.token_hex(16)
        async with pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',t+':preference-journal:'+u)
            await c.execute('INSERT INTO agent_preference_heads(tenant,subject) VALUES($1,$2) ON CONFLICT DO NOTHING',t,u)
            head=await c.fetchrow('SELECT * FROM agent_preference_heads WHERE tenant=$1 AND subject=$2 FOR UPDATE',t,u)
            if not head['learning'] or (head['lease_until'] and head['lease_until']>datetime.now(timezone.utc)):return None
            if head['last_success'] and (datetime.now(timezone.utc)-head['last_success']).total_seconds()<self.config.cooldown_sec:return None
            count=await c.fetchval("""SELECT count(*) FROM agent_preference_builds WHERE tenant=$1 AND subject=$2 AND created_at>=current_date
                AND NOT(status='noop' AND coalesce(details->>'empty_initial','false')='true' AND coalesce((details->>'partitions')::int,0)=0)""",t,u)
            if count>=self.config.max_builds:return None
            cutoff=await c.fetchval('SELECT max(seq) FROM agent_preference_journal WHERE tenant=$1 AND subject=$2',t,u)
            if not cutoff or cutoff<=head['cursor']:return None
            abandoned=await c.fetchval("SELECT build_id FROM agent_preference_builds WHERE tenant=$1 AND subject=$2 AND base_version=$3 AND cutoff>=$4 AND status IN ('running','unknown') LIMIT 1",t,u,head['version'],cutoff)
            if abandoned:
                await c.execute("UPDATE agent_preference_builds SET status='unknown',finished_at=now() WHERE build_id=$1",abandoned)
                return None
            await c.execute("UPDATE agent_preference_heads SET lease_token=$3,lease_until=now()+interval '10 minutes',last_checked=now() WHERE tenant=$1 AND subject=$2",t,u,bid)
            await c.execute("INSERT INTO agent_preference_builds(build_id,tenant,subject,base_version,cutoff,status) VALUES($1,$2,$3,$4,$5,'running')",bid,t,u,head['version'],cutoff)
            return {'id':bid,'base':head['version'],'cursor':head['cursor'],'cutoff':cutoff}

    async def build(self,t,u):
        ticket=await self.claim_build(t,u)
        if not ticket:return None
        dispatched=False;records=[]
        try:
            current=await self.store.current(t,u)
            rows=await self.store.fetch('SELECT content FROM agent_preference_journal WHERE tenant=$1 AND subject=$2 AND seq>$3 AND seq<=$4 AND skipped IS NULL ORDER BY seq',t,u,ticket['cursor'],ticket['cutoff'])
            episodes=[decoded(r['content']) for r in rows]
            refs={ref['feedback_id'] for kind in ('preferences','experiences') for item in current['profile'].get(kind,[]) for ref in item.get('support',[])}
            refs.update(e['feedback']['feedback_id'] for e in episodes if e.get('feedback'))
            latest=await self.store.supports(t,u,sorted(refs))
            profile=abstract(episodes,current['profile'],latest)
            # Give the model bounded partitions to prioritize canonical, already-supported suggestions.
            groups=partitions(episodes,min(self.config.context_tokens,getattr(self.gateway,'context_tokens',50000)),overhead=6000) if profile['preferences'] or profile['experiences'] else []
            selected_p=set();selected_e=set();receipts=[]
            for group in groups:
                request=SummaryRequest(purpose='preference_partition',source={'episodes':[e.get('learned',{}) for e in group],
                    'candidates':profile},requirements='只返回候选中值得保留的preference_keys和experience_keys。合并同义项，舍弃弱项，不生成新key。',
                    input_tokens=self.config.context_tokens,output_tokens=1024)
                async with reservation(self.store,t,u,self.config,min(self.config.context_tokens,estimate_tokens(raw(request.source))+5000)) as records:
                    dispatched=True
                    result=await self.summary.summarize(request,Selection,role='preference_build')
                selected_p.update(result.content['preference_keys']);selected_e.update(result.content['experience_keys']);receipts.append(result.model_dump())
            if groups:
                profile['preferences']=[p for p in profile['preferences'] if p['key'] in selected_p]
                profile['experiences']=[p for p in profile['experiences'] if p['key'] in selected_e]
            await self.publish(t,u,ticket,profile,{'partitions':len(groups),'summaries':receipts})
            return ticket
        except asyncio.CancelledError:raise
        except Exception as exc:
            unknown=dispatched and (not records or not summary(records)['usage_known'])
            await self.store.fetch("UPDATE agent_preference_builds SET status=$3,details=$2::jsonb,finished_at=now() WHERE build_id=$1 RETURNING status",ticket['id'],raw({'error':type(exc).__name__,'usage':summary(records),'reservation_retained':unknown}),'unknown' if unknown else 'failed')
            await self.store.fetch('UPDATE agent_preference_heads SET lease_until=NULL,lease_token=NULL WHERE tenant=$1 AND subject=$2 AND lease_token=$3 RETURNING version',t,u,ticket['id'])
            self.last_error=type(exc).__name__;return None

    async def publish(self,t,u,ticket,profile,details):
        pool=await self.store.connect();hash_=digest(profile)
        async with pool.acquire() as c,c.transaction():
            head=await c.fetchrow('SELECT * FROM agent_preference_heads WHERE tenant=$1 AND subject=$2 FOR UPDATE',t,u)
            if head['version']!=ticket['base'] or head['lease_token']!=ticket['id'] or head['lease_until']<datetime.now(timezone.utc):raise RuntimeError('stale build fenced')
            previous=await c.fetchval('SELECT hash FROM agent_preference_versions WHERE tenant=$1 AND subject=$2 AND version=$3',t,u,head['version'])
            empty_initial=not head['version'] and not profile['preferences'] and not profile['experiences']
            details=dict(details,empty_initial=empty_initial)
            version=head['version']+int(previous!=hash_ and not empty_initial)
            if version!=head['version']:
                await c.execute('INSERT INTO agent_preference_versions(tenant,subject,version,hash,profile,cutoff,build_id) VALUES($1,$2,$3,$4,$5::jsonb,$6,$7)',t,u,version,hash_,raw(profile),ticket['cutoff'],ticket['id'])
            await c.execute('UPDATE agent_preference_heads SET version=$3::bigint,cursor=$4,last_success=CASE WHEN $3::bigint>0 THEN now() ELSE NULL END,lease_token=NULL,lease_until=NULL WHERE tenant=$1 AND subject=$2',t,u,version,ticket['cutoff'])
            await c.execute('UPDATE agent_preference_builds SET status=$2,details=$3::jsonb,finished_at=now() WHERE build_id=$1',ticket['id'],'published' if version!=head['version'] else 'noop',raw(details))

    async def tick(self):
        # Each tick is finite. One process runs one model stage at a time; PG fences other instances.
        for _ in range(32):
            job=await self.claim_job()
            if not job:break
            await self.job(job)
        for row in await self.candidates():
            if self.due(row):await self.build(row['tenant'],row['subject'])
