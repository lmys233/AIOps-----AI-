"""20 轮页缓存；版本隔离防止并发读回填旧页覆盖新页。"""
import json
from uuid import uuid4


class PageCache:
    def __init__(self,client,tenant='default',ttl=86400):
        self.client,self.tenant,self.ttl=client,tenant,ttl

    def prefix(self,sid):return 'aiops:history:'+json.dumps([self.tenant,sid],ensure_ascii=False,separators=(',',':'))

    async def version(self,sid):
        key=self.prefix(sid)+':version'
        await self.client.set(key,uuid4().hex,nx=True,ex=self.ttl*2)
        value=await self.client.get(key)
        if value is None:raise RuntimeError('页版本不可读')
        await self.client.expire(key,self.ttl*2)
        return value.decode() if isinstance(value,bytes) else str(value)

    def key(self,sid,before,version,mode='full'):return self.prefix(sid)+':'+version+':'+str(before or 'latest')+(':'+mode if mode!='full' else '')

    async def get(self,sid,before,mode='full'):
        version=await self.version(sid)
        raw=await self.client.get(self.key(sid,before,version,mode))
        page=json.loads(raw) if raw else None
        if page is not None and (not isinstance(page,dict) or not isinstance(page.get('turns'),list)
                or len(page['turns'])>20 or type(page.get('has_more')) is not bool or page.get('page_size')!=20):
            raise ValueError('页缓存格式非法')
        return page,version

    async def put(self,sid,before,page,version,mode='full'):
        await self.client.set(self.key(sid,before,version,mode),json.dumps(page,ensure_ascii=False),ex=self.ttl)

    async def invalidate(self,sid):
        await self.client.set(self.prefix(sid)+':version',uuid4().hex,ex=self.ttl*2)
