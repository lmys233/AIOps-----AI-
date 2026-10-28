"""Literal composer completion. Semantic command execution remains model-driven."""
import asyncio,copy,time
from collections import OrderedDict
from ..alerts.query import combine


class Suggestions:
    def __init__(self,warnings,*,ttl=3,capacity=256):
        self.warnings=warnings;self.ttl=ttl;self.capacity=capacity
        self.cache=OrderedDict();self.pending={};self.slots=asyncio.Semaphore(8)

    async def read(self,scope,q,limit,services,snapshot):
        key=(scope.tenant,scope.observed_id,scope.env,scope.cluster,q,limit,tuple(sorted(services)),snapshot)
        now=time.monotonic()
        for old,(expiry,_) in list(self.cache.items()):
            if expiry<=now:self.cache.pop(old,None)
        if key in self.cache:
            self.cache.move_to_end(key);return copy.deepcopy(self.cache[key][1])
        if key not in self.pending:
            if len(self.pending)>=64:raise RuntimeError('candidate_query_busy')
            task=asyncio.create_task(self._load(key,scope,q,limit,services,snapshot))
            task.add_done_callback(lambda t:None if t.cancelled() else t.exception())
            self.pending[key]=task
        # A cancelled keystroke must not cancel the same lookup needed by another request.
        return copy.deepcopy(await asyncio.shield(self.pending[key]))

    async def _load(self,key,scope,q,limit,services,snapshot):
        try:
            async with asyncio.timeout(3),self.slots:
                where={} if not q else dict(field='warning_id' if q.startswith('warn_') else 'name',op='eq' if q.startswith('warn_') else 'contains',value=q)
                names=sorted(services);groups=[dict(field='service',op='in',value=names[i:i+50]) for i in range(0,len(names),50)]
                membership=groups[0] if len(groups)==1 else {'any':groups}
                result=await self.warnings.page(scope,where=combine(where,membership),snapshot=snapshot,limit=limit)
            result.update(completion='literal_name_or_id',model_calls=0)
            self.cache[key]=(time.monotonic()+self.ttl,result);self.cache.move_to_end(key)
            while len(self.cache)>self.capacity:self.cache.popitem(last=False)
            return result
        finally:self.pending.pop(key,None)

    async def close(self):
        tasks=list(self.pending.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True);self.cache.clear()
