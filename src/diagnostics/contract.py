"""Bounded JSON events shared by independently packaged SDK and AIOps."""
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 65536
QUALITY = ('available', 'unconfigured', 'unsupported', 'permission_denied',
           'query_failed', 'empty', 'partial')
IDENTITY_KEYS = ('tenant', 'env', 'cluster', 'service', 'instance', 'boot_id')
EVENT_KEYS = ('schema_version', 'event_id', 'observation_id', 'identity', 'kind',
              'source', 'occurred_at', 'captured_at', 'coverage', 'quality',
              'correlation', 'payload', 'raw_ref')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    if not isinstance(value,str):raise ValueError('timestamp requires an ISO string')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return stamp.astimezone(timezone.utc)


def identifier(value, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(r'[\w.:/-]{1,128}', value):
        raise ValueError('invalid identity or identifier')
    if '://' in value:
        raise ValueError('identity must not contain a connection URL')


def validate_quality(quality, coverage):
    if set(quality) != {'status', 'reason', 'sampled', 'truncated', 'dropped'}:
        raise ValueError('invalid quality fields')
    if quality['status'] not in QUALITY:
        raise ValueError('unknown quality state')
    if not isinstance(quality['reason'], str) or len(quality['reason']) > 1000:
        raise ValueError('invalid quality reason')
    for key in ('sampled', 'truncated'):
        if type(quality[key]) is not bool:
            raise ValueError('quality flags must be boolean')
    if type(quality['dropped']) is not int or quality['dropped'] < 0:
        raise ValueError('invalid dropped count')
    if set(coverage) != {'start', 'end', 'complete'} or type(coverage['complete']) is not bool:
        raise ValueError('invalid coverage')
    if timestamp(coverage['start']) > timestamp(coverage['end']):
        raise ValueError('coverage end precedes start')
    limited = quality['sampled'] or quality['truncated'] or quality['dropped'] > 0
    if coverage['complete'] and (limited or quality['status'] not in ('available', 'empty')):
        raise ValueError('incomplete source cannot claim complete coverage')


def validate_event(row):
    if not isinstance(row, dict) or set(row) != set(EVENT_KEYS):
        raise ValueError('invalid event fields')
    if type(row['schema_version']) is not int or row['schema_version'] not in (1,2):
        raise ValueError('unsupported schema version')
    for name in ('event_id', 'observation_id', 'kind', 'source'):
        identifier(row[name])
    identity = row['identity']
    expected=set(IDENTITY_KEYS)|({'observed_id'} if row['schema_version']==2 else set())
    if set(identity) != expected:
        raise ValueError('invalid identity fields')
    for name in IDENTITY_KEYS:
        identifier(identity[name], optional=name == 'cluster')
    if row['schema_version']==2:
        from .observed import system_id
        system_id(identity['observed_id'])
    for name in ('occurred_at', 'captured_at'):
        timestamp(row[name])
    validate_quality(row['quality'], row['coverage'])
    if not isinstance(row['correlation'], dict) or len(row['correlation']) > 12:
        raise ValueError('invalid correlation')
    for key, value in row['correlation'].items():
        identifier(key); identifier(value)
    if not isinstance(row['payload'], dict) or len(row['payload']) > 64:
        raise ValueError('invalid payload')
    validate_payload(row['kind'],row['payload'])
    if row['kind']=='trace_span' and not re.fullmatch(r'[a-f0-9]{32}',row['correlation'].get('trace_id','')):
        raise ValueError('invalid trace id')
    if not isinstance(row['raw_ref'], str) or len(row['raw_ref']) > 1000:
        raise ValueError('invalid raw reference')
    if len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode()) > MAX_EVENT_BYTES:
        raise ValueError('event size exceeds limit')
    return row


def validate_payload(kind,payload):
    from .cancellation import validate as validate_cancellation
    validate_cancellation(kind,payload)
    for key in ('execution_ms','total_ms','elapsed_ms','hold_ms','wait_ms','run_ms','duration_ms'):
        value=payload.get(key)
        if value is not None and (type(value) not in (int,float) or value<0):raise ValueError('invalid timing '+key)
    for key in ('capacity','queue_capacity','active','queued','in_use','server_pid','cancel_count_before','cancel_count'):
        value=payload.get(key)
        if value is not None and (type(value) is not int or value<0):raise ValueError('invalid count '+key)
    if kind=='http_request' and payload.get('status') is not None:
        if type(payload['status']) is not int or not 100<=payload['status']<=599:raise ValueError('invalid HTTP status')
    if kind=='trace_span':
        if not isinstance(payload.get('name'),str) or len(payload['name'])>512:raise ValueError('invalid span name')
        for k in ('start_ns','end_ns'):
            if type(payload.get(k)) is not int or payload[k]<0:raise ValueError('invalid span time')
        if payload['end_ns']<payload['start_ns']:raise ValueError('inverted span time')
        if not re.fullmatch(r'[a-f0-9]{16}',payload.get('span_id','')):raise ValueError('invalid span id')
        if payload.get('parent_span_id') and not re.fullmatch(r'[a-f0-9]{16}',payload['parent_span_id']):raise ValueError('invalid parent span')
        if payload.get('kind') not in ('INTERNAL','SERVER','CLIENT','PRODUCER','CONSUMER'):raise ValueError('invalid span kind')
        if payload.get('status') not in ('UNSET','OK','ERROR'):raise ValueError('invalid span status')


def event(identity, kind, source, payload, *, correlation=None, observation_id=None,
          occurred_at=None, coverage=None, quality=None, raw_ref=''):
    now = utc_now(); at = occurred_at or now
    row = dict(schema_version=2 if 'observed_id' in identity else SCHEMA_VERSION, event_id=uuid4().hex,
               observation_id=observation_id or uuid4().hex, identity=dict(identity),
               kind=kind, source=source, occurred_at=at, captured_at=now,
               coverage=coverage or dict(start=at, end=at, complete=True),
               quality=quality or dict(status='available', reason='', sampled=False,
                                      truncated=False, dropped=0),
               correlation=dict(correlation or {}), payload=payload, raw_ref=raw_ref)
    return validate_event(row)
