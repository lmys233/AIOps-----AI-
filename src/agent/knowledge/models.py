from hashlib import sha256
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


def digest(text):return sha256(text.encode('utf-8')).hexdigest()


class DocumentInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    title:str=Field(min_length=1,max_length=160)
    content:str=Field(min_length=1,max_length=60_000)
    uploader:str=Field(default='admin',min_length=1,max_length=80)
    category:str=Field(default='运维知识',min_length=1,max_length=80)
    tags:list[str]=Field(default_factory=list,max_length=24)
    status:Literal['review','active','inactive']='review'
    observed_id:str|None=Field(default=None,pattern=r'^obs_[a-f0-9]{32}$')

    @field_validator('title','content','uploader','category')
    @classmethod
    def nonblank(cls,value):
        if not value.strip():raise ValueError('字段不能为空白')
        return value.strip()

    @field_validator('tags')
    @classmethod
    def valid_tags(cls,values):
        if any(not t.strip() or len(t)>80 for t in values):raise ValueError('标签须为1~80字')
        return list(dict.fromkeys(t.strip() for t in values))


class Mutation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:str=Field(min_length=1,max_length=128)
    document:DocumentInput


class Conflict(Exception):pass
