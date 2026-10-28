"""Validated problem-scoped projection; never edit the parent diagnosis."""
from copy import deepcopy
from .contracts import Snapshot


def problem_snapshot(snapshot,problem_id):
    raw=deepcopy(snapshot.model_dump(mode='json') if isinstance(snapshot,Snapshot) else snapshot)
    problems=[p for p in raw.get('problems',[]) if p['problem_id']==problem_id]
    if len(problems)!=1:raise ValueError('problem not in diagnosis')
    service=problems[0]['service']
    raw['problems']=problems
    raw['candidate_backlog']={k:v for k,v in raw.get('candidate_backlog',{}).items() if k==problem_id}
    raw['pending_problems']=[p for p in raw.get('pending_problems',[]) if p==problem_id]
    raw['capability_gaps']=[g for g in raw.get('capability_gaps',[]) if g.get('service')==service]
    raw['observations']=[o for o in raw.get('observations',[]) if o.get('service')==service]
    raw['sdk_access_checks']=[c for c in raw.get('sdk_access_checks',[]) if c['identity'].get('service')==service]
    raw['observability_problems']=[p for p in raw.get('observability_problems',[]) if p['service']==service]
    if raw.get('sdk_verification'):
        raw['sdk_verification']['checks']=[c for c in raw['sdk_verification'].get('checks',[]) if c['service']==service]
    return Snapshot.model_validate(raw)
