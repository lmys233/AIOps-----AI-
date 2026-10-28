"""Semantic proposal consistency. Never infer safety from words in model reasons."""
from ..common.enums import Intent
from .classifier import SLOT_REQUIREMENTS

VERSION='routing-contract-v1'


def requirements():
    return {intent.value:[list(group) for group in groups] for intent,groups in SLOT_REQUIREMENTS.items()}


def inspect(decision,allowed):
    if decision is None:return {'version':VERSION,'status':'unrecognized','issues':['NO_PROPOSAL']}
    status=decision.scope_status;issues=[]
    required={f for group in SLOT_REQUIREMENTS[Intent(decision.intent)] for f in group}
    missing=list(decision.execution_missing)
    if status in ('unsafe','out_of_scope'):
        return {'version':VERSION,'status':status,'issues':[],'execution_missing':missing}
    if status is None:issues.append('SCOPE_UNDECLARED')
    elif decision.intent not in allowed:issues.append('ABILITY_DISABLED')
    elif status=='in_scope' and decision.intent=='OUT_OF_SCOPE':issues.append('INTENT_SCOPE_CONTRADICTION')
    elif status=='unrecognized' and decision.intent!='OUT_OF_SCOPE' and decision.reference_status!='ambiguous':
        issues.append('KNOWN_INTENT_UNKNOWN_SCOPE')
    elif status=='in_scope' and decision.reference_status=='ambiguous':issues.append('AMBIGUOUS_REFERENCE_IN_SCOPE')
    if set(missing)-required and status=='in_scope':issues.append('UNSUPPORTED_EXECUTION_REQUIREMENT')
    state='recognized' if status=='in_scope' and not issues else 'unrecognized'
    return {'version':VERSION,'status':state,'issues':issues,'execution_missing':missing,
            'reference_status':decision.reference_status,'requirements':[list(g) for g in SLOT_REQUIREMENTS[Intent(decision.intent)]]}


def needs_review(contract):
    if 'ABILITY_DISABLED' in contract['issues']:return False
    return bool(set(contract['issues']) & {'INTENT_SCOPE_CONTRADICTION','KNOWN_INTENT_UNKNOWN_SCOPE',
        'AMBIGUOUS_REFERENCE_IN_SCOPE','UNSUPPORTED_EXECUTION_REQUIREMENT'})


def unknown_cost():
    from ..llm.accounting import REQUEST_RECORDS
    from ..execution.runtime import CURRENT
    records=REQUEST_RECORDS.get() or []
    if any(not r.usage_known for r in records):return True
    execution=CURRENT.get()
    return bool(execution and execution.summary()['budget_unknown'])
