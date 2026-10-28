"""Shared cancellation evidence vocabulary; no framework or Agent dependency."""
from .contract import identifier,timestamp

KINDS=('request_disconnect','request_deadline','request_cancel','request_shutdown_cancel')
OPERATIONS=('application','connection_acquire','database_query','downstream_call','response_send','unknown')


def validate(kind,payload):
    if kind not in KINDS:return
    for key in ('trigger_event_id','deadline_id','source_record'):
        if payload.get(key) is not None:identifier(payload[key])
    for key in ('request_started_at','configured_at','deadline_at'):
        if payload.get(key) is not None:timestamp(payload[key])
    for key in ('accepted','expired','translated_to_timeout','received_after_response'):
        if key in payload and type(payload[key]) is not bool:raise ValueError('invalid cancellation flag '+key)
    if payload.get('operation','unknown') not in OPERATIONS:raise ValueError('invalid cancellation operation')
    if kind=='request_disconnect' and type(payload.get('received_after_response')) is not bool:
        raise ValueError('disconnect completion state required')
    if kind=='request_deadline':
        if payload.get('phase') not in ('configured','expired','finished'):raise ValueError('invalid deadline phase')
        identifier(payload.get('deadline_id'));timestamp(payload.get('configured_at',''))
        if type(payload.get('expired')) is not bool:raise ValueError('deadline expired flag required')
        if payload['expired']:timestamp(payload.get('deadline_at',''))
        if payload.get('translated_to_timeout') and not payload['expired']:raise ValueError('timeout without expired deadline')
    if kind=='request_cancel':
        if payload.get('origin') not in ('client_disconnect','shutdown','unknown'):raise ValueError('invalid cancellation origin')
        if type(payload.get('accepted')) is not bool:raise ValueError('actual cancel result required')
        identifier(payload.get('source_record'))
    if kind=='request_shutdown_cancel':
        if payload.get('adapter')!='uvicorn_cancel_branch':raise ValueError('unsupported shutdown witness')
        identifier(payload.get('source_record'))
        if type(payload.get('cancel_count_before')) is not int:raise ValueError('shutdown cancellation count required')
