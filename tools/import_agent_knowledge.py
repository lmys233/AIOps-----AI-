"""Import an explicit knowledge pack through the live API, then verify PG/BGE reads.

Run from repository root. Existing inactive/deleted documents are never revived.
No observed SDK requests, chat creation or direct database mutation.
"""
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import httpx
from agent.common.envfile import load_env_file
from agent.knowledge.models import DocumentInput
from agent.knowledge.service import build_service
from agent.knowledge.tools import kb_search, kb_document_read
from agent.knowledge.retrieval import semantic

PACK = ROOT / 'docs/knowledge/agent-basics.json'
RECEIPT = ROOT / 'docs/knowledge/agent-basics-import.json'


def save(report):
    RECEIPT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


async def listing(client, status=''):
    documents = []
    while True:
        response = await client.get('/v1/knowledge', params={'offset':len(documents), 'limit':50, 'status':status})
        response.raise_for_status()
        page = response.json()
        documents.extend(page['documents'])
        if not page['has_more']:
            return documents


async def preflight(client, pack):
    existing = await listing(client) + await listing(client, 'deleted')
    plan = []
    for item in pack['documents']:
        data = DocumentInput.model_validate({**item, 'uploader':'admin', 'status':'active', 'observed_id':None})
        matches = [d for d in existing if d['title'] == data.title and d['observed_id'] is None]
        if len(matches) > 1:
            raise ValueError('同名知识不唯一，保留现状：' + data.title)
        old = None
        if matches:
            response = await client.get('/v1/knowledge/' + matches[0]['id'])
            response.raise_for_status()
            old = response.json()
            if old['content'] != data.content:
                raise ValueError('同名知识内容不同，拒绝覆盖：' + data.title)
        plan.append((data.model_dump(), old))
    return plan


async def import_pack(client, plan, report):
    for data, old in plan:
        action = 'reused' if old else 'created'
        if old:
            document = old
            if old['status'] != 'active':
                action = 'skipped_existing_' + old['status']
        else:
            response = await client.post('/v1/knowledge', json=data)
            response.raise_for_status()
            document = response.json()
        report['documents'].append({k:document.get(k) for k in
            ('id','title','version','revision','uploader','category','status','observed_id','index_state','chunk_count','index_model')})
        report['documents'][-1].update(action=action, content_sha256=hashlib.sha256(data['content'].encode()).hexdigest())
        save(report)
        print(action + ': ' + document['title'], flush=True)


async def wait_ready(client, report):
    deadline = time.monotonic() + 180
    while True:
        pending = []
        for item in report['documents']:
            if item['action'].startswith('skipped_'):
                continue
            response = await client.get('/v1/knowledge/' + item['id'])
            response.raise_for_status()
            doc = response.json()
            for key in ('index_state','chunk_count','index_model','index_error'):
                item[key] = doc.get(key)
            if doc['index_state'] == 'failed':
                raise RuntimeError(doc['title'] + ': ' + str(doc.get('index_error')))
            if doc['index_state'] != 'ready':
                pending.append(doc['id'])
        save(report)
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError('索引仍在排队：' + str(len(pending)))
        await asyncio.sleep(1)


async def verify(report):
    service = build_service()
    if service is None:
        raise RuntimeError('知识库数据库未配置')
    ids = [d['id'] for d in report['documents'] if not d['action'].startswith('skipped_')]
    try:
        records = await service.store.fetch('''SELECT c.document_id,count(*) AS chunks,
            min(vector_dims(c.embedding)) AS min_dim,max(vector_dims(c.embedding)) AS max_dim,
            bool_and(pg_typeof(c.embedding)::text='vector') AS real_vector
            FROM agent_knowledge_chunks c WHERE c.tenant=$1 AND c.document_id=ANY($2::text[])
            GROUP BY c.document_id''', service.store.tenant, ids)
        assert len(records) == len(ids) and all(r['real_vector'] and r['min_dim']==r['max_dim']==512 for r in records)
        report['postgres'] = {'documents':len(ids), 'chunks':sum(r['chunks'] for r in records), 'dimensions':512}
        context = SimpleNamespace(knowledge=service)
        cases = [(1,'ReAct 的行动反馈循环是什么？'), (2,'Agent 工具调用失败后怎样分类并进行重试兜底？'),
                 (3,'Agent 的短期记忆和长期记忆有什么区别？'), (4,'RAG 混合检索如何融合关键词与向量结果？')]
        report['retrieval_checks'] = []
        for index, query in cases:
            expected = report['documents'][index]['id']
            result = (await kb_search(context, {'query':query,'top_k':10,'filters':{}})).data
            dense = await semantic(service, query, None, {})
            assert not result['degraded'] and any(h['document_id']==expected for h in result['hits'])
            assert any(h['document_id']==expected for h in dense), '真实向量未召回预期文档：' + query
            refs = [{k:h[k] for k in ('document_id','version','parent_id')} for h in result['hits']]
            original = (await kb_document_read(context, {'references':refs})).data
            report['retrieval_checks'].append({'query':query, 'expected_document_id':expected,
                'hybrid_hits':len(result['hits']), 'vector_hits':len(dense), 'originals':len(original['documents']),
                'passed':any(d['document_id']==expected for d in original['documents'])})
            assert report['retrieval_checks'][-1]['passed']
            print('检索及原文通过：' + query, flush=True)
    finally:
        await service.close()


async def main():
    load_env_file()
    pack = json.loads(PACK.read_text(encoding='utf-8'))
    report = {'pack_id':pack['pack_id'], 'started_at':datetime.now(timezone.utc).isoformat(),
              'scope':'current tenant / platform shared', 'documents':[], 'phase':'importing'}
    token = os.getenv('AIOPS_AGENT_TOKEN','')
    headers = {'Authorization':'Bearer ' + token} if token else {}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8020', headers=headers, timeout=60) as client:
        config = await client.get('/v1/knowledge/config')
        config.raise_for_status()
        assert config.json()['embedding_kind'] == 'semantic', '正式环境未配置语义向量'
        plan = await preflight(client, pack)
        try:
            await import_pack(client, plan, report)
            await wait_ready(client, report)
            report['phase'] = 'indexed'
            save(report)
            await verify(report)
            report['phase'] = 'complete'
            report['completed_at'] = datetime.now(timezone.utc).isoformat()
        finally:
            save(report)
    print('导入与检索完成：' + str(len(report['documents'])) + ' 篇', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
