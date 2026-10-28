"""请求内事件收集，保存页面已展示的载荷，不复制原始遥测。"""
import json


def capture(details,event,data):
    if event=='tool' and data.get('phase') not in ('start','done'):return
    if event=='report':return
    item=json.loads(json.dumps(data,ensure_ascii=False,default=str))
    size=len(json.dumps(item,ensure_ascii=False).encode())
    details['size']=details.get('size',0)+size
    if details['size']>2_000_000:
        details['overflow']=True;return
    if event=='tool':
        if data.get('phase')=='done':details.setdefault('tools',[]).append(item)
        seen=details.setdefault('_tool_positions',set())
        key=item.get('call_id')
        if key and key in seen:return
        if key:seen.add(key)
        item={k:item.get(k) for k in ('call_id','name','phase')}
    details.setdefault('steps',[]).append({'event':event,'data':item,**item})
