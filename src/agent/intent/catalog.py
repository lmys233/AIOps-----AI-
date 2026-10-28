"""Local cache-aside: commit to PG, invalidate locally, refresh other workers by TTL."""
import asyncio
import copy
import time
from .catalog_models import RouteDefinition, defaults, digest
from .catalog_store import CatalogConflict


class IntentCatalog:
    def __init__(self, store=None, ttl=5):
        self.store, self.ttl = store, ttl
        self.cached, self.until = None, 0
        self.lock = asyncio.Lock()

    async def snapshot(self, strict=False):
        async with self.lock:
            if not strict and self.cached and time.monotonic() < self.until:
                notes = ['意图目录缓存处于数据库降级状态，管理不可用'] if self.cached.get('degraded') else []
                return copy.deepcopy(self.cached), notes
            try:
                if self.store is None:
                    raise ConnectionError('unconfigured')
                data = await self.store.read()
                data['routes'] = [RouteDefinition.model_validate(r).model_dump(mode='json')
                                  for r in data['routes']]
                if len(data['routes']) > 64:
                    raise ValueError('目录超过 64 条路由')
                self.cached, self.until = copy.deepcopy(data), time.monotonic() + self.ttl
                return data, []
            except Exception:
                if strict:
                    raise ConnectionError('意图数据库不可用，未保存或加载管理目录') from None
                # Retry after a short interval, not a database connection storm per utterance.
                data = copy.deepcopy(self.cached)
                if data is None:
                    rows = defaults()
                    data = {'revision': 'builtin-' + digest(rows), 'routes': rows}
                data['degraded'] = True
                self.cached, self.until = copy.deepcopy(data), time.monotonic() + 1
                return data, ['意图数据库不可用，使用最后有效目录/内置规则；管理操作不可用']

    async def mutate(self, action, rid, route, revision):
        async with self.lock:
            if self.store is None:
                raise ConnectionError('意图数据库未配置')
            current = await self.store.read()
            if revision != current['revision']:
                raise CatalogConflict('目录版本冲突，请刷新')
            rows = current['routes']
            found = next((i for i, r in enumerate(rows) if r['id'] == rid), None)
            if action == 'create':
                if found is not None:
                    raise CatalogConflict('路由 ID 已存在')
                if len(rows) >= 64:
                    raise ValueError('目录最多 64 条路由')
                rows.append(route.model_dump(mode='json'))
            elif found is None:
                raise KeyError('路由不存在')
            elif action == 'delete':
                rows.pop(found)
            else:
                if route.id != rid:
                    raise ValueError('更新不能修改路由 ID')
                rows[found] = route.model_dump(mode='json')
            result = await self.store.write(rows, revision)
            self.cached, self.until = None, 0
            return result
