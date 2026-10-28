"""档案回源与缓存旁路；数据库失败不作为空历史。"""
from .store import PgHistoryStore
from .cache import PageCache


class HistoryService:
    def __init__(self,store,cache=None):self.store,self.cache,self.dirty=store,cache,set()

    async def page(self,sid,before=None,mode='full'):
        if mode not in ('full','view'):raise ValueError('历史模式必须为 full/view')
        from ..observed.context import current,session_key
        scope=current();cache_sid=session_key(sid)
        notes=[];version=None
        if self.cache:
            try:
                if cache_sid in self.dirty:
                    await self.cache.invalidate(cache_sid);self.dirty.discard(cache_sid)
                data,version=await self.cache.get(cache_sid,before,mode) if mode!='full' else await self.cache.get(cache_sid,before)
                if data is not None:return dict(data,source='cache',degraded=notes)
            except Exception as exc:notes.append('历史页缓存不可读：'+type(exc).__name__+'；本次回源数据库')
        if mode=='view':
            from .payloads import history_page
            data=await history_page(self.store,sid,before,scope.observed_id if scope else None)
        else:data=await self.store.page(sid,before,observed_id=scope.observed_id) if scope else await self.store.page(sid,before)
        if self.cache and version:
            try:
                if mode=='full':await self.cache.put(cache_sid,before,data,version)
                else:await self.cache.put(cache_sid,before,data,version,mode)
            except Exception as exc:notes.append('历史页缓存回填失败：'+type(exc).__name__)
        return dict(data,source='database',degraded=notes)

    async def save(self,sid,tid,payload):
        number=await self.store.save(sid,tid,payload)
        notes=[]
        if self.cache:
            from ..observed.context import session_key
            key=session_key(sid)
            try:
                await self.cache.invalidate(sid)
                if key!=sid:await self.cache.invalidate(key)
            except Exception as exc:
                self.dirty.add(sid);self.dirty.add(key)
                notes.append('历史档案已保存，页缓存更新失败：'+type(exc).__name__)
        return number,notes

    async def delete(self,sid):
        await self.store.delete(sid)
        notes=[]
        if self.cache:
            try:await self.cache.invalidate(sid)
            except Exception as exc:
                self.dirty.add(sid);notes.append('历史档案已删除，缓存清理失败：'+type(exc).__name__)
        return notes


def build_history(env):
    dsn=(env.get('AIOPS_HISTORY_DATABASE_URL') or env.get('AIOPS_DATABASE_URL') or '').strip()
    if not dsn:return None
    tenant=env.get('AIOPS_TENANT') or 'default';cache=None
    if env.get('AIOPS_REDIS_URL'):
        import redis.asyncio as redis
        cache=PageCache(redis.from_url(env['AIOPS_REDIS_URL']),tenant)
    return HistoryService(PgHistoryStore(dsn,tenant),cache)
