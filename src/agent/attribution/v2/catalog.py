"""The active query queue is bounded; deferred explanations are not exclusions."""
from copy import deepcopy
from .contracts import Candidate,Problem
from .seeds import seeds

BACKLOG_TOTAL=64
BACKLOG_PER_PROBLEM=8


def defer(snapshot,p,c,*,moving=False):
    if not moving and any(x.candidate_id==c.candidate_id for x in p.candidates):return True
    items=snapshot.candidate_backlog.setdefault(p.problem_id,[])
    if any(x.candidate_id==c.candidate_id for x in items):return True
    if len(items)>=BACKLOG_PER_PROBLEM or sum(map(len,snapshot.candidate_backlog.values()))>=BACKLOG_TOTAL:
        note='待查目录额度未覆盖：'+p.problem_id+' '+c.mechanism_key
        if note not in snapshot.notes:snapshot.notes.append(note)
        return False
    items.append(deepcopy(c))
    if p.problem_id not in snapshot.pending_problems:snapshot.pending_problems.append(p.problem_id)
    return True


def inventory(p,snapshot=None):
    """Read-only old snapshot repair; never claim a missing hypothesis has been tested."""
    problem=Problem.model_validate(p);seen={};out=[]
    entries=[('active',problem.candidates),('deferred',[
        Candidate.model_validate(c) for c in (snapshot or {}).get('candidate_backlog',{}).get(p['problem_id'],[])]),
        ('not_scheduled',seeds(problem))]
    for queue,items in entries:
        for c in items:
            if c.problem_id!=problem.problem_id or not set(c.basis_ids)<=set(problem.evidence_ids):continue
            if c.mechanism_key in seen:continue
            seen[c.mechanism_key]=True;out.append({'candidate':c.model_dump(mode='json'),'queue':queue})
    return out


def activate(snapshot,pid,chosen):
    """Explicit verification moves a deferred item into the existing 12/4 queue."""
    p=next(p for p in snapshot.problems if p.problem_id==pid)
    for raw in chosen:
        c=Candidate.model_validate(raw)
        if any(x.candidate_id==c.candidate_id for x in p.candidates):continue
        if c.problem_id!=pid or not set(c.basis_ids)<=set(p.evidence_ids):raise ValueError('候选越问题')
        while sum(x.status!='excluded' for x in p.candidates)>=4 or sum(
                x.status!='excluded' for q in snapshot.problems for x in q.candidates)>=12:
            same=sum(x.status!='excluded' for x in p.candidates)>=4
            victims=[(q,x) for q in snapshot.problems for x in q.candidates
                     if x.status=='candidate' and (not same or q.problem_id==pid)
                     and x.candidate_id not in {v['candidate_id'] for v in chosen}]
            if not victims:return False
            q,v=min(victims,key=lambda pair:pair[1].rank)
            if not defer(snapshot,q,v,moving=True):return False
            q.candidates.remove(v)
        p.candidates.append(c)
        snapshot.candidate_backlog[pid]=[x for x in snapshot.candidate_backlog.get(pid,[]) if x.candidate_id!=c.candidate_id]
    return True
