"""The final answer boundary. Wording adapters cannot modify captured facts."""
import os
from ..domain.presentation import AnswerView,DiagnosisSnapshot
from ..attribution.evidence_identity import groups
from .status import label


def enabled():return os.getenv('AIOPS_ANSWER_VIEW','on').lower() not in ('off','0','false')


def saved(body):
    answer=(body.get('explanation') or {}).get('answer') or {}
    return answer if answer.get('view_version')==1 else None


def references(body):
    from .references import clean_item,reference
    scope=(body.get('explanation') or {}).get('observed_system') or body.get('slots') or {}
    out=[]
    focus=((body.get('explanation') or {}).get('plan_scope') or {}).get('service')
    evidence=body.get('evidence') or []
    if focus:evidence=[e for e in evidence if (e.get('entity') or {}).get('name')==focus]
    for members in groups(evidence,scope):
        ref=reference(clean_item(members[0]),with_metadata=True);ref['aliases']=[e['evidence_id'] for e in members[1:]]
        ref['source_roles']=sorted({str(e.get('agent_role','')) for e in members})
        out.append(ref)
    return out


def report(view):
    rows=view.get('details') or []
    lines=[view.get('summary') or '本轮未留存答案正文。']
    if rows:
        from .formatting import details
        text=details(rows,view['report_format']) if view.get('report_format') else '\n'.join(str(r['label'])+'：'+str(r['value']) for r in rows)
        lines.extend(['',text])
    if view.get('report_note'):lines.extend(['',view['report_note']])
    return '\n'.join(lines)


def finish(body):
    if not enabled():return body
    diagnosis=DiagnosisSnapshot.capture(body);ex=body.setdefault('explanation',{})
    if saved(body):
        if saved(body)['diagnosis_ref']!=diagnosis.reference():raise ValueError('saved diagnosis integrity mismatch')
        AnswerView.verify(saved(body))
        return body
    answer=dict(ex.get('answer') or {})
    answer.pop('attribution_v2',None)
    answer.setdefault('summary',body.get('report') or '本轮未留存答案正文。')
    answer.setdefault('details',[]);answer.setdefault('details_kind','table');answer.setdefault('source','contract')
    answer['report_format']=answer['details_kind']
    from .integrity import missing
    absent=missing(body)
    answer.update(evidence_archive_complete=not absent,missing_evidence_ids=absent)
    if absent:
        answer['details']=list(answer['details'])+[{'label':'引用正文缺口',
            'value':str(len(absent))+' 条诊断引用没有留存原文，本轮不能全部回查；不根据 ID 补造内容。'}]
    refs=references(body)
    answer.update(references=refs[:500],references_complete=len(refs)<=500,
        reference_total=len(refs),status_label=label(body))
    if ex.get('plan_scope'):answer['plan_scope']=ex['plan_scope']
    answer['content_kind']='dialogue' if (ex.get('dialogue') or {}).get('action') not in (None,'new_task','reinvestigate','resume_execution') else 'result'
    # Explicit export advice belongs to this turn's view, not to diagnosis facts.
    if '## 导出' in str(body.get('report','')):answer['report_note']=body['report'].split('## 导出',1)[1]
    view=AnswerView.create(answer,diagnosis).data()
    ex['answer']=view;ex['diagnosis_ref']=diagnosis.reference();body['report']=report(view)
    if DiagnosisSnapshot.capture(body)!=diagnosis:raise ValueError('presentation changed diagnosis facts')
    return body
