"""从真实错误抽取问题，不以摘要里的元数据猜对象或原因。"""
from __future__ import annotations
import hashlib
import json
import re
from typing import Sequence

from ...common.contracts import Evidence
from ..errors import error_groups
from .contracts import Conclusion, Problem

OBJECT = re.compile(r'''(?:relation|table|column)\s+["']([^"']+)["']''', re.I)
BARE_OBJECT = re.compile(r'(?:relation|table|column)\s+([\w.]+)\s+does not exist', re.I)
ENDPOINT = re.compile(r'\b(GET|POST|PUT|DELETE|PATCH|HEAD)\s+(/[^\s：:，]+)')
EXCEPTION = re.compile(r'\b([A-Za-z_]\w*(?:Error|Exception))\b')


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:20]
    return prefix + '-' + digest


def sample_of(item: Evidence) -> str:
    return next((tag[7:] for tag in item.tags if tag.startswith('sample=')), item.summary)


def mechanism(sample: str, code: str, obj: str) -> str:
    if code.startswith('SDK_ACCESS_'):return 'AIOps 对已登记 SDK 接口的实际访问未取得有效响应，业务可用性与形成原因待核查'
    meanings={'POOL_ACQUIRE_TIMEOUT':'在期限内未取得数据库连接',
        'HTTP_REQUEST_FAILED':'接口请求失败，具体失败来源待核查',
        'REQUEST_SLOW':'请求耗时超过配置的诊断门槛，慢阶段待核查',
        'SQL_SLOW':'SQL 执行耗时超过配置的诊断门槛，等待与计划原因待核查',
        'PROCESS_EXIT':'独立观察器记录了应用异常退出，终止原因待核查',
        'PROCESS_START_FAILED':'独立观察器记录了应用启动失败'}
    meanings.update(HTTP_REQUEST_CANCELLED='HTTP 处理任务在完整响应前被中止，触发来源待核查',
                    HTTP_REQUEST_TIMEOUT='请求出现超时异常或超时响应，期限与发生阶段待核查',
                    PROCESS_RESTART_BURST='短时多次启动已被观测，触发原因待核实')
    if code in meanings:return meanings[code]
    if obj and ('does not exist' in sample or 'UndefinedTableError' in sample):
        return f'数据库在应用解析路径中找不到对象 {obj}，查询失败'
    if code == 'DB_DEADLOCK' and 'deadlock' in sample.lower():
        return '数据库检测到事务死锁，至少一个事务被中止'
    if code == 'THREAD_POOL_REJECTED' and ('拒绝' in sample or 'AbortPolicy' in sample):
        return '有界工作池承载已满，新任务被拒绝'
    return ''


def build_problems(evidence: Sequence[Evidence], *, env: str = '',
                   services: Sequence[str] = ()) -> list[Problem]:
    grouped: dict[str, Problem] = {}
    for item in evidence:
        rows = error_groups([item])
        if not rows:
            metric=metric_row(item)
            rows=[metric] if metric else []
        if not rows or rows[0]['propagated'] or 'propagated' in item.tags:
            continue
        row = rows[0]
        if services and row['service'] not in services:
            continue
        sample = sample_of(item)
        obj = OBJECT.search(sample) or BARE_OBJECT.search(sample)
        endpoint, exception = ENDPOINT.search(sample), EXCEPTION.search(sample)
        scope = (env, item.entity.cluster or '', row['service'], row['error_type'],
                 exception[1] if exception else '', obj[1] if obj else '',
                 endpoint[0] if endpoint else '')
        encoded=next((t.split('=',1)[1] for t in item.tags if t.startswith('diagnostic_event_json=')),None)
        if encoded:
            raw=json.loads(encoded)
            scope+= (raw['identity']['instance'],raw['identity']['boot_id'],
                     raw['payload'].get('query_fingerprint','') if row['error_type']=='SQL_SLOW' else '')
            if row['error_type'] in ('HTTP_REQUEST_CANCELLED','HTTP_REQUEST_TIMEOUT'):
                scope+=(raw['correlation'].get('request_id',''),)
            if raw['correlation'].get('probe_id'):
                if len(scope)==10:scope+=('',)
                scope+=('active_probe:'+raw['correlation']['probe_id'],)
        oid=getattr(item.entity,'observed_id',None)
        pid = stable_id('P', *scope,*([oid] if oid else []))
        if pid not in grouped:
            grouped[pid] = _problem(pid, scope, sample, item)
            grouped[pid].observed_id=oid or ''
        _merge(grouped[pid], item, sample, row['count'])
    return sorted(grouped.values(), key=lambda p: (p.service, p.error_type, p.object_name, p.endpoint))


def metric_row(item):
    import os
    if os.getenv('AIOPS_APPLICATION_DIAGNOSTICS','off').lower() not in ('1','on','true'):return None
    if str(item.source)!='metrics' or str(item.direction) not in ('up','spike') or not item.magnitude:return None
    return dict(service=item.entity.name,error_type='METRIC_'+item.signal.upper(),propagated=False,count=0)


def _problem(pid: str, scope: tuple, sample: str, item: Evidence) -> Problem:
    direct = mechanism(sample, scope[3], scope[5])
    native={}
    if scope[3].startswith('METRIC_'):
        name=next((t.split('=',1)[1] for t in item.tags if t.startswith('metric_name=')),'')
        raw=next((t.split('=',1)[1] for t in item.tags if t.startswith('metric_metadata=')),'{}')
        try:metadata=json.loads(raw)
        except (ValueError,TypeError):metadata={}
        if not isinstance(metadata,dict):metadata={}
        native=dict(metric_name=name,metric_family=str(metadata.get('family') or ''),
            metric_type=str(metadata.get('type') or ''),metric_unit=metadata.get('unit'),
            metric_statistic=str(metadata.get('statistic') or ''))
    return Problem(problem_id=pid, env=scope[0], cluster=scope[1], service=scope[2],
        **native,
        error_type=scope[3], exception_type=scope[4], object_name=scope[5], endpoint=scope[6],
        time_range=item.time_range, first_seen=item.onset,
        instance=scope[7] if len(scope)>7 else '',boot_id=scope[8] if len(scope)>8 else '',
        query_fingerprint=scope[9] if len(scope)>9 else '',
        request_id=scope[10] if len(scope)>10 else '',
        fact=Conclusion(status='confirmed', statement=f'{scope[2]}：{sample[:1000]}',
                        evidence_ids=[item.evidence_id]),
        mechanism=Conclusion(status='confirmed', statement=direct, evidence_ids=[item.evidence_id])
                  if direct else Conclusion())


def _merge(problem: Problem, item: Evidence, sample: str, count: int) -> None:
    problem.sample_count = max(problem.sample_count, count)
    problem.first_seen = min(problem.first_seen, item.onset)
    problem.time_range = (min(problem.time_range[0], item.time_range[0]),
                          max(problem.time_range[1], item.time_range[1]))
    if sample[:1000] not in problem.samples and len(problem.samples) < 2:
        problem.samples.append(sample[:1000])
    for values, value in ((problem.evidence_ids, item.evidence_id),
                          (problem.raw_refs, item.raw_ref)):
        if value not in values:
            values.append(value)
    token = next((t.split('=', 1)[1] for t in item.tags
                  if t.startswith(('observation_id=', 'trace_id=', 'request_id='))), '')
    problem.observation_groups[item.evidence_id] = stable_id('O', problem.problem_id,
        token or item.onset.isoformat())
