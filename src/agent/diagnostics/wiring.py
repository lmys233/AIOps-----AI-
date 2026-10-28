import os
from .store import DiagnosticStore


def enabled():return os.getenv('AIOPS_APPLICATION_DIAGNOSTICS','off').lower() in ('1','on','true')


def build_store():
    if not enabled():return None
    dsn=os.getenv('AIOPS_DIAGNOSTIC_DATABASE_URL') or os.getenv('AIOPS_DATABASE_URL')
    return DiagnosticStore(dsn) if dsn else None


def install_routes(app,store=None):
    from .ingestion import install
    install(app,store=store if store is not None else build_store())


def source_auth(app,request):
    policy=getattr(app.state,'diagnostic_policy',None)
    if policy is None:raise PermissionError('diagnostic source not configured')
    policy.authenticate(request.headers.get('authorization',''))


def register_tools(registry):
    from .reader import DiagnosticReader
    from .tools import register
    from .ingestion import from_env
    try:bindings=from_env().bindings
    except (OSError,ValueError,KeyError):bindings=[]
    register(registry,DiagnosticReader(build_store(),os.getenv('AIOPS_TENANT','default'),bindings))
