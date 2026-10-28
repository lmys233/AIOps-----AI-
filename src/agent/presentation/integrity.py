"""Explain missing saved witnesses without manufacturing their original contents."""
def missing(body):
    existing={e['evidence_id'] for e in body.get('evidence') or [] if e.get('evidence_id')}
    snapshot=(body.get('explanation') or {}).get('attribution_v2') or {};ids=set()
    def visit(value,key=''):
        if key in ('evidence_ids','supporting','contradicting','basis_ids') and isinstance(value,list):
            ids.update(v for v in value if isinstance(v,str) and v)
        elif isinstance(value,dict):
            for k,v in value.items():visit(v,k)
        elif isinstance(value,list):
            for v in value:visit(v,key)
    visit(snapshot)
    return sorted(ids-existing)
