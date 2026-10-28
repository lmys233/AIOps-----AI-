"""Replaceable embeddings. Lexical fallback is labelled, never called semantic."""
import math
import os
import httpx
from .catalog_models import digest


def validate(vector):
    if not isinstance(vector, (list, tuple)) or not 1 <= len(vector) <= 8192:
        raise ValueError('invalid vector dimensions')
    if any(type(x) not in (float, int) or not math.isfinite(x) for x in vector):
        raise ValueError('invalid vector components')
    norm = math.sqrt(sum(x*x for x in vector))
    if not norm or not math.isfinite(norm):
        raise ValueError('invalid vector norm')
    return tuple(x/norm for x in vector)


class LocalVectors:
    def __init__(self):
        from ..memory.embedding import LexicalHashEmbedder
        self.engine = LexicalHashEmbedder()
        self.identity = self.engine.name
        self.kind = 'lexical'

    async def embed(self, texts):
        return [validate(self.engine.embed(x)) for x in texts]


class HTTPVectors:
    kind = 'semantic'

    def __init__(self, base_url, model, key='', revision='', transport=None):
        self.url = base_url.rstrip('/') + '/embeddings'
        self.model, self.key, self.transport = model, key, transport
        self.identity = 'semantic-' + digest([base_url, model, revision])

    async def embed(self, texts):
        headers = {'Authorization': 'Bearer ' + self.key} if self.key else {}
        async with httpx.AsyncClient(timeout=8, transport=self.transport) as client:
            response = await client.post(self.url, headers=headers,
                                         json={'model': self.model, 'input': texts})
            response.raise_for_status()
            data = response.json()['data']
        data.sort(key=lambda r: r['index'])
        if [r['index'] for r in data] != list(range(len(texts))):
            raise ValueError('embedding response count/index mismatch')
        vectors = [validate(r['embedding']) for r in data]
        if len({len(v) for v in vectors}) != 1:
            raise ValueError('inconsistent embedding dimensions')
        return vectors


def build_provider():
    provider = os.getenv('AIOPS_INTENT_EMBEDDING_PROVIDER', 'bge').lower()
    if provider == 'bge':
        from .bge_provider import BGEVectors
        return BGEVectors(os.getenv('AIOPS_INTENT_BGE_MODEL', 'BAAI/bge-small-zh-v1.5'),
                          os.getenv('AIOPS_INTENT_BGE_PYTHON'),
                          os.getenv('AIOPS_INTENT_EMBEDDING_REVISION', ''))
    if provider == 'lexical':
        return LocalVectors()
    url, model = os.getenv('AIOPS_INTENT_EMBEDDING_URL'), os.getenv('AIOPS_INTENT_EMBEDDING_MODEL')
    if provider == 'http' and url and model:
        return HTTPVectors(url, model, os.getenv('AIOPS_INTENT_EMBEDDING_KEY', ''),
                           os.getenv('AIOPS_INTENT_EMBEDDING_REVISION', ''))
    raise ValueError('意图 embedding provider 必须是 bge/lexical/http，http 需配置 URL/model')
