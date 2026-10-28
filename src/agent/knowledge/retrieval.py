"""Filtering happens before ranking; RRF ranks are never confidence probabilities."""
import asyncio
import json
from .text import terms
from ..intent.vector_provider import validate

BASE=''' FROM agent_knowledge_chunks c JOIN agent_knowledge_documents d
 ON d.tenant=c.tenant AND d.id=c.document_id AND d.version=c.version
 WHERE c.tenant=$1 AND d.status='active' AND d.index_state='ready'
 AND (d.observed_id IS NULL OR d.observed_id=$2) AND ($3='' OR d.category=$3)
 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(coalesce($4::jsonb->'dates','[]'::jsonb)
    ||coalesce($4::jsonb->'exact_codes','[]'::jsonb)) AS required(value)
    WHERE strpos(lower(c.text||c.parent_text||c.metadata::text),lower(required.value))=0)
 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(coalesce($4::jsonb->'exact_entities','[]'::jsonb)) AS required(value)
    WHERE NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(coalesce(c.metadata->'entities','[]'::jsonb)
        ||coalesce(c.metadata->'code_terms','[]'::jsonb)) AS recorded(value) WHERE lower(recorded.value)=lower(required.value)))'''
COLUMNS='c.document_id,c.version,c.id,c.parent_id,c.text,c.metadata'


def rows(data):
    return [{**dict(row),'metadata':json.loads(row['metadata']) if isinstance(row['metadata'],str) else row['metadata']} for row in data]


def rrf(rankings,k=60,limit=10):
    scores={};items={};routes={};details={}
    for source,ranking in rankings:
        seen=set()
        for rank,item in enumerate(ranking,1):
            key=(item['document_id'],item['version'],item['id'])
            if key in seen:continue
            seen.add(key);items[key]=item;scores[key]=scores.get(key,0)+1/(k+rank)
            routes.setdefault(key,[]).append(source)
            details.setdefault(key,{})[source]={'rank':rank,'routes':item.get('retrieval_routes',[]),
                                              'score':item.get('rrf_score'), 'components':item.get('fusion_details',{})}
    keys=sorted(scores,key=lambda key:(-scores[key],key))[:limit]
    return [{**items[key],'rrf_score':round(scores[key],8),'retrieval_routes':routes[key],'fusion_details':details[key]} for key in keys]


async def keyword(store,query,oid,filters):
    tokens=terms(query,64)
    if not tokens:return []
    tsquery=' OR '.join('"'+token.replace('"','')+'"' for token in tokens)
    data=await store.fetch('SELECT '+COLUMNS+',ts_rank_cd(c.terms,websearch_to_tsquery(\'simple\',$5)) AS lexical_score'+BASE+'''
      AND c.terms @@ websearch_to_tsquery('simple',$5)
      ORDER BY lexical_score DESC,c.document_id,c.ordinal LIMIT 40''',store.tenant,oid,filters.get('category',''),json.dumps(filters),tsquery)
    return rows(data)


async def semantic(service,query,oid,filters):
    vector=validate((await service.provider.embed([query]))[0])
    data=await service.store.fetch('SELECT '+COLUMNS+',1-(c.embedding <=> $5::vector) AS similarity'+BASE+'''
        AND c.model=$6 AND vector_dims(c.embedding)=$7
        AND 1-(c.embedding <=> $5::vector)>=$8
        ORDER BY c.embedding <=> $5::vector,c.document_id,c.ordinal LIMIT 40''',
        service.store.tenant,oid,filters.get('category',''),json.dumps(filters),json.dumps(vector),service.provider.identity,len(vector),service.threshold)
    return rows(data)


def exact(items,slots):
    # Exact strings are constraints only when present in the user's original query.
    required=list(slots.get('exact_codes') or [])+list(slots.get('dates') or [])
    return [item for item in items if all(value.lower() in
        (item['text']+' '+str(item['metadata'])).lower() for value in required)]


async def hybrid(service,query,oid,slots):
    outcomes=await asyncio.gather(keyword(service.store,query,oid,slots),semantic(service,query,oid,slots),return_exceptions=True)
    rankings=[];notes=[]
    for source,result in zip(('keyword','vector'),outcomes):
        if isinstance(result,BaseException):
            if isinstance(result,asyncio.CancelledError):raise result
            notes.append(source+'检索未完成：'+type(result).__name__)
        else:rankings.append((source,exact(result,slots)))
    if not rankings:raise ConnectionError('知识向量/关键词两路均不可用')
    return rrf(rankings,limit=40),notes


async def search(service,params,oid):
    original=params['query'];rewritten=params.get('rewritten_query') or original
    slots=params.get('filters') or {};queries=[('original',original)]
    if rewritten!=original:queries.append(('rewritten',rewritten))
    outcomes=await asyncio.gather(*(hybrid(service,q,oid,slots) for _,q in queries),return_exceptions=True)
    rankings=[];notes=[]
    for (name,_),result in zip(queries,outcomes):
        if isinstance(result,BaseException):
            if isinstance(result,asyncio.CancelledError):raise result
            notes.append(name+'检索失败：'+type(result).__name__)
        else:
            hits,degraded=result;rankings.append((name,hits));notes.extend(degraded)
    if not rankings:raise ConnectionError('知识检索未完成')
    return {'hits':rrf(rankings,limit=params.get('top_k',10)),'original_query':original,
            'rewritten_query':rewritten,'slots':slots,'degraded':notes,'ranking':'two-stage RRF(k=60), not confidence'}


async def recall(store,references,oid):
    seen=set();parents=[];docs={}
    for ref in references:
        key=(ref['document_id'],ref['version'],ref['parent_id'])
        if key in seen:continue
        seen.add(key)
        row=await store.one('''SELECT c.parent_text,c.metadata,d.title,d.uploader,d.category,d.updated_at
          FROM agent_knowledge_chunks c JOIN agent_knowledge_documents d ON c.tenant=d.tenant AND c.document_id=d.id AND c.version=d.version
          WHERE c.tenant=$1 AND c.document_id=$2 AND c.version=$3 AND c.parent_id=$4 AND d.status='active'
          AND d.index_state='ready' AND (d.observed_id IS NULL OR d.observed_id=$5) LIMIT 1''',store.tenant,*key,oid)
        if not row:continue
        meta=json.loads(row['metadata']) if isinstance(row['metadata'],str) else row['metadata']
        doc_key=(key[0],key[1])
        if doc_key not in docs:
            original=await store.get(key[0])
            if original['version']!=key[1] or original['status']!='active' or original['index_state']!='ready':continue
            if original['observed_id'] not in (None,oid):continue
            docs[doc_key]={'document_id':key[0],'version':key[1],'title':original['title'],
                'content':original['content'],'uploader':original['uploader'],'category':original['category'],'updated_at':str(original['updated_at'])}
        parents.append({'document_id':key[0],'version':key[1],'parent_id':key[2],
                        'text':row['parent_text'],'metadata':meta})
    return {'parents':parents,'documents':list(docs.values()),'requested':len(references),'deduplicated_parents':len(seen),
            'unavailable_parents':len(seen)-len(parents)}
