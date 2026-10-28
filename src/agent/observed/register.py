"""Transactional service registration; replay does not create another system or boot."""
import json
from diagnostics.observed import registration
from .store import public


async def authenticate(store,credentials,token):
    gid=credentials.verify(token)
    grant=await store.one('SELECT * FROM agent_observed_grants WHERE grant_id=$1 AND active',gid)
    if not grant:raise PermissionError('enrollment revoked or unknown')
    return grant


async def register(store,credentials,token,data):
    r=registration(data);g=await authenticate(store,credentials,token);oid=g['observed_id']
    if r['observed_id'] not in (None,oid) or r['service'] not in g['services'] or r['env'] not in g['envs'] or r['base_url'] not in g['origins']:
        raise PermissionError('registration scope mismatch')
    if r.get('business_url',r['base_url']) not in g['origins']:raise PermissionError('business origin outside enrollment')
    pool=await store.connect()
    async with pool.acquire() as c:
        async with c.transaction():
            sys=await c.fetchrow('SELECT * FROM agent_observed_systems WHERE tenant=$1 AND observed_id=$2 FOR UPDATE',g['tenant'],oid)
            current=await c.fetchrow('SELECT active FROM agent_observed_grants WHERE grant_id=$1 FOR SHARE',g['grant_id'])
            if not current or not current['active'] or not sys['active'] or sys['name']!=r['observed_name']:
                raise PermissionError('system inactive or name mismatch')
            await c.execute('INSERT INTO agent_observed_services(tenant,observed_id,service) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',g['tenant'],oid,r['service'])
            active=await c.fetchval('SELECT active FROM agent_observed_services WHERE tenant=$1 AND observed_id=$2 AND service=$3 FOR UPDATE',g['tenant'],oid,r['service'])
            if not active:raise PermissionError('service disabled')
            await unique_origin(c,g,r)
            if r.get('business_url') and r['business_url']!=r['base_url']:
                await unique_origin(c,g,{**r,'base_url':r['business_url']})
            await save_instance(c,g,r)
            await c.execute('UPDATE agent_observed_systems SET registered_at=coalesce(registered_at,now()),last_registered_at=now(),revision=revision+1 WHERE tenant=$1 AND observed_id=$2',g['tenant'],oid)
            await store.audit(c,g['tenant'],oid,'register',{'service':r['service'],'instance':r['instance'],'boot_id':r['boot_id']})
            revision=await c.fetchval('SELECT revision FROM agent_observed_instances WHERE tenant=$1 AND observed_id=$2 AND service=$3 AND instance=$4 AND boot_id=$5',g['tenant'],oid,r['service'],r['instance'],r['boot_id'])
    return {'schema_version':1,'tenant':g['tenant'],'observed_id':oid,'service':r['service'],
        'instance':r['instance'],'boot_id':r['boot_id'],'revision':revision,'state':'registered_unverified'}


async def save_instance(c,g,r):
    old=await c.fetchrow('SELECT env,cluster FROM agent_observed_instances WHERE tenant=$1 AND observed_id=$2 AND service=$3 AND instance=$4 AND boot_id=$5',
        g['tenant'],g['observed_id'],r['service'],r['instance'],r['boot_id'])
    if old and (old['env']!=r['env'] or old['cluster']!=r['cluster']):raise PermissionError('boot identity cannot change environment or cluster')
    await c.execute('''INSERT INTO agent_observed_instances
        (tenant,observed_id,service,instance,boot_id,grant_id,env,cluster,base_url,sdk_version,capabilities)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
        ON CONFLICT(tenant,observed_id,service,instance,boot_id) DO UPDATE SET
        base_url=EXCLUDED.base_url,sdk_version=EXCLUDED.sdk_version,capabilities=EXCLUDED.capabilities,
        env=EXCLUDED.env,cluster=EXCLUDED.cluster,last_registered_at=now(),revision=agent_observed_instances.revision+1
        WHERE agent_observed_instances.grant_id=EXCLUDED.grant_id AND agent_observed_instances.active''',
        g['tenant'],g['observed_id'],r['service'],r['instance'],r['boot_id'],g['grant_id'],r['env'],r['cluster'],r['base_url'],r['sdk_version'],json.dumps(r['capabilities']))
    row=await c.fetchrow('SELECT grant_id,active FROM agent_observed_instances WHERE tenant=$1 AND observed_id=$2 AND service=$3 AND instance=$4 AND boot_id=$5',g['tenant'],g['observed_id'],r['service'],r['instance'],r['boot_id'])
    if row['grant_id']!=g['grant_id'] or not row['active']:raise PermissionError('instance owned by another enrollment or disabled')
    await c.execute('''UPDATE agent_observed_instances SET endpoints=$6::jsonb,business_url=$7,
        endpoint_revision=$8,endpoint_status=$9 WHERE tenant=$1 AND observed_id=$2 AND service=$3 AND instance=$4 AND boot_id=$5''',
        g['tenant'],g['observed_id'],r['service'],r['instance'],r['boot_id'],json.dumps(r.get('endpoints',[])),
        r.get('business_url'),r.get('endpoint_revision',''),r.get('endpoint_status','legacy_unavailable'))


async def unique_origin(c,g,r):
    await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',g['tenant']+'|'+r['base_url'])
    foreign=await c.fetchval('''SELECT 1 FROM agent_observed_instances i
        JOIN agent_observed_services s USING(tenant,observed_id,service)
        JOIN agent_observed_systems o USING(tenant,observed_id)
        WHERE i.tenant=$1 AND (i.base_url=$2 OR i.business_url=$2) AND i.observed_id<>$3 AND i.active AND s.active AND o.active LIMIT 1''',
        g['tenant'],r['base_url'],g['observed_id'])
    if foreign:raise PermissionError('origin already registered to another system')
