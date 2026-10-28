"""V2 页面原文有总大小上限，超限显式失败，不静默截断诊断。"""
import json


def with_diagnosis(answer,diagnosis=None):
    # Bounded model-context DTO only; never inject a duplicate into saved views.
    return dict(answer or {},attribution_v2=diagnosis) if diagnosis else answer


def snapshot_of(answer,diagnosis=None):
    answer=with_diagnosis(answer,diagnosis)
    if not (answer or {}).get('attribution_v2'):return {}
    raw=json.dumps(answer,ensure_ascii=False)
    if len(raw.encode('utf-8'))>512_000:raise ValueError('诊断答案快照超过 512KB 上限')
    return json.loads(raw)
