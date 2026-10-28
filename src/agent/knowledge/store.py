import json
from uuid import uuid4
from ..observed.store import RegistryStore
from .schema import DDL
from .models import Conflict


def document(row):
    result=dict(row)
    for key in ('tags',):
        if isinstance(result.get(key),str):result[key]=json.loads(result[key])
    return result


class KnowledgeStore(RegistryStore):
    ddl=DDL
    def __init__(self,dsn,tenant='default'):
        super().__init__(dsn);self.tenant=tenant

    async def get(self,kid):
        row=await self.one('SELECT * FROM agent_knowledge_documents WHERE tenant=$1 AND id=$2',self.tenant,kid)
        if not row:raise KeyError(kid)
        return document(row)

    async def listing(self,offset=0,limit=20,status='',category='',query=''):
        columns='id,title,uploader,category,tags,observed_id,status,revision,version,index_state,index_error,index_model,index_chunker,chunk_count,created_at,updated_at'
        rows=await self.fetch('SELECT '+columns+''' FROM agent_knowledge_documents WHERE tenant=$1
          AND (($2='' AND status!='deleted') OR status=$2) AND ($3='' OR category=$3)
          AND ($4='' OR title ILIKE '%'||$4||'%' OR content ILIKE '%'||$4||'%')
          ORDER BY updated_at DESC,id DESC LIMIT $5 OFFSET $6''',self.tenant,status,category,query,limit+1,offset)
        return {'documents':[document(r) for r in rows[:limit]],'has_more':len(rows)>limit,'offset':offset,'limit':limit}

    async def mutate(self,data,kid=None,revision=None):
        kid=kid or 'kb_'+uuid4().hex;pool=await self.connect();new_revision=uuid4().hex
        async with pool.acquire() as c,c.transaction():
            if revision:
                old=await c.fetchrow('SELECT revision,status FROM agent_knowledge_documents WHERE tenant=$1 AND id=$2 FOR UPDATE',self.tenant,kid)
                if not old:raise KeyError(kid)
                if old['revision']!=revision:raise Conflict('知识版本已变更，请刷新后再编辑')
                if old['status']=='deleted':raise ValueError('已删除知识不可重新启用')
                await c.execute('''UPDATE agent_knowledge_documents SET title=$3,content=$4,uploader=$5,category=$6,
                    tags=$7::jsonb,observed_id=$8,status=$9,revision=$10,version=version+1,index_state='pending',
                    index_error='',chunk_count=0,updated_at=now() WHERE tenant=$1 AND id=$2''',
                    self.tenant,kid,data.title,data.content,data.uploader,data.category,json.dumps(data.tags),data.observed_id,data.status,new_revision)
            else:
                await c.execute('''INSERT INTO agent_knowledge_documents(tenant,id,title,content,uploader,category,tags,
                    observed_id,status,revision,version) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,1)''',
                    self.tenant,kid,data.title,data.content,data.uploader,data.category,json.dumps(data.tags),data.observed_id,data.status,new_revision)
            row=await c.fetchrow('SELECT * FROM agent_knowledge_documents WHERE tenant=$1 AND id=$2',self.tenant,kid)
            await c.execute('INSERT INTO agent_knowledge_versions VALUES($1,$2,$3,$4::jsonb)',
                            self.tenant,kid,row['version'],json.dumps(document(row),default=str,ensure_ascii=False))
        return document(row)

    async def delete(self,kid,revision):
        rows=await self.fetch('''UPDATE agent_knowledge_documents SET status='deleted',revision=$3,index_state='disabled',
            updated_at=now() WHERE tenant=$1 AND id=$2 AND revision=$4 RETURNING id''',self.tenant,kid,uuid4().hex,revision)
        if not rows:
            await self.get(kid);raise Conflict('删除前知识版本已变更')
        return {'deleted':True,'id':kid}

    async def reindex(self,kid,revision):
        rows=await self.fetch('''UPDATE agent_knowledge_documents SET index_state='pending',index_error=''
            WHERE tenant=$1 AND id=$2 AND revision=$3 AND status!='deleted' RETURNING id''',self.tenant,kid,revision)
        if not rows:
            await self.get(kid);raise Conflict('知识已变更或删除，请刷新')
        return await self.get(kid)

    async def version(self,kid,version):
        await self.get(kid)
        row=await self.one('SELECT body FROM agent_knowledge_versions WHERE tenant=$1 AND document_id=$2 AND version=$3',self.tenant,kid,version)
        if not row:raise KeyError('version not found')
        return json.loads(row['body']) if isinstance(row['body'],str) else row['body']
