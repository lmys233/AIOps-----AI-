"""Export the saved view verbatim; no diagnosis or plan regeneration."""
from urllib.parse import quote
from .projection import saved


def evidence_href(body,eid):
    ex=body.get('explanation') or {};oid=(ex.get('observed_system') or {}).get('observed_id')
    root='/v1/observed-systems/'+quote(oid,safe='') if oid else '/v1'
    return root+'/incidents/'+quote(body['incident_id'],safe='')+'/evidence/'+quote(eid,safe='')


def reference_lines(body):
    refs=saved(body)['references'];out=[]
    for r in refs:
        title=(r['icon']+' '+r['summary']).replace('[','\\[').replace(']','\\]')
        line='- ['+title+']('+evidence_href(body,r['evidence_id'])+') · `'+r['evidence_id']+'`'
        if r['aliases']:line+='；同观测重复读取：'+', '.join('`'+a+'`' for a in r['aliases'])
        if r.get('source_roles'):line+='；读取来源：'+', '.join(r['source_roles'])
        if r.get('summary_truncated'):line+='；简述已截短，完整正文见引用'
        out.append(line)
    if not saved(body).get('references_complete',True):
        out.append('引用索引已截短：展示 '+str(len(refs))+' / '+str(saved(body)['reference_total'])+' 条；原证据仍随事故留存。')
    return out or ['本轮没有留存证据引用；不能据此判断正常。']


def conclusion_lines(body):
    view=saved(body)
    lines=[view['summary']]
    lines.extend('\n### '+str(r['label'])+'\n\n'+str(r['value']) for r in view['details'])
    if view.get('report_note'):lines.append('\n'+view['report_note'])
    return lines


def problem_lines(body):
    view=saved(body);slots=body.get('slots') or {};scope=(body.get('explanation') or {}).get('observed_system') or {}
    ref=view['diagnosis_ref']
    return ['- 状态：'+view['status_label'],'- 系统：'+str(scope.get('observed_name') or scope.get('observed_id') or '未留存'),
        '- 服务：'+', '.join(slots.get('service_names') or []),'- 环境：'+str(slots.get('env') or '未留存'),
        '- 时间窗：'+str(slots.get('time_window') or '未留存'),
        '- 诊断版本：`'+ref['snapshot_id']+'`；完整性：`'+ref['sha256']+'`',
        '- 本轮视图：`'+view['view_id']+'`（'+view['source']+'）']
