import asyncio
import os
from .store import KnowledgeStore
from .chunking import build,records
from .indexing import cached_vectors,save_vectors,publish,failed
from ..intent.vector_provider import build_provider,validate


class KnowledgeService:
    def __init__(self,store,provider,chunker=None,threshold=.65,owns_provider=False):
        self.store,self.provider=store,provider;self.chunker=chunker or build()
        self.threshold=threshold;self.owns_provider=owns_provider;self.tasks={};self.worker=None
        self.semaphore=asyncio.Semaphore(1);self.last_error=''

    def schedule(self,kid):
        if kid not in self.tasks:self.tasks[kid]=asyncio.create_task(self.index(kid))

    async def index(self,kid):
        doc=None
        try:
            async with self.semaphore:
                doc=await self.store.get(kid)
                if doc['status']=='deleted' or doc['index_state']!='pending':return
                chunks=records(doc,self.chunker)
                if not chunks:raise ValueError('清洗后没有可索引内容')
                model=self.provider.identity;hashes=[r['metadata']['content_hash'] for r in chunks]
                vectors=await cached_vectors(self.store,model,hashes)
                missing={r['metadata']['content_hash']:r['embedding_text'] for r in chunks if r['metadata']['content_hash'] not in vectors}
                keys=list(missing)
                for start in range(0,len(keys),32):
                    batch=keys[start:start+32]
                    values=await asyncio.wait_for(self.provider.embed([missing[k] for k in batch]),60)
                    if len(values)!=len(batch):raise ValueError('embedding count mismatch')
                    encoded={k:validate(v) for k,v in zip(batch,values)}
                    await save_vectors(self.store,model,encoded);vectors.update(encoded)
                for row in chunks:row['vector']=vectors[row['metadata']['content_hash']]
                if len({len(r['vector']) for r in chunks})!=1:raise ValueError('embedding dimensions changed')
                await publish(self.store,doc,chunks,model,self.chunker.identity)
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.last_error=type(exc).__name__+': '+str(exc)[:300]
            if doc:
                try:await failed(self.store,kid,doc['revision'],self.last_error)
                except Exception:pass
        finally:self.tasks.pop(kid,None)

    async def recover(self):
        while True:
            try:
                rows=await self.store.fetch("SELECT id FROM agent_knowledge_documents WHERE tenant=$1 AND index_state='pending' AND status!='deleted' ORDER BY updated_at LIMIT 40",self.store.tenant)
                for row in rows:self.schedule(row['id'])
            except asyncio.CancelledError:raise
            except Exception as exc:self.last_error=type(exc).__name__
            await asyncio.sleep(10)

    async def stop_workers(self):
        tasks=list(self.tasks.values())+([self.worker] if self.worker else [])
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    async def close(self):
        await self.stop_workers();await self.store.close()
        if self.owns_provider and hasattr(self.provider,'close'):await self.provider.close()


def build_service(provider=None):
    dsn=os.getenv('AIOPS_DATABASE_URL')
    if not dsn:return None
    return KnowledgeService(KnowledgeStore(dsn,os.getenv('AIOPS_TENANT','default')),provider or build_provider(),
        build(os.getenv('AIOPS_KB_CHUNKER','hierarchical'),size=int(os.getenv('AIOPS_KB_CHUNK_SIZE','360')),
              overlap=int(os.getenv('AIOPS_KB_CHUNK_OVERLAP','80')),parent_size=int(os.getenv('AIOPS_KB_PARENT_SIZE','2400'))),
        float(os.getenv('AIOPS_KB_VECTOR_THRESHOLD','.65')),owns_provider=provider is None)
