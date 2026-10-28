"""Read only bounded, same-task archive fields; no tools/evidence blobs in prompts."""
from ..llm.context import CURRENT,context_scope

def bind(service,request,scope):
    async def load():
        history=getattr(service,'history',None);store=getattr(history,'store',None)
        if not store or not hasattr(store,'context_turns'):return []
        from ..execution.runtime import CURRENT as EXECUTION
        execution=EXECUTION.get();ctx=CURRENT.get()
        anchor=execution.body['started_at'] if execution else None
        task_id=ctx.task.get('task_id','')
        return await store.context_turns(scope.session_id,scope.observed_id,task_id,anchor,scope.env,scope.cluster)
    async def preferences(ctx):
        from ..preferences.tools import load as load_preferences
        return await load_preferences(service,ctx)
    return context_scope(scope.model_dump(),request.message,load,preferences if getattr(service,'preferences',None) else None)

def stamp(body,ctx):
    if not ctx.audits:return body
    calls=[]
    for audit in ctx.audits:
        entry={k:v for k,v in audit.items() if k not in ('summaries','sources')}
        entry['summary_refs']=[{'cache_key':s['cache_key'],'version':s['version'],'source':s['source'],'url':s['url']} for s in audit['summaries']]
        entry['source_refs']=audit['sources'];calls.append(entry)
    body.setdefault('explanation',{})['model_context']={'version':1,'scope':ctx.scope,'calls':calls,
        'note':'单次提示/schema/输出预留的本地估算；累计费用见model_usage，历史摘录不证明当前状态'}
    if any(a.get('history_error') or a.get('summary_errors') for a in ctx.audits):
        body.setdefault('degraded',[]).append('历史上下文读取或摘录未完成：保留原档案，失败项按引用回查；本轮未将其当作已确认事实。')
    if any(a['status']=='blocked' for a in ctx.audits):
        body.setdefault('degraded',[]).append('部分受保护提示超出配置的单次上下文窗口，已停止该模型请求；没有通过删除关键事实来凑长度。')
    return body
