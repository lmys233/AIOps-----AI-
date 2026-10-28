"""Editable routing data; execution handlers and tool policy remain code-owned."""
import hashlib
import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..common.enums import Intent


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text).strip().split())


class PatternData(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pattern: str = Field(min_length=1, max_length=512)
    weight: float = Field(default=1.4, gt=0, le=2, allow_inf_nan=False)
    note: str = Field(default='', max_length=300)

    @model_validator(mode='after')
    def valid_regex(self):
        # Validate both the existing Python dialect and the bounded execution engine.
        import regex
        try:
            re.compile(self.pattern)
            regex.compile(self.pattern)
        except (re.error, regex.error) as exc:
            raise ValueError('正则语法非法') from exc
        return self


class ScopeData(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['service_fault'] = 'service_fault'
    abstract: str = Field(min_length=1, max_length=512)
    assertion: str = Field(min_length=1, max_length=512)

    @model_validator(mode='after')
    def valid_regex(self):
        PatternData(pattern=self.abstract)
        PatternData(pattern=self.assertion)
        return self


class RouteDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=1500)
    handler: Intent
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10000)
    gate: float = Field(default=.5, ge=.2, le=1, allow_inf_nan=False)
    patterns: list[PatternData] = Field(default_factory=list, max_length=24)
    scope: ScopeData | None = None
    examples: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode='after')
    def examples_valid(self):
        self.examples = list(dict.fromkeys(normalized(x) for x in self.examples))
        if any(not x or len(x) > 500 for x in self.examples):
            raise ValueError('示例不能为空，每条最多 500 字符')
        return self


def defaults():
    import yaml
    from .rules import RULES_FILE
    raw = yaml.safe_load(RULES_FILE.read_text(encoding='utf-8'))
    examples = {
        'REMEDIATION': ['服务检测到数据库事务死锁，请给出修复方案', '怎么解决工作线程池已满的问题', '针对这个服务的故障提供处置建议'],
        'INCIDENT_RCA': ['整个系统怎么样，有没有问题？', '系统有没有什么问题？', '帮我排查请求耗时异常升高的原因', '评估系统运行状况是否正常'],
        'METRIC_QUERY': ['服务现在的 QPS 是多少？', '查询连接池使用率趋势', '查一下每秒请求数量', '显示请求延迟随时间的变化'],
        'CHANGE_IMPACT': ['最近的配置变更影响哪些服务？', '这个版本上线会有什么影响？', '分析最近发布对上下游的影响'],
        'KNOWLEDGE_QA': ['什么是数据库事务死锁？', '一般如何预防连接池耗尽？', '解释一下数据库连接池的工作原理',
                         'AI Agent 与工作流有什么区别？', 'ReAct 如何根据工具结果推进任务？', '什么是 RAG？',
                         'Agent 的短期记忆和长期记忆有什么区别？', '工具调用失败时一般如何重试？'],
        'OUT_OF_SCOPE': ['给我推荐一部电影', '你好，今天吃什么？', '写一篇游记'],
    }
    labels = {'REMEDIATION': ('故障处置建议', '针对具体故障提供修复方案、流程与风险，不代表执行授权'),
              'INCIDENT_RCA': ('异常诊断', '排查系统健康、接口异常、故障机制与形成原因'),
              'METRIC_QUERY': ('指标查询', '查询已观测服务的指标值、容量、变化与趋势'),
              'CHANGE_IMPACT': ('变更影响', '分析变更、发布或配置调整的影响范围'),
              'KNOWLEDGE_QA': ('知识问答', '运维、软件、数据库、AI Agent、ReAct、工具调用、记忆、RAG、架构与安全的技术概念和通用方法；通过知识库检索及原文引用回答，不要求服务名，不确认现场故障，不执行操作')}
    rows = [RouteDefinition(id=r['id'], name=labels[r['intent']][0], description=labels[r['intent']][1],
             handler=r['intent'], priority=i, gate=r['gate'], patterns=r['patterns'],
             scope=r.get('scope'), examples=examples[r['intent']])
            for i, r in enumerate(raw['rules'])]
    specialized = {
        'R2_write': ['请提供重启服务的处置预案', '请给出回滚部署版本的方案'],
        'R4_interruption_metric': ['查询请求取消率的变化趋势', '统计 HTTP 请求超时率', '查询客户端断开次数'],
        'R4_request_interruption_diagnose': ['处理请求的任务被中断，帮我查来源', '排查 HTTP 请求取消的触发原因'],
    }
    names = {'R2_write': '处置动作预案', 'R4_interruption_metric': '请求中断指标',
             'R4_request_interruption_diagnose': '请求中断诊断'}
    for row in rows:
        if row.id in names:
            row.name = names[row.id]
        if row.id in specialized:
            row.examples = [normalized(t) for t in specialized[row.id]]
    rows.append(RouteDefinition(id='R6_fallback', name='超出范围', handler=Intent.OUT_OF_SCOPE,
                               description='明确不属于启用技术/运维能力的请求，或恶意/未授权风险请求；线索不足不是越界，应继续识别或澄清', examples=examples['OUT_OF_SCOPE']))
    return [r.model_dump(mode='json') for r in rows]
