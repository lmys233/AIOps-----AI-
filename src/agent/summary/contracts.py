from typing import Literal,Any
from pydantic import BaseModel,Field,ConfigDict


class SummaryRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    purpose:Literal['memory_context','feedback_episode','preference_partition','knowledge_context','tool_payload']
    mode:Literal['semantic','extractive','statistical']='semantic'
    source:Any
    source_refs:list[str]=Field(default_factory=list)
    scope:dict=Field(default_factory=dict)
    requirements:str=''
    output_tokens:int=Field(default=512,ge=64,le=4096)
    input_tokens:int=Field(default=50000,ge=256,le=50000)
    protected_fields:list[str]=Field(default_factory=list)


class SummaryResult(BaseModel):
    version:int=1
    purpose:str
    policy:str
    mode:str
    source_hash:str
    source_refs:list[str]
    content:Any=None
    status:str='complete'
    omitted:list[str]=Field(default_factory=list)
    usage:dict=Field(default_factory=dict)
