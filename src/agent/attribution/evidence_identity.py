"""Conservative citation identity. Never alter evidence or verification support."""
import json
from ..domain.presentation import digest


def observation_id(item):
    return next((t.split('=',1)[1] for t in item.get('tags',[])
        if isinstance(t,str) and t.startswith('observation_id=')),None)


def identity(item,scope):
    obs=observation_id(item)
    if not obs:return None
    # A repeated read is one display citation only when all semantic fields agree.
    semantic={k:v for k,v in item.items() if k not in ('evidence_id','agent_role','raw_ref','tags')}
    tags=[t for t in item.get('tags',[]) if not str(t).startswith(('also_read_by=','read_at='))]
    semantic['tags']=sorted(tags)
    return digest({'scope':scope,'observation_id':obs,'semantic':semantic})


def groups(evidence,scope):
    seen={};out=[];ids={}
    for item in evidence:
        eid=item.get('evidence_id')
        if not eid:continue
        # An ambiguous ID must never silently choose one conflicting record.
        original=json.dumps(item,sort_keys=True,ensure_ascii=False)
        if eid in ids and ids[eid]!=original:raise ValueError('conflicting saved evidence ID: '+eid)
        if eid in ids:continue
        ids[eid]=original;key=identity(item,scope)
        if key and key in seen:
            seen[key].append(item)
        else:
            members=[item];out.append(members)
            if key:seen[key]=members
    return out
