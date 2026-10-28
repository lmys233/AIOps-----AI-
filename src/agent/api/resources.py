"""Composition root owns infrastructure; injected services are borrowed by default."""


def path(root, value):
    for name in value.split('.'):
        root = getattr(root, name, None)
        if root is None: break
    return root


def service_resources(owner, service, *, owns_router=True):
    # These getters also cover lazy repositories opened after startup.
    for name in ('history.store', 'history.cache.client', 'workspace_store', 'alert_receipts','commands',
                 '_gate._backend._client', '_ledger._backend._client', '_results._backend._client',
                 '_pending._backend._client', 'sessions',
                 'orchestrator.memory.working.client', 'orchestrator.session.working.client',
                 'orchestrator.memory.episodic.db', 'orchestrator.memory.procedural.db',
                 'orchestrator.executor.context.prometheus', 'orchestrator.executor.context.loki',
                 'orchestrator.executor.context.tempo', 'orchestrator.executor.context.sql',
                 'orchestrator.executor.context.topology', 'orchestrator.board.client'):
        owner.own(name, lambda name=name: path(service, name), lazy=True)
    for store in getattr(getattr(getattr(service.orchestrator, 'executor', None), 'registry', None), 'owned_resources', []):
        owner.own('tool archive', store)
    if owns_router:
        owner.own('model gateway', lambda: path(service, 'router.gateway'), lazy=True)

    owner.own('request workers', service, method='close_workers', worker=True)
