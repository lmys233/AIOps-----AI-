"""Content-addressed immutable graph payloads, separate from mutable progress."""
import hashlib, json

def canonical(payload):
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False)
    if len(raw.encode())>8_000_000:raise ValueError('执行快照超过 8MB，停止新增执行')
    return raw

def reference(payload):
    raw=canonical(payload)
    return {'checkpoint_id':hashlib.sha256(raw.encode()).hexdigest(),'version':payload['version'],
            'fields_available':sorted(payload.get('graph',payload.get('fields',{}))),'bytes':len(raw.encode())},raw

def is_reference(value):return isinstance(value,dict) and 'checkpoint_id' in value

def verify(ref,payload):
    if set(ref)!={'checkpoint_id','version','fields_available','bytes'}:raise ValueError('非法检查点引用')
    expected,_=reference(payload)
    if ref!=expected:raise ValueError('恢复检查点引用不匹配或载荷损坏，停止恢复')
    from .checkpoint import validate
    validate(payload)
    return payload
