"""仅缓存未命中时恢复最新工作上下文；旧历史页不进入模型记忆。"""
from .codec import as_turn


class ReadThroughWorking:
    def __init__(self,base,history):self.base,self.history=base,history

    def __getattr__(self,key):return getattr(self.base,key)

    async def turns(self,sid,limit=20):
        try:turns=await self.base.turns(sid,limit=limit)
        except Exception:turns=[]
        if turns:return turns
        from ..observed.context import current,body_scope
        scope=current()
        if scope and hasattr(self.history.store,'model_turns'):
            public=sid.removeprefix(scope.namespace+':')
            rows=await self.history.store.model_turns(public,scope.observed_id,limit)
            turns=[as_turn(r) for r in rows]
        else:
            data=await self.history.page(sid)
            turns=[as_turn(r) for r in data['turns'] if not scope or body_scope(r.get('response') or {})==scope.observed_id]
        try:
            if turns:await self.base.restore_turns(sid,turns)
        except Exception:pass  # 完整历史已在 PG；缓存不可用仍返回有界原文
        return turns[-min(20,max(1,limit)):]
