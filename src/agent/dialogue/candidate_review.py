"""Cover the target's available explanations, not just the active tool queue."""
from copy import deepcopy
from ..attribution.v2.catalog import inventory,defer
from ..attribution.v2.contracts import Snapshot,Candidate
from .contracts import snapshot


def prepare(source,p,d):
    reviewed=deepcopy(p)
    entries=inventory(p,snapshot(source))
    reviewed['candidates']=[x['candidate'] for x in entries]
    reviewed['_candidate_queue']={x['candidate']['candidate_id']:x['queue'] for x in entries}
    reviewed['_focused_candidate']=d.candidate_id
    return reviewed,entries


def retain(body,entries):
    snap=Snapshot.model_validate(snapshot(body))
    for entry in entries:
        if entry['queue']=='active':continue
        c=Candidate.model_validate(entry['candidate'])
        p=next(p for p in snap.problems if p.problem_id==c.problem_id)
        defer(snap,p,c)
    from ..attribution.v2.confidence import refresh
    body['explanation']['attribution_v2']=refresh(snap).model_dump(mode='json')
    d=body['explanation']['dialogue']
    pid=entries[0]['candidate']['problem_id'] if entries else ''
    current=next((p for p in snap.problems if p.problem_id==pid),None)
    actual=inventory(current.model_dump(mode='json'),snap.model_dump(mode='json')) if current else []
    d['candidate_coverage']={'reviewed':len(actual),'active':sum(x['queue']=='active' for x in actual),
        'deferred':sum(x['queue']!='active' for x in actual),
        'scope':'适用规则及已留存候选；开放集合，未列出不等于已排除，不声称覆盖全部可能原因'}


def rows(p,d):
    out=[{'label':'候选覆盖范围','value':'候选清单与本轮取证队列分别维护；未核查的候选仍待验证。适用规则与已留存模型解释均覆盖，清单保持开放，其他机制可提出后验证。'}]
    for c in p['candidates']:
        if d.candidate_id and c['candidate_id']!=d.candidate_id:continue
        queue=p.get('_candidate_queue',{}).get(c['candidate_id'],'active')
        state={'candidate':'待验证','confirmed':'已由证据确认','excluded':'已由可靠反证排除'}[c['status']]
        waiting='；因额度未排入本轮取证，尚未验证，不能据此排除' if queue!='active' and c['status']=='candidate' else ''
        missing=[r['question'] for r in c['requirements'] if r['required'] and r['state']!='satisfied']
        refs='、'.join(c['supporting']+c['contradicting']) or '暂无形成原因的独立引用'
        text=c['candidate_id']+'；'+state+waiting+'；依据：'+refs
        from ..attribution.v2.confidence import assess_candidate,describe
        text+='；'+describe(assess_candidate(c))
        if c['status']=='candidate':
            text+='；确认需满足：'+('；'.join(missing) or '核实独立语义见证')
            text+='；排除需同范围完整记录对必要条件给出可靠反证；未采到或查询失败不能排除'
        else:text+='；'+c['reason']+'；保留已有依据，无需重补已证实条件'
        out.append({'label':'候选证据需求 · '+c['statement'],'value':text})
    return out
