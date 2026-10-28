"""Structured attempts are observations, never an automatic outage diagnosis."""
import json
from .contract import identifier, timestamp
from .observed import scoped_identity

ERRORS=('none','connect_timeout','read_timeout','connection_refused','dns_error',
    'connection_error','http_5xx','unauthorized','forbidden','not_found','rate_limited',
    'invalid_params','unavailable','budget_stop','policy_denied','cancelled','internal_error')
FIELDS={'schema_version','call_id','attempt_id','incident_id','observer_id','identity',
    'tool','interface_id','transport','attempt','sent','started_at','finished_at',
    'elapsed_ms','timeout_sec','params','result','ok','error','http_status','raw_ref'}


def validate_attempt(row):
    if not isinstance(row,dict) or set(row)!=FIELDS:raise ValueError('invalid attempt fields')
    if type(row['schema_version']) is not int or row['schema_version']!=1:raise ValueError('invalid attempt version')
    scoped_identity(row['identity'])
    for key in ('call_id','attempt_id','observer_id','tool'):identifier(row[key])
    for key in ('incident_id','interface_id'):
        if row[key]:identifier(row[key])
    if row['transport'] not in ('sdk_direct','business_probe','metrics','logs','traces','archive','other'):
        raise ValueError('invalid attempt transport')
    if type(row['attempt']) is not int or row['attempt']<1:raise ValueError('invalid attempt number')
    if type(row['sent']) is not bool or type(row['ok']) is not bool:raise ValueError('invalid attempt outcome')
    if row['error'] not in ERRORS or row['ok']!=(row['error']=='none'):raise ValueError('inconsistent error')
    if not row['sent'] and row['error'] not in ('budget_stop','policy_denied','invalid_params','unavailable','cancelled'):
        raise ValueError('unsent request cannot claim transport failure')
    if timestamp(row['started_at'])>timestamp(row['finished_at']):raise ValueError('inverted attempt time')
    for key in ('elapsed_ms','timeout_sec'):
        if type(row[key]) not in (int,float) or row[key]<0:raise ValueError('invalid timing')
    if row['http_status'] is not None and (type(row['http_status']) is not int or not 100<=row['http_status']<=599):
        raise ValueError('invalid HTTP status')
    if not isinstance(row['params'],dict) or not isinstance(row['result'],dict):raise ValueError('parameters/results must be structured')
    if not isinstance(row['raw_ref'],str) or len(row['raw_ref'])>1000:raise ValueError('invalid raw reference')
    if len(json.dumps(row,allow_nan=False).encode())>262144:raise ValueError('attempt too large')
    return row
