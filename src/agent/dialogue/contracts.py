from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

Action=Literal['explain_result','explain_gaps','submit_evidence','compare_candidates',
    'verify_target','request_plan','clarify_target','new_task']

class Target(BaseModel):
    model_config=ConfigDict(extra='forbid')
    task_id:str=Field(default='',max_length=128)
    problem_id:str=Field(default='',max_length=128)
    candidate_id:str=Field(default='',max_length=128)
    requirement_key:str=Field(default='',max_length=128)
    revision:str=Field(default='',max_length=128)

class Decision(Target):
    action:Action='clarify_target'
    decision_source:Literal['rule','model','fallback']='rule'
    reason:str=Field(default='',max_length=500)

def snapshot(body):return (body.get('explanation') or {}).get('attribution_v2') or {}
def metadata(body):return (body.get('explanation') or {}).get('dialogue') or {}
def task_id(body):return metadata(body).get('task_id') or body.get('incident_id','')

def cost(body):
    c=body.get('cost') or {};d=metadata(body)
    return {'tokens':int(c.get('input_tokens',0))+int(c.get('output_tokens',0)),
        'reserved_tokens':((body.get('explanation') or {}).get('model_usage') or {}).get('reserved_tokens',0),
        'tool_calls':int(c.get('tool_calls',0)),'llm_calls':int(d.get('llm_calls',len((body.get('explanation') or {}).get('model_calls') or []))),
        'wall_ms':float(c.get('wall_ms',0))}
