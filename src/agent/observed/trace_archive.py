"""Scoped SDK span archive avoids shared Tempo trace-id collisions and metadata leakage."""
import json,re
from datetime import datetime,timezone
from .context import current
from ..diagnostics.system_store import SystemDiagnosticStore
from ..tools.sources import RawOutcome,QueryRejected


from ..queries.trace_language import validate_query,selected


async def spans(params,trace_id=None):
    scope=current();names=await scope.registered();store=SystemDiagnosticStore(scope.catalog.store.dsn)
    try:
        clauses=['tenant=$1','observed_id=$2','env=$3','service=ANY($4::text[])',"kind='trace_span'"]
        args=[scope.tenant,scope.observed_id,scope.env,sorted(selected(params.get('query',''),names))]
        if trace_id:
            if not re.fullmatch(r'[a-f0-9]{32}',trace_id):raise QueryRejected('invalid trace id')
            args.append(trace_id);clauses.append("payload#>>'{correlation,trace_id}'=$5")
        else:
            args.extend([datetime.fromtimestamp(float(params['start']),timezone.utc),datetime.fromtimestamp(float(params['end']),timezone.utc)])
            clauses.append('occurred_at BETWEEN $5 AND $6')
        if scope.cluster:
            args.append(scope.cluster);clauses.append('cluster=$'+str(len(args)))
        rows=await store.fetch('SELECT payload FROM agent_system_diagnostic_events WHERE '+ ' AND '.join(clauses)+' ORDER BY seq DESC LIMIT 2000',*args)
        return [json.loads(r['payload']) for r in rows]
    finally:await store.close()


def summary(rows,query,limit):
    kind=re.search(r'\bkind\s*=\s*(server|client|internal)',query)
    if kind:rows=[r for r in rows if r['payload']['kind'].lower()==kind[1]]
    grouped={}
    for r in rows:grouped.setdefault(r['correlation']['trace_id'],[]).append(r)
    traces=[];threshold=re.search(r'(?:span\.)?duration\s*>\s*([\d.]+)\s*(ms|s|m)',query)
    minimum=float(threshold[1])*{'ms':1,'s':1000,'m':60000}[threshold[2]] if threshold else 0
    for tid,members in grouped.items():
        if 'status' in query and '= error' in query and not any(r['payload']['status']=='ERROR' for r in members):continue
        root=min(members,key=lambda r:r['payload']['start_ns']);start=min(r['payload']['start_ns'] for r in members);end=max(r['payload']['end_ns'] for r in members)
        duration=(end-start)/1e6
        if duration<=minimum and minimum:continue
        traces.append({'trace_id':tid,'root_service':root['identity']['service'],'root_span':root['payload']['name'],
            'duration_ms':duration,'start_ns':str(start)})
    return sorted(traces,key=lambda t:t['duration_ms'],reverse=True)[:limit]


async def search(params):
    rows=await spans(params);scope=current()
    return RawOutcome(data=summary(rows,str(params['query']),int(params.get('limit') or 20)),
        raw_ref='system_trace_search:'+scope.observed_id+':'+str(params['start']),note='Scoped SDK span archive; retained/sample coverage, not a complete trace census')


async def fetch(params):
    rows=await spans({},str(params['trace_id']));scope=current();parsed=[]
    for row in rows:
        p=row['payload'];i=row['identity']
        parsed.append({'trace_id':row['correlation']['trace_id'],'span_id':p['span_id'],'parent_span_id':p.get('parent_span_id',''),
            'service':i['service'],'name':p['name'],'kind':p['kind'].lower(),'status':p['status'].lower(),
            'start_ns':p['start_ns'],'end_ns':p['end_ns'],'duration_ms':(p['end_ns']-p['start_ns'])/1e6,'attributes':p.get('attributes',{})})
    return RawOutcome(data={'trace_id':params['trace_id'],'spans':parsed,'truncated':len(rows)>=2000} if rows else {},raw_ref='system_trace:'+scope.observed_id+':'+str(params['trace_id']),
        note='Only current registered services in scoped archive; missing spans/parents are not inferred')
