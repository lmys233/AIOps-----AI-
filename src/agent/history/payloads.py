"""Scoped PostgreSQL read projections of immutable saved incident payloads."""
import base64
import json
from ..presentation.transport import PARTS, project, summary_sql, array_sql


def decode_cursor(value):
    if value is None: return None
    if len(value)>1024: raise ValueError('载荷游标过长')
    try:
        data=json.loads(base64.urlsafe_b64decode(value+'='*((-len(value))%4)))
        if set(data)!={'tenant','observed_id','incident_id','part','offset','revision'}: raise ValueError()
        if type(data['offset']) is not int or data['offset']<=0: raise ValueError()
        if any(not isinstance(data[k],str) for k in data if k!='offset'): raise ValueError()
        return data
    except Exception as exc: raise ValueError('载荷游标非法') from exc


def encode_cursor(data):
    return base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).decode().rstrip('=')


async def page(store,oid,iid,part,*,cursor=None,limit=20,version=None):
    if part not in PARTS: raise ValueError('未知载荷分区')
    if not 1<=limit<=50: raise ValueError('载荷页大小必须在 1～50')
    token=decode_cursor(cursor);identity=dict(tenant=store.tenant,observed_id=oid,incident_id=iid,part=part)
    if token and any(token[k]!=v for k,v in identity.items()): raise ValueError('载荷游标作用域或分区不匹配')
    offset=token['offset'] if token else 0
    array=array_sql('body',PARTS[part]);pool=await store.connection()
    row=await pool.fetchrow(f"""SELECT md5(body::text) AS revision,
        body#>>'{{explanation,answer,view_id}}' AS view_id,
        jsonb_array_length({array}) AS total,
        COALESCE((SELECT jsonb_agg(item ORDER BY n) FROM jsonb_array_elements({array})
          WITH ORDINALITY AS x(item,n) WHERE n>$4 AND n<=$4+$5),'[]'::jsonb) AS items
        FROM agent_chat_incidents WHERE tenant_id=$1 AND incident_id=$2
        AND body#>>'{{explanation,observed_system,observed_id}}'=$3""",store.tenant,iid,oid,offset,limit)
    if not row: raise KeyError('incident not in system')
    if token and token['revision']!=row['revision']: raise ValueError('留存版本变化，请重新读取第一页')
    if version and version!=row['view_id']: raise ValueError('答案版本不匹配，请重新读取该事故')
    if token and offset>=row['total']: raise ValueError('载荷游标超出留存范围')
    records=json.loads(row['items']) if isinstance(row['items'],str) else row['items']
    end=offset+len(records);more=end<row['total']
    return dict(identity,items=records,total=row['total'],page_size=limit,has_more=more,
        next_cursor=encode_cursor(dict(identity,offset=end,revision=row['revision'])) if more else None,
        revision=row['revision'],view_id=row['view_id'],content_source='saved_incident',
        note='仅读取该事故留存记录；未查询 SDK 或重新生成诊断。')


async def incident_view(store,oid,iid):
    pool=await store.connection()
    raw=await pool.fetchval(f"SELECT {summary_sql('body')} FROM agent_chat_incidents WHERE tenant_id=$1 AND incident_id=$2 AND body#>>'{{explanation,observed_system,observed_id}}'=$3",store.tenant,iid,oid)
    if raw is None: raise KeyError('incident not in system')
    return project(json.loads(raw) if isinstance(raw,str) else raw,'view')


async def history_page(store,sid,before=None,observed_id=None):
    if not await store.readable(sid,observed_id):return {"turns":[],"has_more":False,"next_cursor":None,"page_size":20}
    pool=await store.connection()
    expr=summary_sql("payload->'response'")
    rows=await pool.fetch(f"""SELECT turn_no,turn_id,
        CASE WHEN payload#>>'{{response,explanation,answer,view_version}}'='1'
          AND payload#>>'{{response,explanation,task_control,commit_state}}'='committed'
          THEN (payload-'response'-'answer') || jsonb_build_object('response',{expr})
          ELSE payload END AS payload
        FROM agent_chat_turns WHERE tenant_id=$1 AND session_id=$2
        AND ($3::bigint IS NULL OR turn_no<$3)
        AND ($4::text IS NULL OR payload#>>'{{response,explanation,observed_system,observed_id}}'=$4)
        ORDER BY turn_no DESC LIMIT 21""",store.tenant,sid,before,observed_id)
    more=len(rows)>20;turns=[]
    for row in reversed(rows[:20]):
        value=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
        if value.get('response'): value['response']=project(value['response'],'view')
        turns.append(dict(value,turn=int(row['turn_no']),turn_id=row['turn_id']))
    return dict(turns=turns,has_more=more,next_cursor=str(turns[0]['turn']) if turns and more else None,page_size=20)
