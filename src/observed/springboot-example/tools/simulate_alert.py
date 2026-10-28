"""Explicit Java example fault -> real SDK observation -> alert -> saved conversation."""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT/'src'))
import httpx
import asyncpg
from agent.common.envfile import load_env_file


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Explicitly generate one isolated read-only fault and run a real diagnostic model')
    parser.add_argument('--output',type=Path,default=ROOT/'.run/alert-ingestion/demo')
    parser.add_argument('--follow-up',action='store_true',help='Ask one evidence-gap follow-up in the generated alert conversation')
    args=parser.parse_args()
    if not args.execute:parser.error('This creates an example fault and invokes the configured model; pass --execute')
    load_env_file();deployment=json.loads((ROOT/'.run/java-sdk/enrollment.json').read_text(encoding='utf-8'))
    if deployment['name']!='springboot-example' or deployment['env']!='dev':raise RuntimeError('Only the authorized dev example is permitted')
    oid=deployment['observed_id'];service=deployment['service'];base=deployment['agent_url']
    token=os.getenv('AIOPS_AGENT_TOKEN');headers={'Authorization':'Bearer '+token} if token else {}
    folder=args.output;folder.mkdir(parents=True,exist_ok=True)
    def save(name,value):(folder/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    async with httpx.AsyncClient(timeout=8,headers=headers) as agent,httpx.AsyncClient(timeout=5) as business:
        health=(await business.get(deployment['sdk_origin']+'/health')).json()
        if health.get('observed_id')!=oid:raise RuntimeError('SDK identity does not match enrollment')
        started=datetime.now(timezone.utc);trace=uuid4().hex
        actual=await business.get(deployment['business_origin']+'/drills/missing-table',
            headers={'traceparent':'00-'+trace+'-'+uuid4().hex[:16]+'-01'})
        if actual.status_code!=500:raise RuntimeError('Expected explicit drill route500; verify drills are enabled')
        conn=await asyncpg.connect(os.environ['AIOPS_DATABASE_URL'],timeout=4)
        try:
            events=[]
            for _ in range(60):
                rows=await conn.fetch('''SELECT event_id,kind,payload FROM agent_system_diagnostic_events
                    WHERE tenant=$1 AND observed_id=$2 AND service=$3 AND env='dev' AND occurred_at>=$4
                    AND payload#>>'{correlation,trace_id}'=$5 ORDER BY seq''',deployment['tenant'],oid,service,started,trace)
                events=[{'event_id':r['event_id'],'kind':r['kind'],'row':json.loads(r['payload']) if isinstance(r['payload'],str) else r['payload']} for r in rows]
                if any(r['kind']=='sql_error' for r in events) and any(r['kind']=='http_request' for r in events):break
                await asyncio.sleep(.5)
            else:raise RuntimeError('Java SDK events did not reach platform archive; do not invent evidence')
        finally:await conn.close()
        save('sdk-events.json',events)
        fp='java-example-alert-'+uuid4().hex
        notification={'source':'springboot-example-monitor-simulator','auto_diagnose':True,'version':'4','status':'firing','receiver':'aiops-demo',
            'alerts':[{'fingerprint':fp,'status':'firing','startsAt':started.isoformat(),
                'labels':{'observed_id':oid,'service':service,'env':'dev','alertname':'DatabaseQueryFailed','severity':'warning'},
                'annotations':{'summary':service+' 的数据库查询失败',
                    'description':f'开发演练 GET /drills/missing-table 返回500。trace_id={trace}；请用采集数据核实失败机制和原因，不执行修复。',
                    'time_window':'5m'}}]}
        save('notification.json',notification)
        response=await agent.post(base+'/v1/alerts',json=notification)
        if response.status_code!=202:raise RuntimeError('Alert reception failed: HTTP '+str(response.status_code))
        ack=response.json();save('accepted.json',ack)
        if ack['accepted']!=1:raise RuntimeError('Alert was not admitted; inspect receipt')
        identity=ack['results'][0];iid=identity['incident_id'];sid=identity['session_id']
        print(json.dumps({'stage':'accepted','observed_id':oid,'incident_id':iid,'session_id':sid},ensure_ascii=False),flush=True)
        duplicate=(await agent.post(base+'/v1/alerts',json=notification)).json();save('duplicate.json',duplicate)
        assert duplicate['suppressed']==1 and duplicate['results'][0]['incident_id']==iid
        body=None
        for index in range(150):
            statuses=await agent.get(base+'/v1/observed-systems/'+oid+'/alerts')
            receipt=next((r for r in statuses.json().get('alerts',[]) if r['receipt_id']==identity['receipt_id']),None)
            if receipt and receipt['state'] in ('completed','failed','interrupted'):
                result=await agent.get(base+identity['incident_url'])
                if result.status_code==200:body=result.json();break
            if index%10==0:print(json.dumps({'stage':'waiting','state':receipt['state'] if receipt else 'not_readable','elapsed_seconds':index*2}),flush=True)
            await asyncio.sleep(2)
        if body is None:raise RuntimeError('Diagnosis has not completed within demo wait; existing alert is not resent')
        save('incident.json',body)
        if args.follow_up:
            follow=await agent.post(base+'/v1/chat',json={'session_id':sid,
                'message':'要定位原因，需要我提供什么数据？','user':{'id':'alert-demo-review'},
                'context':{'observed_id':oid,'env':'dev'}},timeout=90)
            if follow.status_code!=200:raise RuntimeError('Follow-up failed: HTTP '+str(follow.status_code))
            save('follow-up.json',follow.json())
        history=await agent.get(base+'/v1/observed-systems/'+oid+'/sessions/'+sid)
        if history.status_code!=200:raise RuntimeError('Saved alert conversation cannot be opened')
        save('conversation.json',history.json())
        metadata=body.get('explanation',{}).get('alert') or {}
        assert metadata.get('evidence_role')=='sender_claim_not_observation'
        assert body.get('explanation',{}).get('observed_system',{}).get('observed_id')==oid
        assert body['slots']['env']=='dev' and body['slots'].get('service_names')==[service]
        notification['status']='resolved';notification['alerts'][0].update(status='resolved',endsAt=datetime.now(timezone.utc).isoformat())
        resolved=(await agent.post(base+'/v1/alerts',json=notification)).json();save('resolved.json',resolved)
        assert resolved['accepted']==0 and resolved['resolved']==1 and resolved['results'][0]['incident_id']==iid
        final={'observed_id':oid,'service':service,'env':'dev','incident_id':iid,'session_id':sid,
            'conversation_url':base+identity['conversation_url'],'status':body['status'],'summary':body.get('explanation',{}).get('answer',{}).get('summary') or body.get('report',''),
            'evidence_count':len(body.get('evidence',[])),'sdk_events':len(events),
            'tool_steps':sum(len(t.get('tools',[])) for t in history.json()['turns']),'turns':len(history.json()['turns']),
            'actual_http_status':actual.status_code,'duplicate_suppressed':True,'resolved_without_new_diagnosis':True,
            'note':'resolved为模拟发送方通知；未修复或修改示例数据库；这是当前窗口中的历史样本'}
        save('result.json',final);print(json.dumps(final,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':asyncio.run(main())
