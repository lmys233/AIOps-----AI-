"""Precomputed examples; provider/text hashes allow incremental updates and restart reuse."""
import asyncio
from .catalog_models import digest, normalized
from .vector_provider import validate


class ExampleIndex:
    def __init__(self, provider, store=None):
        self.provider, self.store = provider, store
        self.cached = {}
        self.lock = asyncio.Lock()
        self.last_error = ''

    def key(self, text):
        return digest([self.provider.identity, normalized(text)])

    async def prepare(self, routes):
        examples = {self.key(t): t for r in routes if r['enabled'] for t in r['examples']}
        async with self.lock:
            missing = [k for k in examples if k not in self.cached]
            if missing and self.store:
                stored = await self.store.vectors(missing)
                self.cached.update({k: validate(v) for k, v in stored.items()})
            missing = [k for k in examples if k not in self.cached]
            for start in range(0, len(missing), 64):
                keys = missing[start:start+64]
                values = await self.provider.embed([examples[k] for k in keys])
                if len(keys) != len(values):
                    raise ValueError('embedding count mismatch')
                batch = {k: validate(v) for k, v in zip(keys, values)}
                if self.store:
                    await self.store.save_vectors(self.provider.identity, batch)
                self.cached.update(batch)
            # Bound per-process memory; database vectors remain reusable after restart.
            self.cached = {k: self.cached[k] for k in examples}
        return len(examples)

    async def match(self, text, routes, threshold=.84, margin=.12, prepared=False):
        if not prepared:
            await self.prepare(routes)
        query = validate((await self.provider.embed([normalized(text)]))[0])
        scores = []
        for route in routes:
            if not route['enabled'] or not route['examples']:
                continue
            def similarity(example):
                v = self.cached[self.key(example)]
                if len(v) != len(query):
                    raise ValueError('embedding model dimensions changed; bump provider revision')
                return sum(a*b for a, b in zip(v, query))
            best = max(route['examples'], key=similarity)
            scores.append({'route_id': route['id'], 'handler': route['handler'],
                           'score': round(similarity(best), 6), 'example': best})
        scores.sort(key=lambda r: (-r['score'], r['route_id']))
        best = scores[0] if scores else None
        other = next((r for r in scores if r['handler'] != best['handler']), None) if best else None
        gap = best['score'] - (other['score'] if other else 0) if best else 0
        accepted = bool(best and best['score'] >= threshold and gap >= margin)
        return {'provider': self.provider.identity, 'kind': self.provider.kind,
                'threshold': threshold, 'margin': margin, 'gap': round(gap, 6),
                'accepted': accepted, 'candidates': scores[:5], 'best': best}
