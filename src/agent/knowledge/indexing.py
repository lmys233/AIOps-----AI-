import json
from .text import terms


async def cached_vectors(store,model,hashes):
    rows=await store.fetch('''SELECT text_hash,embedding::text AS embedding FROM agent_knowledge_embeddings
        WHERE tenant=$1 AND model=$2 AND text_hash=ANY($3::text[])''',store.tenant,model,hashes)
    return {r['text_hash']:json.loads(r['embedding']) for r in rows}


async def save_vectors(store,model,values):
    pool=await store.connect()
    async with pool.acquire() as c:
        await c.executemany('''INSERT INTO agent_knowledge_embeddings VALUES($1,$2,$3,$4::vector)
            ON CONFLICT DO NOTHING''',[(store.tenant,model,k,json.dumps(v)) for k,v in values.items()])


async def publish(store,document,chunks,model,chunker):
    pool=await store.connect()
    async with pool.acquire() as c,c.transaction():
        row=await c.fetchrow('SELECT revision,status,index_state FROM agent_knowledge_documents WHERE tenant=$1 AND id=$2 FOR UPDATE',store.tenant,document['id'])
        if not row or row['revision']!=document['revision'] or row['status']=='deleted':return False
        await c.execute('DELETE FROM agent_knowledge_chunks WHERE tenant=$1 AND document_id=$2',store.tenant,document['id'])
        await c.executemany('''INSERT INTO agent_knowledge_chunks VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11::vector,
            setweight(to_tsvector('simple',$12),'A')||setweight(to_tsvector('simple',$13),'B'))''',
            [(store.tenant,document['id'],document['version'],r['id'],r['parent_id'],r['ordinal'],r['text'],r['parent_text'],
              json.dumps({**r['metadata'],'embedding_model':model,'embedding_dimensions':len(r['vector'])},ensure_ascii=False),
              model,json.dumps(r['vector']),' '.join(terms(document['title'])),' '.join(terms(r['embedding_text']))) for r in chunks])
        await c.execute('''UPDATE agent_knowledge_documents SET index_state='ready',index_error='',chunk_count=$3,
            index_model=$4,index_chunker=$5 WHERE tenant=$1 AND id=$2''',store.tenant,document['id'],len(chunks),model,chunker)
    return True


async def failed(store,kid,revision,error):
    await store.fetch('''UPDATE agent_knowledge_documents SET index_state='failed',index_error=$4
        WHERE tenant=$1 AND id=$2 AND revision=$3 AND status!='deleted' RETURNING id''',store.tenant,kid,revision,error[:500])
