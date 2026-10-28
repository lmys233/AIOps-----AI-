from typing import Literal, Any
from pydantic import BaseModel, ConfigDict, Field


class RequestPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_kind: Literal['business','sdk'] = 'business'
    sdk_interface: str = Field(default='',max_length=64)
    sdk_cluster: str = Field(default='',max_length=128)
    service: str = Field(min_length=1,max_length=128)
    endpoint_id: str = Field(pattern=r'^ep_[a-f0-9]{24}$')
    endpoint_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    instance: str = Field(min_length=1,max_length=128)
    boot_id: str = Field(min_length=1,max_length=128)
    path_params: dict = Field(default_factory=dict)
    query: dict = Field(default_factory=dict)
    body: Any = None
    mode: Literal['probe', 'load'] = 'probe'
    max_requests: int = Field(default=1, ge=1, le=200)
    duration_sec: float = Field(default=1, gt=0, le=30)
    concurrency: int = Field(default=1, ge=1, le=16)
    rate_per_sec: float = Field(default=1, gt=0, le=50)
    timeout_sec: float = Field(default=3, gt=0, le=10)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['ask', 'probe', 'observe', 'stop']
    reason: str = Field(max_length=1000)
    question: str = Field(default='', max_length=1000)
    candidates: list[str] = Field(default_factory=list, max_length=5)
    requests: list[RequestPlan] = Field(default_factory=list, max_length=2)
