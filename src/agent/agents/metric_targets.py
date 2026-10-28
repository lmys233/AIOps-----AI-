"""Preserve explicitly written metric identifiers before choosing an algorithm."""
import re


def select(question,series,services=()):
    # An exact underscored identifier is not permission to substitute a nearby
    # metric. Natural-language aliases without such identifiers remain semantic.
    identifiers=set(re.findall(r'(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+',question))
    identifiers-=set(services)
    if not identifiers:return list(series),[]
    available={s.get('metric') for s in series}
    return [s for s in series if s.get('metric') in identifiers],sorted(identifiers-available)
