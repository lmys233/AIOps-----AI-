"""Source-specific reads stay inside the registered system, including graph/changes."""
import json
from urllib.parse import urlsplit
from datetime import datetime,timezone
from .context import current
from .guard import service_name
from ..tools.sources import RawOutcome


async def changes(params):
    from ..diagnostics.system_store import SystemDiagnosticStore
    scope=current();names=await scope.registered();name=service_name(params.get('service'))
    if name:names={name}
    store=SystemDiagnosticStore(scope.catalog.store.dsn);rows=[]
    try:
        for name in sorted(names):
            page=await store.query(scope.tenant,name,datetime.fromtimestamp(float(params['start']),timezone.utc).isoformat(),
                datetime.fromtimestamp(float(params['end']),timezone.utc).isoformat(),observed_id=scope.observed_id,
                env=scope.env,cluster=scope.cluster,kinds=['change_event'],limit=int(params.get('limit') or 50))
            for e in page['events']:
                p=e['payload'];rows.append({'change_id':e['event_id'],'entity_id':name,'changed_at':e['occurred_at'],
                    'changed_by':p.get('actor','sdk'),'payload_json':p,'status':'recorded','change_type':p.get('change_type','configuration')})
    finally:await store.close()
    return RawOutcome(data=rows,raw_ref='system_changes:'+scope.observed_id,note='Scoped retained configuration events; absence does not exclude changes')


async def topology(params):
    scope=current();names=await scope.registered();target=service_name(params['service'])
    rows=await scope.catalog.store.fetch('''SELECT service,base_url FROM agent_observed_instances
        WHERE tenant=$1 AND observed_id=$2 AND env=$3 AND active AND service=ANY($4::text[])''',scope.tenant,scope.observed_id,scope.env,sorted(names))
    origins={name:{r['base_url'] for r in rows if r['service']==name} for name in names}
    data=await scope.catalog.store.fetch('''SELECT DISTINCT ON(service) service,payload FROM agent_system_diagnostic_events
        WHERE tenant=$1 AND observed_id=$2 AND env=$3 AND service=ANY($4::text[]) AND kind='service_manifest'
        ORDER BY service,seq DESC''',scope.tenant,scope.observed_id,scope.env,sorted(names))
    edges=[]
    for r in data:
        for dest,url in json.loads(r['payload'])['payload'].get('downstream',{}).items():
            p=urlsplit(url);origin=p.scheme+'://'+p.netloc
            if dest in names and origin in origins.get(dest,set()):edges.append({'from_service':r['service'],'to_service':dest,'call_type':'http','criticality':'unknown'})
    found=[];front={target};seen={target};up=params.get('direction')=='upstream'
    for _ in range(min(3,int(params.get('depth') or 1))):
        selected=[e for e in edges if e['to_service' if up else 'from_service'] in front]
        found.extend(e for e in selected if e not in found);front={e['from_service' if up else 'to_service'] for e in selected}-seen;seen|=front
    return RawOutcome(data=found,raw_ref='system_topology:'+scope.observed_id+':'+target,
        note='Only registered targets with matching configured origins; declared graph, not a runtime dependency witness')
