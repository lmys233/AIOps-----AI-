"""Versioned data contracts; untrusted model material cannot grant permissions."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')


class FeedbackInput(Strict):
    answer_ref:str=Field(min_length=8,max_length=128)
    vote:Literal['up','down']|None=None
    reason_codes:list[Literal['concise','detailed','plain_language','examples','structured','explain_gaps','evidence_first','effective_process',
                             'too_long','unclear','missing_evidence','incorrect','unhelpful']]=Field(default_factory=list,max_length=5)
    comment:str=Field(default='',max_length=1000)
    expected_revision:int=Field(ge=0)
    request_id:str=Field(min_length=8,max_length=128)


class Support(Strict):
    feedback_id:str
    revision:int=Field(ge=1)


class Preference(Strict):
    key:str=Field(min_length=1,max_length=80)
    instruction:str=Field(min_length=1,max_length=300)
    support:list[Support]=Field(min_length=1,max_length=20)
    feedback_counts:dict[str,int]=Field(default_factory=dict)
    last_supported_at:str|None=None


class Experience(Strict):
    key:str=Field(min_length=1,max_length=80)
    observed_id:str
    env:str
    intent:str
    family:str
    capability_hash:str
    when:str=Field(min_length=1,max_length=300)
    steps:list[str]=Field(min_length=1,max_length=6)
    stop:str=Field(min_length=1,max_length=300)
    support:list[Support]=Field(min_length=1,max_length=20)
    feedback_counts:dict[str,int]=Field(default_factory=dict)
    last_supported_at:str|None=None


class Profile(Strict):
    preferences:list[Preference]=Field(default_factory=list,max_length=16)
    experiences:list[Experience]=Field(default_factory=list,max_length=8)


class Episode(Strict):
    summary:str=Field(max_length=1600)
    preferences:list[str]=Field(default_factory=list,max_length=5)
    witnesses:dict[str,str]=Field(default_factory=dict)
    process_effective:bool=False


class BuildResult(Strict):
    profile:Profile
    omitted:list[str]=Field(default_factory=list,max_length=32)
