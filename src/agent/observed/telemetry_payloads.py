"""Fixed Loki/OTLP messages from authenticated SDK rows, not arbitrary destinations."""
import json
from diagnostics.contract import timestamp


def labels(row):
    i=row['identity']
    return {k:str(i[k]) for k in ('tenant','observed_id','service','env')}


def loki(row):
    i=row['identity'];body=dict(row['payload']);body.update(row['correlation']);body.update(labels(row))
    body.update(event_id=row['event_id'],observation_id=row['observation_id'],source=row['source'],instance=i['instance'],boot_id=i['boot_id'])
    ns=str(int(timestamp(row['occurred_at']).timestamp()*1e9))
    return {'streams':[{'stream':labels(row),'values':[[ns,json.dumps(body,ensure_ascii=False)]]}]}


def otlp(row):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    body=ExportTraceServiceRequest();r=body.resource_spans.add();i=row['identity'];p=row['payload']
    for key,value in {'service.name':i['service'],'aiops.observed_id':i['observed_id'],
        'aiops.tenant':i['tenant'],'deployment.environment':i['env']}.items():
        a=r.resource.attributes.add();a.key=key;a.value.string_value=value
    span=r.scope_spans.add().spans.add();span.name=p['name']
    span.trace_id=bytes.fromhex(row['correlation']['trace_id']);span.span_id=bytes.fromhex(p['span_id'])
    if p.get('parent_span_id'):span.parent_span_id=bytes.fromhex(p['parent_span_id'])
    span.start_time_unix_nano=p['start_ns'];span.end_time_unix_nano=p['end_ns']
    span.kind={'INTERNAL':1,'SERVER':2,'CLIENT':3,'PRODUCER':4,'CONSUMER':5}.get(p['kind'],1)
    span.status.code={'UNSET':0,'OK':1,'ERROR':2}.get(p['status'],0)
    for key,value in p.get('attributes',{}).items():
        a=span.attributes.add();a.key=key
        if isinstance(value,int):a.value.int_value=value
        else:a.value.string_value=str(value)
    return body.SerializeToString()
