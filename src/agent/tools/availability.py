"""工具声明的配置预检；不连接上游，不代替权限、参数或执行闸门。"""
from dataclasses import dataclass
import os
import re
from typing import Callable, Mapping, Any, Literal
from ..memory.gates import redact_text


def safe_detail(value, limit=900):
    text=str(value or '')
    for key,secret in os.environ.items():
        if len(secret)>=6 and re.search(r'PASSWORD|TOKEN|SECRET|API_KEY|DSN|DATABASE_URL',key,re.I):
            text=text.replace(secret,'***')
    text=re.sub(r'\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+','***',text)
    return redact_text(text)[0][:limit]


@dataclass(frozen=True)
class Availability:
    status: Literal['available','unconfigured','unsupported','invalid_source']='available'
    reason: str=''
    remedy: str=''

    def __post_init__(self):
        if self.status not in ('available','unconfigured','unsupported','invalid_source'):
            raise ValueError('未知可用性状态')
        for key in ('reason','remedy'):
            object.__setattr__(self,key,safe_detail(getattr(self,key)))

    @property
    def ready(self):return self.status=='available'


Probe=Callable[[Mapping[str,Any]],Availability]


def check_availability(probe,params):
    if probe is None:return Availability()
    try:
        result=probe(params)
        if not isinstance(result,Availability):raise TypeError('探针返回类型非法')
        return result
    except Exception as exc:
        return Availability('invalid_source','配置预检失败：'+type(exc).__name__,
                            '检查该工具适配器与配置；预检不发送上游查询')
