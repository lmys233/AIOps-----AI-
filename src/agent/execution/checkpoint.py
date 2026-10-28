"""Explicit JSON codec; no pickle, eval, or imports chosen by stored data."""
import dataclasses, math
from datetime import datetime
from enum import Enum
from functools import lru_cache
from pydantic import BaseModel

from .types import types, LEGACY

@lru_cache(maxsize=1)
def registry():
    stable=types()
    return {**{'dto:'+k:v for k,v in stable.items()},**{k:stable[v] for k,v in LEGACY.items()}}

def type_id(value):
    cls=type(value)
    for key,registered in registry().items():
        if key.startswith('dto:') and registered is cls:return key
    raise TypeError('执行快照不支持类型 '+cls.__name__)

def encode(value):
    if isinstance(value,Enum):return {'$type':type_id(value),'value':value.value}
    if isinstance(value,float) and not math.isfinite(value):return {'$type':'nonfinite','value':'nan' if math.isnan(value) else 'inf' if value>0 else '-inf'}
    if value is None or isinstance(value,(str,int,float,bool)):return value
    if isinstance(value,datetime):return {'$type':'datetime','value':value.isoformat()}
    if isinstance(value,tuple):return {'$type':'tuple','value':[encode(v) for v in value]}
    if isinstance(value,set):return {'$type':'set','value':[encode(v) for v in value]}
    if isinstance(value,list):return [encode(v) for v in value]
    if isinstance(value,dict):return {'$type':'dict','value':{str(k):encode(v) for k,v in value.items()}}
    key=type_id(value)
    data=encode(value.model_dump(mode='json')) if isinstance(value,BaseModel) else {f.name:encode(getattr(value,f.name)) for f in dataclasses.fields(value)}
    return {'$type':key,'value':data}

def decode(value):
    if isinstance(value,list):return [decode(v) for v in value]
    if not isinstance(value,dict):return value
    key=value.get('$type')
    if not key:return {k:decode(v) for k,v in value.items()}
    data=value['value']
    if key=='datetime':return datetime.fromisoformat(data)
    if key=='nonfinite':return float(data)
    if key=='dict':return {k:decode(v) for k,v in data.items()}
    if key in ('tuple','set'):return (tuple if key=='tuple' else set)(decode(v) for v in data)
    cls=registry().get(key)
    if cls is None:raise ValueError('检查点类型不在恢复白名单')
    if issubclass(cls,Enum):return cls(data)
    if issubclass(cls,BaseModel):return cls.model_validate(decode(data))
    return cls(**{k:decode(v) for k,v in data.items()})

# Runtime dependencies are freshly bound by the orchestrator, never deserialized.
RUNTIME_FLAGS={'board_store','usage','on_event','task_control','executor','scorer','coordinator','directed_agents'}
FIELDS=('user_message','slots','intent','route','reference','round','plan','outcomes',
 'board','degraded_signals','degraded_detail','empty_signals','hypotheses','arbitration',
 'critic','open_questions','directed_rounds','termination','status','report','notes','evidence_gains')

def snapshot(state):
    return {'version':2,'contract':'aiops.recovery.graph','incident_id':state.incident_id,
        'graph':{k:encode(getattr(state,k)) for k in FIELDS},
        'extensions':{k:encode(v) for k,v in state.flags.items() if k not in RUNTIME_FLAGS},
        'budget':dataclasses.asdict(state.budget),
        'directed_roles':[a.role for a in state.flags.get('directed_agents') or []]}

def restore(state,payload):
    validate(payload)
    fields=payload['fields'] if payload['version']==1 else payload['graph']
    flags=payload['flags'] if payload['version']==1 else payload['extensions']
    for k,v in fields.items():
        if k not in FIELDS:raise ValueError('检查点含非法状态字段')
        setattr(state,k,decode(v))
    state.flags.update({k:decode(v) for k,v in flags.items() if k not in RUNTIME_FLAGS})
    if payload.get('directed_roles'):
        from ..agents.runner import default_agents
        agents=default_agents();roles=set(payload['directed_roles'])
        if roles-set(a.role for a in agents):raise ValueError('定向角色未注册，不能恢复执行')
        state.flags['directed_agents']=[a for a in agents if a.role in roles]
    from ..orchestration.budget import Budget
    prior=Budget(**payload['budget'])
    for name in ('max_rounds','max_tool_calls','max_llm_calls','max_tokens','max_wall_time_sec'):
        setattr(prior,name,min(getattr(prior,name),getattr(state.budget,name)))
    state.budget=prior

def validate(payload):
    """Refuse incompatible state before claiming a lease or dispatching work."""
    version=payload.get('version')
    if version not in (1,2):raise ValueError('不兼容的执行快照，未重放旧步骤')
    names={'version','incident_id','budget','directed_roles'}|({'fields','flags'} if version==1 else {'contract','graph','extensions'})
    if set(payload)-names:raise ValueError('检查点含未知契约字段')
    if version==2 and payload.get('contract')!='aiops.recovery.graph':raise ValueError('不兼容的恢复契约')
    fields=payload.get('fields' if version==1 else 'graph')
    if not isinstance(fields,dict) or set(fields)!=set(FIELDS):raise ValueError('检查点状态字段缺失或未知')
    flags=payload.get('flags' if version==1 else 'extensions')
    if not isinstance(flags,dict) or set(flags)&RUNTIME_FLAGS:raise ValueError('检查点含运行依赖或非法扩展')
    for value in [*fields.values(),*flags.values()]:decode(value)
    from ..orchestration.budget import Budget
    Budget(**payload['budget'])
