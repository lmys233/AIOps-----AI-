"""Read saved incident evidence only; no tool, model or upstream fetch."""
import json
import re
from ..diagnostics.ingestion import redact
from ..diagnostics.questions import QUESTIONS

SOURCES={'logs':('🧾','日志'),'metrics':('📈','指标'),'traces':('🔗','链路'),
         'changes':('🔎','核查'),'topology':('🕸','拓扑'),'memory':('📚','历史记忆')}


def structured(item,prefix):
    value=next((t[len(prefix):] for t in item.get('tags',[]) if isinstance(t,str) and t.startswith(prefix)),None)
    if not value or len(value)>100000:return None
    try:
        parsed=json.loads(value)
        return parsed if isinstance(parsed,dict) else None
    except (ValueError,TypeError):return None


def clean_item(item):
    plain=dict(item,tags=[t for t in item.get('tags',[]) if isinstance(t,str)
                         and not t.startswith(('diagnostic_event_json=','observation_json='))])
    clean=redact(plain)
    for prefix in ('diagnostic_event_json=','observation_json='):
        value=structured(item,prefix)
        if value is not None:clean['tags'].append(prefix+json.dumps(redact(value),ensure_ascii=False))
    return clean


def reference(item,with_metadata=False):
    icon,label=SOURCES.get(item.get('source'),('📄','证据'))
    text=str(item.get('summary') or '证据正文未留存')
    event=structured(item,'diagnostic_event_json=')
    if event:
        p=event.get('payload',{});kind=event.get('kind','');svc=event.get('identity',{}).get('service','')
        icon='🗄' if kind.startswith(('sql_','db_','pool_','transaction_')) else '⚙' if kind.startswith(('worker_','runtime_','process_')) else icon
        label='数据库采集' if icon=='🗄' else '运行采集' if icon=='⚙' else label
        if kind=='sql_error':
            failure={'40P01':'数据库检测到事务死锁','42P01':'数据库对象不存在','42703':'数据库列不存在'}.get(p.get('sqlstate'))
            text=f"{svc} · {failure or p.get('exception_type') or 'SQL 执行失败'}：{text}"
        elif kind=='sql_query':text=f"{svc} · SQL 执行 {p.get('execution_ms','未知')}ms，结果 {p.get('outcome','未知')}"
        elif kind=='http_request':
            result={'cancelled':'请求被取消','timeout':'请求出现超时','post_response_cancelled':'响应完成后的清理取消',
                    'post_response_timeout':'响应完成后的清理超时'}.get(p.get('outcome'),f"状态 {p.get('status','未知')}")
            duration=p.get('total_ms','未知')
            if isinstance(duration,(int,float)):duration=round(duration,2)
            text=f"{svc} · {p.get('method','')} {p.get('route','')}，{result}，耗时 {duration}ms"
        elif kind=='request_disconnect':text=f"{svc} · 应用观测客户端断开，阶段 {p.get('operation','未知')}"
        elif kind=='request_deadline':text=f"{svc} · 已配置期限{'到达' if p.get('expired') else '观测'}，阶段 {p.get('operation','未知')}"
        elif kind=='request_cancel':text=f"{svc} · 运行回调记录取消结果 {p.get('accepted')}，来源 {p.get('origin','未知')}"
        elif kind=='request_shutdown_cancel':text=f"{svc} · 已核实运行框架进入停机取消分支"
        elif kind=='runtime_snapshot':text=f"{svc} · 运行快照：{len(p.get('holders',[]))} 个连接持有者、{len(p.get('workers',[]))} 个工作池"
    observed=structured(item,'observation_json=')
    if observed and item.get('signal') in QUESTIONS:
        result='是' if observed.get('value') is True else '否' if observed.get('value') is False else '待核实'
        entity=item.get('entity',{})
        svc=entity.get('name','') if isinstance(entity,dict) else str(entity)
        text=svc+' · '+QUESTIONS[item['signal']]+' · 核查结果：'+result
    text=re.sub(r'\s+',' ',text).strip()
    ref={'evidence_id':item['evidence_id'],'icon':icon,'source_label':label,'summary':text[:220]}
    if with_metadata:ref['summary_truncated']=len(text)>220
    return ref


def detail(item,incident_id):
    clean=clean_item(item)
    event=structured(clean,'diagnostic_event_json=')
    check=structured(clean,'observation_json=')
    plain=dict(clean,tags=[t for t in clean.get('tags',[]) if not t.startswith(('diagnostic_event_json=','observation_json='))])
    return dict(reference(clean),incident_id=incident_id,evidence=plain,observation=event,
                verification=check,content_source='saved_incident',
                note='显示本次事故留存的证据；原始引用是追溯标识，此处不会重新查询上游。')


def index(body):
    from copy import deepcopy
    from .projection import saved
    view=saved(body)
    return deepcopy(view['references']) if view else [reference(clean_item(e)) for e in body.get('evidence',[])
        if isinstance(e,dict) and e.get('evidence_id')]


def lookup(body,evidence_id):
    from .projection import saved
    rows=(body or {}).get('evidence') or []
    row=next((e for e in rows if e.get('evidence_id')==evidence_id),None)
    if row is None and not saved(body or {}):
        alias=((body or {}).get('explanation') or {}).get('evidence_aliases',{}).get(evidence_id)
        row=next((e for e in rows if e.get('evidence_id')==alias),None)
    if row is None:return None
    result=detail(row,body['incident_id'])
    if saved(body):
        ref=next((r for r in index(body) if evidence_id==r['evidence_id'] or evidence_id in r.get('aliases',[])),None)
        if ref:result.update({k:ref[k] for k in ('icon','source_label','summary')},
            citation_id=ref['evidence_id'],aliases=ref['aliases'],source_roles=ref['source_roles'])
        else:
            result.update(summary=str(row.get('summary') or '证据正文未留存'),
                note='本轮引用索引未展示此项（聚焦范围或裁剪）；此处显示留存原记录的摘要，不补造引用。')
    return result
