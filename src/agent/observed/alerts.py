"""Alert labels identify the registered system; same fingerprint never merges systems."""
from copy import deepcopy
import hashlib,json,os
from .context import Scope,enabled
from ..api.errors import InvalidAlerts


async def scoped_alerts(catalog,alerts):
    if not enabled() and not any((a.get('labels') or {}).get('observed_id') for a in alerts):return alerts
    if not catalog:raise InvalidAlerts('注册中心未接入，未启动告警查询')
    result=[]
    for raw in alerts:
        labels=raw.get('labels') or {};oid=labels.get('observed_id');service=labels.get('service')
        if not oid or not service:raise InvalidAlerts('告警需包含 observed_id 和已注册 service')
        try:
            scope=Scope(catalog,os.getenv('AIOPS_TENANT','default'),oid,labels.get('env') or 'prod',labels.get('cluster') or None)
            if service not in await scope.registered():raise PermissionError('unregistered')
        except Exception:raise InvalidAlerts('告警系统/服务未登记或范围不可核实，未启动查询') from None
        item=deepcopy(raw);fingerprint=str(raw.get('fingerprint') or '')
        # Explicit sender fingerprints identify an alert independently of changing severity/annotations.
        identity=[oid,service,scope.env,scope.cluster,fingerprint]
        item['fingerprint']=hashlib.sha256(json.dumps(identity,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        result.append(item)
    return result
