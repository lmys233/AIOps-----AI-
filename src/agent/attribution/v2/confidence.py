"""Evidence rubric, never a probability or authority to confirm a cause."""
from .contracts import Confidence,CauseAssessment

LABELS={'high':'高','medium':'中','low':'低','insufficient':'无法评估'}


def assess_candidate(candidate):
    c=candidate.model_dump(mode='json') if hasattr(candidate,'model_dump') else candidate
    support=list(dict.fromkeys(c.get('supporting',[])))
    refute=list(dict.fromkeys(c.get('contradicting',[])))
    required=[r for r in c['requirements'] if r.get('required',True)]
    missing=[r['question'] for r in required if r['state']!='satisfied']
    conflict=any('冲突' in r.get('reason','') for r in required)
    independent=set(support)-set(c.get('basis_ids',[]))
    if refute or c['status']=='excluded':
        level='low';reason='必要条件已出现可靠反证；撤回该原因方向，不作为当前暂定结论'
    elif conflict:
        level='insufficient';reason='同范围支持与反证冲突，不能维持原置信度或强选原因'
    elif c['status']=='confirmed':
        level='high';reason='具体原因条件已核实且有独立可回查支持；高是证据等级，不是统计概率'
    elif independent:
        level='medium';reason='已有部分独立条件支持，但仍缺必要条件或独立语义见证；仅作暂定判断'
    elif c.get('basis_ids'):
        level='low';reason='仅有症状依据，尚无独立原因支持；保留为待核查解释，不足以声称最可能'
    else:
        level='insufficient';reason='尚无可回查依据，无法评估该原因方向'
    if c['status']=='candidate' and not missing:
        missing=['独立语义见证及相互竞争解释的核查']
    return Confidence(level=level,rationale=reason,supporting=support,contradicting=refute,missing=missing)


def refresh(snapshot):
    snapshot.source['confidence_version']='evidence-confidence-v1'
    for p in snapshot.problems:
        values=p.candidates+snapshot.candidate_backlog.get(p.problem_id,[])
        for c in values:c.confidence=assess_candidate(c)
        eligible=[c for c in values if c.status!='excluded']
        order={'high':0,'medium':1,'low':2,'insufficient':3}
        eligible.sort(key=lambda c:(order[c.confidence.level],-len(c.confidence.supporting),c.candidate_id))
        p.cause_assessments=[CauseAssessment(candidate_id=c.candidate_id,statement=c.statement,
            status='confirmed' if c.status=='confirmed' else 'provisional',confidence=c.confidence) for c in eligible[:4]]
    return snapshot


def describe(confidence):
    c=confidence.model_dump(mode='json') if hasattr(confidence,'model_dump') else confidence
    return '置信度：'+LABELS[c['level']]+'（证据评估，未统计校准）；'+c['rationale']


def assessment_rows(snapshot):
    from .contracts import Snapshot
    # A new presentation may upgrade a legacy snapshot's projection, never its stored body.
    snap=refresh(Snapshot.model_validate(snapshot))
    out=[]
    for p in snap.problems:
        for a in p.cause_assessments:
            c=a.confidence
            out.append(dict(label=('已确认原因' if a.status=='confirmed' else '暂定结论')+' · '+p.service+' · '+a.statement,
                value=a.candidate_id+'；'+describe(c)+'；支持：'+('、'.join(c.supporting) or '仅有症状引用 '+ '、'.join(next(x for x in p.candidates+snap.candidate_backlog.get(p.problem_id,[]) if x.candidate_id==a.candidate_id).basis_ids))+
                    '；反证：'+('、'.join(c.contradicting) or '尚无可靠反证，不能视为已排除其他方向')+
                    '；待核查：'+('；'.join(c.missing) or '其他未覆盖方向及当前状态')))
    return out


def headline(snapshot):
    from .contracts import Snapshot
    snap=refresh(Snapshot.model_validate(snapshot));parts=[]
    for p in snap.problems:
        if p.cause.status=='confirmed':continue
        items=p.cause_assessments
        medium=[a for a in items if a.confidence.level=='medium']
        chosen=medium or items[:2]
        if chosen:
            lead='当前较有依据的暂定判断' if medium else '可供核查的暂定解释（尚不足以选定最可能原因）'
            parts.append(p.service+' '+lead+'：'+'；'.join(a.statement+'〔置信度：'+LABELS[a.confidence.level]+'，未校准〕' for a in chosen[:2]))
    return '。'.join(parts[:3])


def questions(snapshot):
    """Open obligations, including identity needed for a useful user reply."""
    result=[];seen=set()
    for p in snapshot.get('problems',[]):
        if p['cause']['status']=='confirmed':continue
        for c in p['candidates']+snapshot.get('candidate_backlog',{}).get(p['problem_id'],[]):
            if c['status']!='candidate':continue
            for r in c['requirements']:
                key=(p['problem_id'],r['signal'])
                if r['state'] in ('satisfied','refuted') or key in seen:continue
                seen.add(key)
                result.append(dict(problem_id=p['problem_id'],candidate_id=c['candidate_id'],requirement_key=r['key'],
                    signal=r['signal'],question=r['question'],tool=r['tool'],
                    correlation={k:p[k] for k in ('service','env','request_id','instance','boot_id','object_name','query_fingerprint') if p.get(k)},
                    time_range=p['time_range'],purpose=c['statement']))
    return result[:12]
