"""Restore task facts after the bounded plan-generation adapter, before presentation."""
from copy import deepcopy


def merge_evidence(original,new):
    rows=deepcopy(original);known={e['evidence_id']:e for e in rows}
    for e in new:
        old=known.get(e['evidence_id'])
        if old is not None and old!=e:raise ValueError('conflicting evidence identity in task update')
        if old is None:rows.append(deepcopy(e));known[e['evidence_id']]=e
    return rows


def preserve(rendered,source,problem=None):
    for key in ('evidence','root_cause','hypotheses','slots'):
        if key in source:rendered[key]=deepcopy(source[key])
    rendered['explanation']['attribution_v2']=deepcopy(source['explanation']['attribution_v2'])
    if problem:
        from ..presentation.plan_scope import focus
        focus(rendered,source,problem)
    return rendered
