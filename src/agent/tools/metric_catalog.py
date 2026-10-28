"""Versioned, bounded metric metadata, without guessing units or quantiles."""
import base64
import hashlib
import json
from .sources import RawOutcome,QueryRejected

VERSION=1
MAX_PAGE=64
KINDS={'gauge','counter','histogram','summary','unknown','sampled_value'}

def checked(value,allowed=None):
    if not isinstance(value,dict) or value.get('schema_version')!=VERSION or value.get('kind')!='metric_catalog':
        raise ValueError('unsupported metric catalogue version or kind')
    rows=value.get('metrics')
    if not isinstance(rows,list) or len(rows)>MAX_PAGE:raise ValueError('metric catalogue page exceeds contract')
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('name'),str) or not 1<=len(row['name'])<=200:
            raise ValueError('invalid raw metric name')
        if row.get('type','unknown') not in KINDS:raise ValueError('invalid metric type')
        if row.get('origin','unknown') not in ('application','observation_path','unknown','mixed'):
            raise ValueError('invalid metric ownership')
        names=row.get('services')
        if not isinstance(names,list) or not names or len(names)>128 or not all(isinstance(s,str) for s in names):
            raise ValueError('metric must declare its service scope')
        if allowed is not None and not set(names)<=set(allowed):raise PermissionError('metric metadata exceeds registered service scope')
        if row.get('unit') is not None and (not isinstance(row['unit'],str) or len(row['unit'])>80):raise ValueError('invalid unit')
        for field in ('family','statistic'):
            if field in row and (not isinstance(row[field],str) or len(row[field])>200):raise ValueError('invalid metric metadata')
    if not isinstance(value.get('complete'),bool):raise ValueError('catalogue must declare coverage')
    if value.get('next_cursor') is not None and (not isinstance(value['next_cursor'],str) or len(value['next_cursor'])>512):
        raise ValueError('invalid catalogue cursor')
    return value

def page(rows,*,query,start,end,limit=MAX_PAGE,cursor=None,complete=True,notes=()):
    rows=sorted(rows,key=lambda r:(r['name'],tuple(r['services'])))
    snapshot=hashlib.sha256(json.dumps([query,start,end,rows],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    at=0
    if cursor:
        try:
            old=json.loads(base64.urlsafe_b64decode(cursor));at=old['offset']
            if old['snapshot']!=snapshot or isinstance(at,bool) or not isinstance(at,int) or not 0<=at<=len(rows):raise ValueError()
        except Exception:raise QueryRejected('metric catalogue cursor changed scope, window or snapshot; restart discovery') from None
    limit=min(MAX_PAGE,max(1,int(limit)));end_at=min(len(rows),at+limit)
    nxt=base64.urlsafe_b64encode(json.dumps({'snapshot':snapshot,'offset':end_at}).encode()).decode() if end_at<len(rows) else None
    data=dict(schema_version=VERSION,kind='metric_catalog',metrics=rows[at:end_at],
        complete=bool(complete and nxt is None),next_cursor=nxt,snapshot=snapshot,notes=list(notes))
    checked(data)
    return RawOutcome(data,'metric_catalog:'+snapshot+':'+str(at),
        note=f'{len(data["metrics"])} metric descriptors; complete={data["complete"]}; presence does not prove complete history or normality')

async def from_prometheus(source,query,*,start,end,limit=MAX_PAGE,cursor=None):
    if not query.strip().startswith('{') or not query.strip().endswith('}'):
        raise QueryRejected('catalog operation requires a scoped series selector, not a PromQL expression')
    body=await source._get_json('/api/v1/series',{'match[]':query,'start':start,'end':end,'limit':2001})
    series=body.get('data')
    if body.get('status')!='success' or not isinstance(series,list):raise QueryRejected('invalid Prometheus series catalogue response')
    by_name={};owners={}
    for entry in series[:2000]:
        if not isinstance(entry,dict) or not entry.get('__name__') or not entry.get('service'):continue
        by_name.setdefault(str(entry['__name__']),set()).add(str(entry['service']))
        owners.setdefault(str(entry['__name__']),set()).add('observation_path' if entry.get('source')=='aiops_sdk_access' else 'unknown')
    rows=[{'name':name,'services':sorted(services),'type':'unknown','unit':None,
           'family':name,'statistic':'unknown',
           'origin':next(iter(owners[name])) if len(owners[name])==1 else 'mixed'} for name,services in by_name.items()]
    # Per-name metadata lookup is bounded to this page and returns no unregistered metric names.
    # The series list, not the global metadata API, establishes service ownership.
    prelim=page(rows,query=query,start=start,end=end,limit=limit,cursor=cursor,
        complete=len(series)<2000 and not body.get('warnings'))
    import asyncio
    semaphore=asyncio.Semaphore(6)
    async def enrich(row):
        async with semaphore:
            try:
                meta=await source._get_json('/api/v1/metadata',{'metric':row['name'],'limit':2})
                entries=(meta.get('data') or {}).get(row['name']) or []
                types={e.get('type','unknown') for e in entries};units={e.get('unit') or None for e in entries}
                row=dict(row,type=next(iter(types)) if len(types)==1 and types<=KINDS else 'unknown',
                    unit=next(iter(units)) if len(units)==1 else None)
            except Exception:pass  # Missing metadata remains explicitly unknown, never a guessed gauge.
            return row
    enriched=await asyncio.gather(*(enrich(r) for r in prelim.data['metrics']))
    data=dict(prelim.data,metrics=enriched)
    return RawOutcome(checked(data),prelim.raw_ref,prelim.note)
