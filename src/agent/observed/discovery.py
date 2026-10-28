"""Prometheus HTTP SD reads registered targets, never probes them for health."""
import os
from urllib.parse import urlsplit


async def targets(catalog,tenant):
    rows=await catalog.store.fetch('''SELECT DISTINCT ON(i.observed_id,i.service,i.base_url)
        i.observed_id,i.service,i.env,i.cluster,i.base_url FROM agent_observed_instances i
        JOIN agent_observed_services s USING(tenant,observed_id,service)
        JOIN agent_observed_systems o USING(tenant,observed_id)
        JOIN agent_observed_grants g ON i.grant_id=g.grant_id
        WHERE i.tenant=$1 AND i.active AND s.active AND o.active AND g.active
        AND i.capabilities @> '[{"id":"metrics","enabled":true}]'::jsonb
        ORDER BY i.observed_id,i.service,i.base_url,i.last_registered_at DESC''',tenant)
    out=[]
    for r in rows:
        p=urlsplit(r['base_url']);host=p.hostname
        gateway=os.getenv('AIOPS_PROMETHEUS_HOST_GATEWAY')
        if gateway and host in ('127.0.0.1','localhost','::1'):host=gateway
        address=host+(':'+str(p.port) if p.port else '')
        labels={'tenant':tenant,'observed_id':r['observed_id'],'service':r['service'],'env':r['env'],
            '__scheme__':p.scheme,'__metrics_path__':'/metrics'}
        if r['cluster']:labels['cluster']=r['cluster']
        out.append({'targets':[address],'labels':labels})
    return out
