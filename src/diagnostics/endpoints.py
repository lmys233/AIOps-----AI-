"""Business route inventory, independent of SDK/framework/platform imports."""
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import unquote

MAX_ENDPOINTS = 256
METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}


def route_path(value):
    if not isinstance(value, str) or len(value) > 512 or not value.startswith('/'):
        raise ValueError('invalid business route')
    decoded = unquote(value)
    if any(s in decoded for s in ('\\', '?', '#', '://', '//')) or any(p in ('.', '..') for p in decoded.split('/')):
        raise ValueError('unsafe business route')
    if any(ord(c) < 32 for c in value) or decoded.startswith('/_aiops/'):
        raise ValueError('reserved business route')
    return value


def endpoint_id(method, path):
    return 'ep_' + hashlib.sha256((method + ' ' + path).encode()).hexdigest()[:24]


def endpoint(value):
    fields = {'id', 'method', 'path', 'description', 'parameters', 'body_schema', 'policy'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid endpoint fields')
    method, path = value['method'], route_path(value['path'])
    if method not in METHODS or value['id'] != endpoint_id(method, path):
        raise ValueError('invalid endpoint identity')
    if not isinstance(value['description'], str) or len(value['description']) > 1000:
        raise ValueError('invalid endpoint description')
    parameters = value['parameters']
    if not isinstance(parameters, list) or len(parameters) > 32:
        raise ValueError('too many endpoint parameters')
    for p in parameters:
        if set(p) != {'name', 'in', 'required', 'schema'} or p['in'] not in ('path', 'query'):
            raise ValueError('unsupported endpoint parameter')
        if not re.fullmatch(r'[A-Za-z_][\w.-]{0,63}', p['name']) or type(p['required']) is not bool or not isinstance(p['schema'], dict):
            raise ValueError('invalid parameter schema')
    if len({(p['in'], p['name']) for p in parameters}) != len(parameters):
        raise ValueError('duplicate parameters')
    if value['body_schema'] is not None and not isinstance(value['body_schema'], dict):
        raise ValueError('invalid body schema')
    policy = value['policy']
    if not isinstance(policy, dict) or set(policy) != {'effect', 'probe', 'load', 'max_requests', 'max_concurrency', 'max_duration_sec', 'max_rps'}:
        raise ValueError('invalid endpoint policy')
    if policy['effect'] not in ('unknown', 'read_only', 'write') or type(policy['probe']) is not bool or type(policy['load']) is not bool:
        raise ValueError('invalid endpoint effect')
    for key, upper in [('max_requests', 200), ('max_concurrency', 16), ('max_duration_sec', 30), ('max_rps', 50)]:
        if type(policy[key]) is not int or not 1 <= policy[key] <= upper:
            raise ValueError('invalid probe limit: ' + key)
    if policy['effect'] == 'unknown' and (policy['probe'] or policy['load']) or policy['load'] and not policy['probe']:
        raise ValueError('probe policy requires an explicit effect')
    # Schemas are data, not remote references or instructions. Reject external refs.
    def refs(obj):
        if isinstance(obj, dict):
            if '$ref' in obj: raise ValueError('route schemas must be locally resolved')
            for v in obj.values(): refs(v)
        elif isinstance(obj, list):
            for v in obj: refs(v)
    refs(parameters); refs(value['body_schema'])
    if len(json.dumps(value, allow_nan=False).encode()) > 16384:
        raise ValueError('endpoint too large')
    return deepcopy(value)


def inventory(values):
    if not isinstance(values, list) or len(values) > MAX_ENDPOINTS:
        raise ValueError('endpoint inventory exceeds supported limit')
    rows = [endpoint(v) for v in values]
    if len({r['id'] for r in rows}) != len(rows): raise ValueError('duplicate endpoint')
    return rows


def revision(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def disabled_policy():
    return dict(effect='unknown', probe=False, load=False, max_requests=20,
                max_concurrency=4, max_duration_sec=10, max_rps=5)
