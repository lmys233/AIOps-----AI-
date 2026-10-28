"""有界规则作用域：服务故障建议与抽象知识咨询分开。"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class FaultScope:
    abstract: re.Pattern
    assertion: re.Pattern

    def matches(self,text,slots_config=None):
        from .slots import extract_slots
        slots=extract_slots(text,config=slots_config).slots
        if not slots.service_names or not slots.symptom:return False
        # 服务名与故障术语也可能出现在规范中；明确当前故障才覆盖抽象措辞。
        return not self.abstract.search(text) or bool(self.assertion.search(text))


def parse_scope(raw):
    if raw is None:return None
    if not isinstance(raw,dict) or raw.get('kind')!='service_fault':
        raise ValueError('scope 必须是 service_fault 映射')
    if set(raw)!={'kind','abstract','assertion'}:
        raise ValueError('service_fault 需要 abstract/assertion，拒绝未知字段')
    try:
        if not all(isinstance(raw[k],str) and raw[k] for k in ('abstract','assertion')):
            raise ValueError('作用域判据不能为空')
        return FaultScope(re.compile(raw['abstract']),re.compile(raw['assertion']))
    except re.error as exc:raise ValueError('作用域正则非法') from exc
