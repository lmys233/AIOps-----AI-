"""Fixed query intent and coverage. No ambient context, clients or transport."""
import hashlib,json
from datetime import datetime,timezone
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator,field_validator


class QueryRecord(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)


class QueryScope(QueryRecord):
    tenant:str
    observed_id:str=''
    env:str='prod'
    cluster:str|None=None
    services:tuple[str,...]=()

    @field_validator('services')
    @classmethod
    def canonical_services(cls,value):return tuple(sorted(set(value)))


class QueryWindow(QueryRecord):
    start:datetime
    end:datetime

    @model_validator(mode='after')
    def ordered(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:raise ValueError('query window requires timezone')
        if self.start>=self.end:raise ValueError('query window must have positive duration')
        return self

    def values(self):return [self.start.astimezone(timezone.utc).isoformat(),self.end.astimezone(timezone.utc).isoformat()]


class QuerySpec(QueryRecord):
    scope:QueryScope
    tool:str
    source:str
    signal:str=''
    purpose:Literal['evidence','aux']='evidence'
    requested_window:QueryWindow|None=None
    effective_window:QueryWindow|None=None
    coverage:Literal['sampled_window','window_aggregate','current_snapshot','reference']='sampled_window'
    params_json:str
    tool_version:int=Field(default=1,ge=1)
    schema_hash:str=''

    @model_validator(mode='after')
    def parameters(self):
        if not isinstance(json.loads(self.params_json),dict):raise ValueError('query parameters must be an object')
        return self

    def params(self):return json.loads(self.params_json)

    @property
    def query_id(self):
        encoded=self.model_dump_json()
        return 'qry_'+hashlib.sha256(encoded.encode()).hexdigest()[:24]

    def summary(self):
        p=self.params()
        return dict(query_id=self.query_id,tool=self.tool,source=self.source,
            scope=self.scope.model_dump(),signal=self.signal,purpose=self.purpose,
            requested_window=self.requested_window.values() if self.requested_window else None,
            effective_window=self.effective_window.values() if self.effective_window else None,
            coverage=self.coverage,tool_version=self.tool_version,
            coverage_is_requirement=True,
            filters={k:p[k] for k in ('service','object_name','instance','request_id','trace_id') if k in p},
            sampling={k:p[k] for k in ('limit','step','direction','before') if k in p})
