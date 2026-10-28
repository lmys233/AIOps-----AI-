"""Bounded retention prefers observed errors; it cannot guarantee no loss."""


def priority(row):
    kind=row.get('kind','')
    if kind=='request_deadline':return 2 if row.get('payload',{}).get('expired') else 0
    if kind in ('request_disconnect','request_cancel','request_shutdown_cancel'):return 2
    if kind=='http_request' and isinstance(row.get('payload',{}).get('status'),int) and row['payload']['status']>=500:return 2
    if any(word in kind for word in ('error','timeout','reject','exit','failed')):return 2
    if row.get('payload',{}).get('outcome') in ('error','exception','server_error','cancelled','timeout'):return 2
    if row.get('payload',{}).get('execution_ms',0)>500:return 1
    if row.get('payload',{}).get('total_ms',0)>1000:return 1
    if row.get('payload',{}).get('elapsed_ms',0)>500:return 1
    if kind in ('request_phase','capability_report','runtime_snapshot','sdk_heartbeat'):return 1
    if row.get('payload',{}).get('lock_refs'):return 1
    if kind.startswith(('pool_','worker_','transaction_')):return 1
    return 0
