"""Bounded TraceQL condition syntax shared by compiler and scoped archive."""
import json,re

SERVICE=re.compile(r'resource\.service\.name\s*(=~|=)\s*("(?:\\.|[^"\\])*")')


def selected(query,names):
    out=set(names)
    for match in SERVICE.finditer(query):
        op,value=match[1],json.loads(match[2])
        if op=='=':
            if value not in names:raise PermissionError('unregistered trace service')
            out&={value}
        else:
            try:out&={n for n in names if re.fullmatch(value,n)}
            except re.error:raise ValueError('invalid trace service pattern') from None
    if not out:raise PermissionError('trace matches no registered service')
    return out


def validate_query(query,names):
    if '||' in query:raise ValueError('scoped TraceQL disjunction is not supported; split into bounded queries')
    if len(query)>4000 or not re.fullmatch(r'\s*\{[^{}]*\}\s*',query):raise ValueError('unsupported scoped TraceQL shape')
    if re.search(r'\b(select|by|with|let)\b',query):raise ValueError('unsupported trace pipeline')
    selected(query,names)
    rest=SERVICE.sub('',query.strip()[1:-1])
    rest=re.sub(r'(span\.)?duration\s*[><=]+\s*[\d.]+\s*(ms|s|m)','',rest)
    rest=re.sub(r'(^|\s)(span\.)?status\s*=\s*(error|ok)','',rest)
    rest=re.sub(r'\bkind\s*=\s*(server|client|internal)','',rest)
    if rest.replace('&&','').strip():raise ValueError('unsupported scoped trace condition')


def compile_query(query,names):
    validate_query(query,names);picked=selected(query,names)
    value=next(iter(picked)) if len(picked)==1 else '|'.join(re.escape(n) for n in sorted(picked))
    selector='resource.service.name'+('=' if len(picked)==1 else '=~')+json.dumps(value)
    if SERVICE.search(query):return SERVICE.sub(lambda m:selector,query)
    rest=query.strip()[1:-1].strip()
    return '{ '+(rest+' && ' if rest else '')+selector+' }'
