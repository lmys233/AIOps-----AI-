import os
from dataclasses import dataclass

def enabled(name,default=False):return os.getenv(name,'1' if default else '0').lower() in ('1','true','yes')


@dataclass(frozen=True)
class Config:
    context_tokens:int=50000
    trigger_tokens:int=50000
    backlog_tokens:int=100000
    daily_calls:int=0
    daily_tokens:int=0
    daily_wall_sec:int=0
    poll_sec:int=900
    cooldown_sec:int=3600
    max_builds:int=4

    @classmethod
    def env(cls):
        get=lambda key,default:int(os.getenv('AIOPS_PREFERENCE_'+key,str(default)))
        context=min(50000,max(2048,get('CONTEXT_TOKENS',50000)))
        return cls(context_tokens=context,trigger_tokens=min(context,max(1,get('TRIGGER_TOKENS',50000))),
                   backlog_tokens=max(context,get('BACKLOG_TOKENS',100000)),
                   daily_calls=get('DAILY_MODEL_CALLS',0),daily_tokens=get('DAILY_TOKENS',0),
                   daily_wall_sec=get('DAILY_WALL_SEC',0),poll_sec=max(5,get('POLL_SEC',900)))
