"""Explicit independent operator attestation; SDK tokens cannot submit it."""
from diagnostics.contract import timestamp
from ..attribution.v2.verification import Observation


def verified_conditions(rows):
    out=[]
    for row in rows:
        if row['kind']!='verified_conditions' or row['source']!='human_verification':continue
        if row['payload'].get('verification_method')!='verified_human' or not row['coverage']['complete']:continue
        scope=row['identity'];start,end=map(timestamp,(row['coverage']['start'],row['coverage']['end']))
        for record in row['payload'].get('records',[])[:64]:
            data=dict(record);record_id=data.pop('source_record_id','')
            if not isinstance(record_id,str) or not 1<=len(record_id)<=128:continue
            try:observed=Observation.model_validate(data)
            except (TypeError,ValueError):continue
            if (observed.service,observed.env,observed.cluster)!=(scope['service'],scope['env'],scope['cluster'] or ''):continue
            if not observed.complete or not observed.scope_verified or not start<=observed.time_range[0]<=observed.time_range[1]<=end:continue
            # Never accept a caller-supplied blackboard id; Broker publishes and reads it back.
            observed.evidence_id=''
            observed.reason='独立人工核实记录 '+record_id+'；'+observed.reason
            out.append(observed)
    return out
