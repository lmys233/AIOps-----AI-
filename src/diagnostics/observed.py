"""D6 wire contract. Standard library only; no SDK/Agent dependency or network I/O."""
from copy import deepcopy
import json
import re
from urllib.parse import urlsplit
from .contract import identifier, timestamp

CAPABILITIES = {
    'probe_observations': '/_aiops/probe-observations',
    'capabilities': '/_aiops/capabilities', 'snapshot': '/_aiops/snapshot',
    'metrics': '/metrics', 'health': '/health', 'services': '/services', 'changes': '/changes'}
REGISTER_FIELDS = {'schema_version', 'observed_name', 'observed_id', 'service', 'instance',
    'boot_id', 'env', 'cluster', 'base_url', 'sdk_version', 'capabilities'}


def system_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'obs_[a-f0-9]{32}',value):
        raise ValueError('invalid observed_id')
    return value


def base_url(value):
    if not isinstance(value,str) or len(value)>512:raise ValueError('invalid base_url')
    p=urlsplit(value)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
        raise ValueError('base_url requires HTTP(S) without credentials')
    if p.path not in ('','/') or p.query or p.fragment:raise ValueError('base_url must be an origin')
    if p.hostname.lower() in ('169.254.169.254','metadata.google.internal'):
        raise ValueError('metadata target forbidden')
    if p.port is not None and not 1<=p.port<=65535:raise ValueError('invalid port')
    return value.rstrip('/')


def capability(value):
    if not isinstance(value,dict) or set(value)!={'id','method','path','version','enabled'}:
        raise ValueError('invalid capability fields')
    if value['id'] not in CAPABILITIES or value['path']!=CAPABILITIES[value['id']]:
        raise ValueError('unknown capability/path')
    if value['method']!='GET' or type(value['version']) is not int or value['version']!=1:
        raise ValueError('unsupported read protocol')
    if type(value['enabled']) is not bool:raise ValueError('invalid enabled flag')
    return deepcopy(value)


def registration(value):
    from .endpoints import inventory, revision
    if not isinstance(value,dict):raise ValueError('invalid registration fields')
    version=value.get('schema_version')
    extra={'endpoints','business_url','endpoint_status','endpoint_revision'} if version==2 else set()
    if set(value)!=REGISTER_FIELDS|extra:raise ValueError('invalid registration fields')
    if type(version) is not int or version not in (1,2):raise ValueError('unsupported registration version')
    if version==2:
        inventory(value['endpoints']);base_url(value['business_url'])
        if value['endpoint_revision']!=revision(value['endpoints']):raise ValueError('endpoint revision mismatch')
        if value['endpoint_status'] not in ('complete','configured','unsupported') and not value['endpoint_status'].startswith('unavailable:'):
            raise ValueError('invalid endpoint status')
    for field in ('observed_name','service','instance','boot_id','env','sdk_version'):
        identifier(value[field])
    identifier(value['cluster'],optional=True)
    if value['observed_id'] is not None:system_id(value['observed_id'])
    origin=base_url(value['base_url']);caps=value['capabilities']
    if not isinstance(caps,list) or len(caps)>len(CAPABILITIES):raise ValueError('too many capabilities')
    checked=[capability(c) for c in caps]
    if len({c['id'] for c in checked})!=len(checked):raise ValueError('duplicate capability')
    if len(json.dumps(value,allow_nan=False).encode())>(524288 if version==2 else 16384):raise ValueError('registration too large')
    return dict(deepcopy(value),base_url=origin,capabilities=checked)


def scoped_identity(value):
    fields={'tenant','observed_id','service','env','cluster','instance','boot_id'}
    if not isinstance(value,dict) or set(value)!=fields:raise ValueError('invalid scoped identity')
    system_id(value['observed_id'])
    for name in fields-{'observed_id'}:identifier(value[name],optional=name=='cluster')
    return deepcopy(value)


def ownership(expected,actual):
    """An explicit archive reference never inherits ownership from its caller."""
    if any(not expected.get(k) or expected[k]!=actual.get(k) for k in ('tenant','observed_id')):
        raise PermissionError('system scope mismatch')
    if expected.get('service') and expected['service']!=actual.get('service'):
        raise PermissionError('service scope mismatch')
    return True
