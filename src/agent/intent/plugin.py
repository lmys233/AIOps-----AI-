"""Routing plugin: managed rules -> precomputed examples -> one main-model proposal.

Only routing data are editable. Slots, task continuation and executor authorization
remain outside this plugin. It never reads observed-system telemetry.
"""
import asyncio
import re
import time
from dataclasses import dataclass
from ..common.enums import Intent
from .rules import ACTIVE_RULES, RuleHit, Pattern, _score_rule, match_rules, is_decided_by_rules
from .scopes import FaultScope
from .vector_index import ExampleIndex
from .routing_policy import risk_reason


class BoundedRegex:
    def __init__(self, pattern):
        import regex
        self.regex = regex.compile(pattern)
        self.pattern = pattern

    def search(self, text):
        return self.regex.search(text, timeout=.003)


@dataclass
class PreparedRoute:
    hit: RuleHit
    routes: list
    trace: dict
    notes: list


def managed_hit(text, routes, slots):
    until = time.monotonic() + .15
    for row in sorted(routes, key=lambda r: (r['priority'], r['id'])):
        if time.monotonic() > until:
            raise TimeoutError('rule budget')
        if not row['enabled']:
            continue
        patterns = tuple(Pattern(BoundedRegex(p['pattern']), p['weight'], p['note']) for p in row['patterns'])
        score, matched = _score_rule(text, patterns)
        if score < row['gate']:
            continue
        if row.get('scope'):
            scope = row['scope']
            if not FaultScope(BoundedRegex(scope['abstract']), BoundedRegex(scope['assertion'])).matches(text, slots):
                continue
        handler = Intent(row['handler'])
        if handler == Intent.KNOWLEDGE_QA and ACTIVE_RULES.imperative_write.search(text):
            continue
        return RuleHit(handler, score, row['id'], matched)
    return RuleHit(Intent.OUT_OF_SCOPE, .2, 'R6_fallback', ())


class IntentRoutingPlugin:
    name = 'intent-routing-v1'

    def __init__(self, catalog, provider, threshold=.84, margin=.12):
        if not .5 <= threshold <= 1 or not 0 < margin <= 1:
            raise ValueError('invalid vector threshold/margin')
        self.catalog = catalog
        self.index = ExampleIndex(provider, catalog.store)
        self.threshold, self.margin = threshold, margin
        self.warm_task = None
        self.warm_error = ''

    async def prepare(self, text, slots):
        data, notes = await self.catalog.snapshot()
        trace = {'plugin': self.name, 'catalog_revision': data['revision'],
                 'catalog_degraded': bool(data.get('degraded')), 'vector': {'attempted': False}}
        risk = risk_reason(text)
        if risk:
            trace['scope_status'] = 'unsafe'
            trace['risk_rejection'] = risk
            return PreparedRoute(RuleHit(Intent.OUT_OF_SCOPE, 1., 'protected_risk', (risk,)), data['routes'], trace, notes)
        # Preserve the original specific-fault/advisor and explicit dangerous-action guard.
        guard = match_rules(text, slots_config=slots)
        generic_advice = guard.score < .7 and bool(guard.matched) and all(re.fullmatch(
            r'(怎么|如何|怎样)(解决|修复|处理|恢复|搞定)|解决(方案|办法)|处置(方案|建议)|修复方案',m) for m in guard.matched)
        if generic_advice:
            trace['advice_review_required'] = True
        if guard.intent == Intent.REMEDIATION and is_decided_by_rules(guard) and not generic_advice:
            trace['protected_rule'] = guard.rule_id
            return PreparedRoute(guard, data['routes'], trace, notes)
        try:
            hit = managed_hit(text[:4000], data['routes'], slots)
        except (TimeoutError, ValueError):
            hit = guard
            notes.append('可编辑规则执行超时或非法，使用内置规则判断；未采信失败规则')
            trace['rule_error'] = 'RULE_EVALUATION_FAILED'
        from .guidance import needs_semantics
        if needs_semantics(text,hit):
            trace['guidance_review_required']=True
            notes.append('通用方法问句同时命中异常词；规则只作候选，继续向量与模型复核语义')
        return PreparedRoute(hit, data['routes'], trace, notes)

    async def vector(self, text, prepared, slots):
        started=time.perf_counter()
        # Scope applies to examples as well as regex. Never promote an abstract
        # knowledge question into incident advice because it resembles an example.
        routes = []
        for row in prepared.routes:
            if row.get('scope'):
                s = row['scope']
                try:
                    if not FaultScope(BoundedRegex(s['abstract']), BoundedRegex(s['assertion'])).matches(text, slots):
                        continue
                except TimeoutError:
                    continue
            routes.append(row)
        try:
            async def run():
                await self.index.prepare(prepared.routes)
                return await self.index.match(text, routes, self.threshold, self.margin, prepared=True)
            result = await asyncio.wait_for(run(), 12)
            result['attempted'] = True
            prepared.trace['vector'] = result
            return result['best'] if result['accepted'] else None
        except Exception as exc:
            prepared.trace['vector'] = {'attempted': True, 'accepted': False,
                 'error': type(exc).__name__, 'provider': self.index.provider.identity,
                 'kind': self.index.provider.kind}
            prepared.notes.append('示例向量不可用/超时，转主力大模型；未把失败当成匹配')
            return None
        finally:
            prepared.trace['vector']['latency_ms']=round((time.perf_counter()-started)*1000,1)

    async def warm(self):
        try:
            data, _ = await self.catalog.snapshot(strict=True)
            async def run():
                warm = getattr(self.index.provider, 'warm', None)
                if warm:
                    await warm()
                await self.index.prepare(data['routes'])
            await asyncio.wait_for(run(), 30)
            self.warm_error = ''
        except Exception as exc:
            self.warm_error = type(exc).__name__

    def schedule_warm(self):
        # Serialize background precomputation; queued catalog changes are read fresh.
        if self.warm_task and not self.warm_task.done():
            self.warm_task.cancel()
        self.warm_task = asyncio.create_task(self.warm())

    def status(self, rows):
        entries = [{'route_id': r['id'], 'hash': self.index.key(t),
                    'ready': self.index.key(t) in self.index.cached}
                   for r in rows if r['enabled'] for t in r['examples']]
        return {'plugin': self.name, 'provider': self.index.provider.identity,
                'kind': self.index.provider.kind, 'threshold': self.threshold,
                'margin': self.margin, 'examples': entries, 'error': self.warm_error,
                'ready': sum(e['ready'] for e in entries), 'total': len(entries)}

    @staticmethod
    def allowed(prepared):
        return {r['handler'] for r in prepared.routes if r['enabled']} | {'OUT_OF_SCOPE'}

    @staticmethod
    def prompt_data(prepared):
        return [{'id': r['id'], 'name': r['name'], 'handler': r['handler'],
                 'description': r['description'], 'examples': r['examples'][:3]}
                for r in prepared.routes if r['enabled']]
