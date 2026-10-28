"""Admin enrollment is an explicit authorization, not trust in a submitted system name."""
import json
from uuid import uuid4
from diagnostics.contract import identifier
from diagnostics.observed import base_url,system_id


def validate(data):
    if not isinstance(data,dict) or set(data)-{'tenant','observed_name','observed_id','services','origins','envs','source_types'}:
        raise ValueError('invalid enrollment fields')
    for k in ('tenant','observed_name'):identifier(data[k])
    for field in ('services','envs'):
        values=data[field]
        if not isinstance(values,list) or not 1<=len(values)<=128:raise ValueError('invalid enrollment scope')
        for value in values:identifier(value)
    if not isinstance(data['origins'],list) or not 1<=len(data['origins'])<=128:raise ValueError('invalid origins')
    data=dict(data,origins=[base_url(u) for u in data['origins']])
    if data.get('observed_id'):system_id(data['observed_id'])
    from .ingestion import SDK_SOURCES
    permitted=SDK_SOURCES|{'database_observer','migration','ddl_audit','query_plan','human_verification','process_observer','platform_event'}
    sources=data.get('source_types',sorted(SDK_SOURCES))
    if not isinstance(sources,list) or not sources or not set(sources)<=permitted:raise ValueError('unsupported source types')
    data['source_types']=sources
    return data


async def issue(store,credentials,data):
    d=validate(data);oid=d.get('observed_id') or 'obs_'+uuid4().hex;gid='enr_'+uuid4().hex
    pool=await store.connect()
    async with pool.acquire() as c:
        async with c.transaction():
            if d.get('observed_id'):
                system=await c.fetchrow('SELECT * FROM agent_observed_systems WHERE tenant=$1 AND observed_id=$2 AND active',d['tenant'],oid)
                if not system or system['name']!=d['observed_name']:raise PermissionError('enrollment system mismatch')
            else:
                await c.execute('INSERT INTO agent_observed_systems(tenant,observed_id,name) VALUES($1,$2,$3)',d['tenant'],oid,d['observed_name'])
            await c.execute('INSERT INTO agent_observed_grants(grant_id,tenant,observed_id,services,origins,envs) VALUES($1,$2,$3,$4::jsonb,$5::jsonb,$6::jsonb)',
                gid,d['tenant'],oid,json.dumps(d['services']),json.dumps(d['origins']),json.dumps(d['envs']))
            await c.execute('UPDATE agent_observed_grants SET source_types=$2::jsonb WHERE grant_id=$1',gid,json.dumps(d['source_types']))
            await store.audit(c,d['tenant'],oid,'issue_enrollment',{'grant_id':gid})
    return {'grant_id':gid,'observed_id':oid,'registration_token':credentials.token(gid)}
