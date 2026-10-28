"""Request-owned scope propagates to agents via ContextVar, never a process-global choice."""
from contextvars import ContextVar
from dataclasses import dataclass,field
from contextlib import contextmanager
import hashlib,os
from diagnostics.contract import identifier
from diagnostics.observed import system_id

CURRENT=ContextVar('aiops_observed_scope',default=None)


def enabled():return os.getenv('AIOPS_MULTI_SYSTEM','off').lower() in ('on','true','1')


@dataclass(frozen=True)
class Scope:
    catalog: object
    tenant: str
    observed_id: str
    env: str='prod'
    cluster: str|None=None
    services: tuple=()
    names: tuple=()
    observed_name: str=''
    incident_ids: set=field(default_factory=set,compare=False,repr=False)
    attempts: list=field(default_factory=list,compare=False,repr=False)
    sdk_access_checks: dict=field(default_factory=dict,compare=False,repr=False)

    def __post_init__(self):
        system_id(self.observed_id);identifier(self.tenant);identifier(self.env)

    @property
    def namespace(self):return 'scope_'+hashlib.sha256((self.tenant+'|'+self.observed_id).encode()).hexdigest()

    async def registered(self):
        rows=await self.catalog.store.fetch('''SELECT DISTINCT i.service FROM agent_observed_instances i
            JOIN agent_observed_services s USING(tenant,observed_id,service)
            JOIN agent_observed_systems o USING(tenant,observed_id)
            JOIN agent_observed_grants g ON i.grant_id=g.grant_id
            WHERE i.tenant=$1 AND i.observed_id=$2 AND i.env=$3 AND ($4::text IS NULL OR i.cluster=$4)
            AND i.active AND s.active AND o.active AND g.active''',self.tenant,self.observed_id,self.env,self.cluster)
        names={r['service'] for r in rows}
        if self.services:
            if not set(self.services)<=names:raise PermissionError('requested service is unregistered or disabled')
            names=set(self.services)
        if not names:raise PermissionError('system has no registered services in requested scope')
        return names


@contextmanager
def use(scope):
    token=CURRENT.set(scope)
    try:yield scope
    finally:CURRENT.reset(token)


def current():return CURRENT.get()


def session_key(value):
    scope=current()
    if not scope:return value
    prefix=scope.namespace+':'
    return value if value.startswith(prefix) else prefix+value


def body_scope(body):return ((body.get('explanation') or {}).get('observed_system') or {}).get('observed_id')


def stamp(body):
    scope=current()
    if not scope:return body
    if body_scope(body) not in (None,scope.observed_id):raise PermissionError('response scope mismatch')
    ex=body.get('explanation') or {}
    for p in (ex.get('attribution_v2') or {}).get('problems') or []:
        if p.get('observed_id') not in (None,'',scope.observed_id):raise PermissionError('problem belongs to another system')
    body['slots']=body.get('slots') or {};body['slots']['observed_id']=scope.observed_id
    body['explanation']=ex
    ex['observed_system']={'tenant':scope.tenant,'observed_id':scope.observed_id,
        'observed_name':scope.observed_name,'env':scope.env,'cluster':scope.cluster,'registered_services':list(scope.names)}
    if scope.attempts:body['explanation']['tool_attempts']=scope.attempts[-100:]
    d=body['explanation'].get('dialogue')
    if d is not None:d['observed_id']=scope.observed_id
    return body
