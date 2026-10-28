"""Logical warning selections over immutable receipts. All SQL values are bound.

A snapshot freezes notifications, not just the first page: downstream commands
can narrow the complete upstream set, including rows outside its preview.
"""
import base64,hashlib,json
from datetime import datetime,timezone

FIELDS={'warning_id','name','content','location','service','status','severity','source','occurred_at'}
OPS={'eq','contains','gte','lte','gt','lt','in'}
REVIEWER='webui:user'  # Shared platform identity; not an invented per-user login.


def warning_id(tenant,oid,fingerprint):
    return 'warn_'+hashlib.md5((tenant+':'+oid+':'+fingerprint).encode()).hexdigest()


def predicate(value,depth=0):
    if value in (None,{}):return {}
    if not isinstance(value,dict) or depth>4:raise ValueError('筛选条件过深或格式错误')
    if set(value) in ({'all'},{'any'}):
        key=next(iter(value));children=value[key]
        if not isinstance(children,list) or not 1<=len(children)<=10:raise ValueError('组合条件需为1~10项')
        return {key:[predicate(c,depth+1) for c in children]}
    if set(value)!={'field','op','value'} or value['field'] not in FIELDS or value['op'] not in OPS:
        raise ValueError('筛选仅支持已声明的字段和操作符')
    v=value['value'];op=value['op'];field=value['field']
    if op=='in':
        if not isinstance(v,list) or not 1<=len(v)<=50 or any(not isinstance(s,str) or len(s)>512 for s in v):raise ValueError('in需为1~50个短字符串')
    elif not isinstance(v,str) or not v or len(v)>512:raise ValueError('条件值需为1~512字符')
    if field=='occurred_at':
        if op not in ('eq','gte','lte','gt','lt'):raise ValueError('时间字段仅支持eq/gte/lte/gt/lt')
        d=datetime.fromisoformat(v.replace('Z','+00:00'))
        if not d.tzinfo:raise ValueError('时间条件必须包含时区')
        v=d.astimezone(timezone.utc).isoformat()
    elif op in ('gte','lte','gt','lt'):raise ValueError('范围操作仅支持发生时间')
    return dict(field=field,op=op,value=v)


def combine(left,right):
    return {'all':[left,right]} if left and right else left or right or {}


def sql_filter(node,args):
    if not node:return 'TRUE'
    for key,join in (('all',' AND '),('any',' OR ')):
        if key in node:return '('+join.join(sql_filter(c,args) for c in node[key])+')'
    field=node['field'];op=node['op'];value=node['value']
    args.append(datetime.fromisoformat(value) if field=='occurred_at' else value)
    slot='$'+str(len(args))
    if op=='contains':return f"strpos(lower({field}),lower({slot}::text))>0"
    if op=='in':return f'{field}=ANY({slot}::text[])'
    return f'{field} '+{'eq':'=','gte':'>=','lte':'<=','gt':'>','lt':'<'}[op]+f' {slot}'


def matches(row,node):
    if not node:return True
    if 'all' in node:return all(matches(row,c) for c in node['all'])
    if 'any' in node:return any(matches(row,c) for c in node['any'])
    a=row[node['field']];b=node['value'];op=node['op']
    if node['field']=='occurred_at':a=datetime.fromisoformat(a);b=datetime.fromisoformat(b)
    return {'eq':lambda:a==b,'contains':lambda:b.casefold() in a.casefold(),'in':lambda:a in b,
        'gte':lambda:a>=b,'lte':lambda:a<=b,'gt':lambda:a>b,'lt':lambda:a<b}[op]()


CTE='''WITH scoped AS (
 SELECT *,record->'labels' AS labels,record->'annotations' AS annotations
 FROM agent_alert_receipts WHERE tenant=$1 AND observed_id=$2
 AND COALESCE(record->'labels'->>'env','prod')=$3
 AND ($4::text IS NULL OR COALESCE(record->'labels'->>'cluster','')=$4)
 AND sequence<=$5
), grouped AS (
 SELECT DISTINCT ON(fingerprint) *,min(sequence) OVER(PARTITION BY fingerprint) AS first_sequence,
 count(*) OVER(PARTITION BY fingerprint) AS notification_count,
 min(received_at) OVER(PARTITION BY fingerprint) AS first_received_at
 FROM scoped ORDER BY fingerprint,sequence DESC
), warnings AS (
 SELECT 'warn_'||md5(tenant||':'||observed_id||':'||fingerprint) AS warning_id,
 COALESCE(labels->>'alertname','未命名告警') AS name,
 COALESCE(annotations->>'description',annotations->>'summary','') AS content,
 COALESCE(annotations->>'location',labels->>'instance','') AS location,
 COALESCE(labels->>'service','') AS service,COALESCE(labels->>'severity','unknown') AS severity,
 COALESCE(record->>'status','firing') AS status,COALESCE(record->>'source','alertmanager') AS source,
 COALESCE(NULLIF(record->>'startsAt','')::timestamptz,received_at) AS occurred_at,
 COALESCE(labels->>'cluster','') AS cluster,sequence AS last_sequence,
 observed_id,record,receipt_id,incident_id,session_id,state,received_at,first_received_at,
 first_sequence,notification_count FROM grouped
) '''


class Warnings:
    def __init__(self,receipts):self.receipts=receipts

    async def snapshot(self,scope):
        if hasattr(self.receipts,'fetch'):
            row=await self.receipts.one('SELECT COALESCE(max(sequence),0) AS n FROM agent_alert_receipts WHERE tenant=$1 AND observed_id=$2',scope.tenant,scope.observed_id)
            return int(row['n'])
        return max((r['sequence'] for (tenant,_),r in self.receipts.rows.items() if tenant==scope.tenant and r['observed_id']==scope.observed_id),default=0)

    async def review(self,scope,snapshot):
        if isinstance(snapshot,bool) or not isinstance(snapshot,int) or not 0<=snapshot<2**63:raise ValueError('非法审阅快照')
        if snapshot>await self.snapshot(scope):raise ValueError('不能审阅尚未接收的告警')
        if hasattr(self.receipts,'fetch'):
            await self.receipts.fetch('''INSERT INTO agent_warning_reviews
              (tenant,observed_id,env,cluster,reviewer,through_sequence) VALUES($1,$2,$3,$4,$5,$6)
              ON CONFLICT(tenant,observed_id,env,cluster,reviewer) DO UPDATE SET
              through_sequence=GREATEST(agent_warning_reviews.through_sequence,EXCLUDED.through_sequence),updated_at=now()
              RETURNING through_sequence''',scope.tenant,scope.observed_id,scope.env,'='+scope.cluster if scope.cluster is not None else '*',REVIEWER,snapshot)
        else:
            key=(scope.tenant,scope.observed_id,scope.env,'='+scope.cluster if scope.cluster is not None else '*',REVIEWER)
            self.receipts.reviews[key]=max(snapshot,self.receipts.reviews.get(key,0))
        return await self.counts(scope)

    def unread(self,scope,row):
        prefix=(scope.tenant,scope.observed_id,scope.env)
        water=max(self.receipts.reviews.get((*prefix,'*',REVIEWER),0),self.receipts.reviews.get((*prefix,'='+row['cluster'],REVIEWER),0))
        return row['last_sequence']>water

    async def counts(self,scope,*,where=None,snapshot=None):
        snapshot=await self.snapshot(scope) if snapshot is None else snapshot
        if not hasattr(self.receipts,'fetch'):
            result=await self.page(scope,where=where,snapshot=snapshot,limit=1)
            return {k:result[k] for k in ('total','unreviewed','snapshot')}
        args=[scope.tenant,scope.observed_id,scope.env,scope.cluster,snapshot,REVIEWER]
        condition=sql_filter(predicate(where),args)
        row=await self.receipts.one(CTE+'''SELECT count(*) AS total,count(*) FILTER(WHERE last_sequence>GREATEST(
          COALESCE((SELECT through_sequence FROM agent_warning_reviews WHERE tenant=$1 AND observed_id=$2 AND env=$3 AND cluster='*' AND reviewer=$6),0),
          COALESCE((SELECT through_sequence FROM agent_warning_reviews WHERE tenant=$1 AND observed_id=$2 AND env=$3 AND cluster='='||warnings.cluster AND reviewer=$6),0))) AS unreviewed
          FROM warnings WHERE '''+condition,*args)
        return dict(total=int(row['total']),unreviewed=int(row['unreviewed']),snapshot=snapshot)

    async def page(self,scope,*,where=None,snapshot=None,before=None,limit=20,include_record=False):
        if not 1<=limit<=50:raise ValueError('limit需为1~50')
        where=predicate(where);snapshot=await self.snapshot(scope) if snapshot is None else snapshot
        if isinstance(snapshot,bool) or not isinstance(snapshot,int) or not 0<=snapshot<2**63:raise ValueError('非法快照')
        if before is not None and (isinstance(before,bool) or not isinstance(before,int) or before<1):raise ValueError('非法分页位置')
        if hasattr(self.receipts,'fetch'):
            args=[scope.tenant,scope.observed_id,scope.env,scope.cluster,snapshot]
            condition=sql_filter(where,args)
            total=await self.counts(scope,where=where,snapshot=snapshot)
            if before is not None:args.append(before);condition+=' AND first_sequence<$'+str(len(args))
            args.append(limit+1)
            rows=await self.receipts.fetch(CTE+'SELECT * FROM warnings WHERE '+condition+' ORDER BY first_sequence DESC LIMIT $'+str(len(args)),*args)
            for r in rows:
                if isinstance(r['record'],str):r['record']=json.loads(r['record'])
            count=int(total['total']);unreviewed=total['unreviewed']
        else:
            groups={}
            for (tenant,_),r in self.receipts.rows.items():
                labels=r['record']['labels']
                if tenant!=scope.tenant or r['observed_id']!=scope.observed_id or labels.get('env','prod')!=scope.env or scope.cluster is not None and labels.get('cluster','')!=scope.cluster or r['sequence']>snapshot:continue
                groups.setdefault(r['fingerprint'],[]).append(r)
            rows=[]
            for fp,notifications in groups.items():
                notifications.sort(key=lambda r:r['sequence']);r=notifications[-1];record=r['record'];a=record['annotations'];l=record['labels']
                rows.append(dict(warning_id=warning_id(scope.tenant,scope.observed_id,fp),name=l.get('alertname','未命名告警'),
                    content=a.get('description') or a.get('summary',''),location=a.get('location') or l.get('instance',''),
                    service=l.get('service',''),severity=l.get('severity','unknown'),status=record['status'],source=record['source'],
                    occurred_at=record.get('startsAt') or r['received_at'],first_sequence=notifications[0]['sequence'],last_sequence=r['sequence'],cluster=l.get('cluster',''),
                    first_received_at=notifications[0]['received_at'],notification_count=len(notifications),
                    **{k:r[k] for k in ('record','receipt_id','incident_id','session_id','state','received_at','observed_id')}))
            rows=[r for r in rows if matches(r,where)];count=len(rows);unreviewed=sum(self.unread(scope,r) for r in rows)
            rows=sorted([r for r in rows if before is None or r['first_sequence']<before],key=lambda r:r['first_sequence'],reverse=True)[:limit+1]
        more=len(rows)>limit;rows=rows[:limit]
        if not include_record:
            for r in rows:
                r.pop('record',None)
                for key,size in (('content',600),('location',256)):
                    r[key+'_truncated']=len(r[key])>size;r[key]=r[key][:size]
        return dict(items=rows,total=count,unreviewed=unreviewed,snapshot=snapshot,next_before=rows[-1]['first_sequence'] if more else None,
            selection=dict(where=where,snapshot=snapshot),evidence_role='sender_claim_not_observation')


def store_of(service):
    from .receipts import store_of as receipts
    return Warnings(receipts(service))


def cursor_encode(scope,result,where):
    if result['next_before'] is None:return None
    data=dict(scope=[scope.tenant,scope.observed_id,scope.env,scope.cluster],where=predicate(where),snapshot=result['snapshot'],before=result['next_before'])
    return base64.urlsafe_b64encode(json.dumps(data,ensure_ascii=False).encode()).decode().rstrip('=')


def cursor_decode(scope,value):
    if len(value)>16000:raise ValueError('分页标识过长')
    try:data=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
    except Exception:raise ValueError('非法分页标识') from None
    if not isinstance(data,dict) or set(data)!={'scope','where','snapshot','before'}:raise ValueError('非法分页字段')
    if data.get('scope')!=[scope.tenant,scope.observed_id,scope.env,scope.cluster]:raise ValueError('分页标识不属于当前系统/环境/集群')
    return data
