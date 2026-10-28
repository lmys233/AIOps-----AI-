"""Enrollment authenticated events: registered instances only, before archive/relay."""
from diagnostics.contract import validate_event,timestamp
from .register import authenticate

SDK_SOURCES={'sdk_http','sdk_sql','sdk_pool','sdk_worker','sdk_runtime','sdk_lifecycle',
    'sdk_deadline','sdk_shutdown','sdk_log','sdk_trace','sdk_transaction','sdk_lock'}
INDEPENDENT_KINDS={'db_ddl','db_migration','db_plan','verified_conditions'}


async def grant_for_request(app,request):
    from .routes import bearer
    catalog=app.state.observed_catalog
    if catalog is None:raise PermissionError('registry not configured')
    return await authenticate(catalog.store,app.state.observed_credentials,bearer(request))


async def authorize(catalog,grant,rows):
    if not rows or len(rows)>32:raise ValueError('event batch bound exceeded')
    for row in rows:
        validate_event(row);i=row['identity']
        if row['schema_version']!=2 or (i['tenant'],i['observed_id'])!=(grant['tenant'],grant['observed_id']):
            raise PermissionError('event system scope mismatch')
        if i['service'] not in grant['services'] or i['env'] not in grant['envs']:
            raise PermissionError('event service scope mismatch')
        if row['source'] not in grant['source_types'] or row['source'] in SDK_SOURCES and row['kind'] in INDEPENDENT_KINDS:
            raise PermissionError('source cannot claim independent proof')
        found=await catalog.store.one('''SELECT i.base_url FROM agent_observed_instances i
            JOIN agent_observed_services s USING(tenant,observed_id,service)
            JOIN agent_observed_systems o USING(tenant,observed_id)
            JOIN agent_observed_grants g ON i.grant_id=g.grant_id
            WHERE i.tenant=$1 AND i.observed_id=$2 AND i.service=$3 AND i.instance=$4 AND i.boot_id=$5
            AND i.env=$6 AND i.cluster IS NOT DISTINCT FROM $7 AND (i.grant_id=$8 OR $9::boolean)
            AND i.active AND s.active AND o.active AND g.active''',
            i['tenant'],i['observed_id'],i['service'],i['instance'],i['boot_id'],i['env'],i['cluster'],grant['grant_id'],row['source'] not in SDK_SOURCES)
        if not found:raise PermissionError('event instance unregistered or disabled')
        from datetime import datetime,timezone,timedelta
        if timestamp(row['occurred_at'])>datetime.now(timezone.utc)+timedelta(minutes=5):raise ValueError('event timestamp too far in future')
    return True
