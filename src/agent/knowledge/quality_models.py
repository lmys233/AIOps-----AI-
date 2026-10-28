"""Bounded generation/review contracts. A reviewer verdict alone is insufficient."""
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')


class Section(Strict):
    text:str=Field(min_length=1,max_length=2000)
    citations:list[str]=Field(min_length=1,max_length=10)


class KnowledgeAnswer(Strict):
    summary:str=Field(min_length=1,max_length=2000)
    summary_citations:list[str]=Field(default_factory=list,max_length=10)
    sections:list[Section]=Field(min_length=1,max_length=8)


class Support(Strict):
    citation:str=Field(pattern=r'^K[1-9][0-9]*$')
    field:Literal['excerpt','title','updated_at']='excerpt'
    quote:str=Field(min_length=2,max_length=240)


class UnitReview(Strict):
    unit_id:str=Field(max_length=30)
    verdict:Literal['supported','contradicted','unsupported','uncertain','limitation']
    accuracy:Literal['consistent','inconsistent','not_verifiable']
    supports:list[Support]=Field(default_factory=list,max_length=3)
    reason:str=Field(min_length=1,max_length=500)


class Coverage(Strict):
    question_id:str=Field(max_length=30)
    requirement:str=Field(min_length=1,max_length=500)
    status:Literal['answered','insufficient_disclosed','missing']
    unit_ids:list[str]=Field(default_factory=list,max_length=32)
    reason:str=Field(min_length=1,max_length=500)


class Review(Strict):
    units:list[UnitReview]=Field(min_length=1,max_length=32)
    coverage:list[Coverage]=Field(min_length=1,max_length=16)
    source_conflicts:list[str]=Field(default_factory=list,max_length=8)
    summary:str=Field(min_length=1,max_length=600)


class WitnessUnit(Strict):
    unit_id:str=Field(max_length=30)
    verdict:Literal['supported','contradicted','unsupported','uncertain','limitation']
    accuracy:Literal['consistent','inconsistent','not_verifiable']
    supports:list[str]=Field(default_factory=list,max_length=3,description='仅引用资料中W见证编号，不重复抄写原文')
    reason:str=Field(min_length=1,max_length=120)


class WitnessCoverage(Coverage):
    requirement:str=Field(min_length=1,max_length=160)
    reason:str=Field(min_length=1,max_length=120)


class WitnessReview(Strict):
    units:list[WitnessUnit]=Field(min_length=1,max_length=32)
    coverage:list[WitnessCoverage]=Field(min_length=1,max_length=16)
    source_conflicts:list[str]=Field(default_factory=list,max_length=8)
    summary:str=Field(min_length=1,max_length=160)


class WitnessBatch(Strict):
    units:list[WitnessUnit]=Field(min_length=1,max_length=4)
    source_conflicts:list[str]=Field(default_factory=list,max_length=8)


class CoverageReview(Strict):
    coverage:list[WitnessCoverage]=Field(min_length=1,max_length=16)
    source_conflicts:list[str]=Field(default_factory=list,max_length=8)
    summary:str=Field(min_length=1,max_length=160)
