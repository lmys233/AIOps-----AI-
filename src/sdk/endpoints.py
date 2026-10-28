"""Low-intrusion route adapters. Inspect declared routes; never send traffic."""
from diagnostics.endpoints import endpoint_id, inventory, disabled_policy, MAX_ENDPOINTS

ADAPTERS = []


def register_adapter(adapter):
    """Application integration can add adapters in code, never from remote config."""
    ADAPTERS.append(adapter)


def clean_schema(value, definitions, stack=()):
    if not isinstance(value, dict): return {}
    if '$ref' in value:
        ref = value['$ref']
        if not ref.startswith('#/components/schemas/') or ref in stack:
            raise ValueError('recursive or external route schema')
        return clean_schema(definitions[ref.rsplit('/', 1)[-1]], definitions, (*stack, ref))
    allowed = {'type', 'properties', 'required', 'items', 'enum', 'anyOf', 'oneOf', 'allOf',
               'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'minLength', 'maxLength',
               'minItems','maxItems','minProperties','maxProperties','pattern', 'additionalProperties', 'nullable'}
    out = {}
    for key, item in value.items():
        if key not in allowed: continue
        if key == 'properties': out[key] = {k: clean_schema(v, definitions, stack) for k, v in item.items()}
        elif isinstance(item, dict): out[key] = clean_schema(item, definitions, stack)
        elif isinstance(item, list) and key in ('anyOf', 'oneOf', 'allOf'):
            out[key] = [clean_schema(v, definitions, stack) for v in item]
        else: out[key] = item
    return out


def asgi_routes(app):
    if not hasattr(app, 'routes'): return None
    rows = []
    def walk(container, prefix=''):
        openapi = container.openapi() if callable(getattr(container, 'openapi', None)) else {}
        definitions = openapi.get('components', {}).get('schemas', {})
        for route in container.routes:
            path = prefix + getattr(route, 'path', '')
            if getattr(route, 'routes', None):
                walk(route.app, path.rstrip('/')); continue
            methods = getattr(route, 'methods', None)
            if not methods or not path or path.startswith('/_aiops/') or path in ('/health', '/ready', '/metrics', '/services'):
                continue
            for method in sorted(methods):
                operation = openapi.get('paths', {}).get(getattr(route,'path',''), {}).get(method.lower(), {})
                parameters = [dict(name=p['name'], **{'in': p['in']}, required=bool(p.get('required')),
                                   schema=clean_schema(p.get('schema', {}), definitions))
                              for p in operation.get('parameters', []) if p.get('in') in ('path', 'query')]
                declared={p['name'] for p in parameters if p['in']=='path'}
                for key,converter in getattr(route,'param_convertors',{}).items():
                    if key not in declared:
                        parameters.append(dict(name=key,**{'in':'path'},required=True,schema={'type':
                            'integer' if type(converter).__name__=='IntegerConvertor' else 'number' if type(converter).__name__=='FloatConvertor' else 'string'}))
                body = operation.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema')
                body_schema=clean_schema(body, definitions) if body else None
                if body_schema is not None:body_schema['x-aiops-required']=bool(operation.get('requestBody',{}).get('required'))
                rows.append(dict(id=endpoint_id(method, path), method=method, path=path,
                    description=str((operation.get('summary') or getattr(route, 'name', '') or '')+' '+operation.get('description',''))[:1000],
                    parameters=parameters, body_schema=body_schema,
                    policy=disabled_policy()))
    walk(app)
    return rows


def discover(app, config=None):
    options = getattr(config, 'active_probe', {}) or {}
    adapters = [*ADAPTERS, asgi_routes]
    try:
        rows = next((v for a in adapters if (v := a(app)) is not None), None)
        if rows is None: rows = options.get('endpoints', []); status = 'configured' if rows else 'unsupported'
        else: status = 'complete'
        policies = options.get('routes', {})
        for row in rows:
            row['policy'] = {**disabled_policy(), **policies.get(row['method'] + ' ' + row['path'], {})}
        # No silent partial inventory. App continues; discovery failure is explicit.
        if len(rows) > MAX_ENDPOINTS: raise ValueError('route inventory exceeds ' + str(MAX_ENDPOINTS))
        return inventory(rows), status
    except Exception as exc:
        return [], 'unavailable:' + type(exc).__name__
