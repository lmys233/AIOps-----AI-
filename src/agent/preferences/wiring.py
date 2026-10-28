"""Composition and lifetime of optional learning, never an unbounded web request."""
from .config import enabled,Config
from .store import PreferenceStore


def install(app,service,resources):
    history=getattr(service,'history',None)
    if not history or not getattr(getattr(history,'store',None),'dsn',None) or not enabled('AIOPS_FEEDBACK_ENABLED',True):return
    store=PreferenceStore(history.store.dsn);service.preferences=store
    resources.own('preference store',store)
    from .routes import install as routes
    routes(app,service,store)
    from .tools import register
    register(service)
    if not enabled('AIOPS_PREFERENCE_LEARNING_ENABLED'):return
    config=Config.env()
    if min(config.daily_calls,config.daily_tokens,config.daily_wall_sec)<=0:return
    import os
    from ..api.wiring import build_gateway
    from ..llm.call_policy import Policy
    gateway=build_gateway(base_url=os.getenv('AIOPS_LLM_BASE_URL',''),model=os.getenv('AIOPS_PREFERENCE_MODEL') or os.getenv('AIOPS_LLM_MODEL',''),api_key=os.getenv('AIOPS_LLM_API_KEY'))
    if gateway:
        gateway.context_tokens=min(config.context_tokens,int(os.getenv('AIOPS_PREFERENCE_MODEL_CONTEXT_TOKENS',str(gateway.context_tokens))))
        gateway.retry_policy=Policy(timeout=45,max_attempts=3,backoff=.2,max_backoff=2)
    from .worker import PreferenceAgent
    executor=getattr(service.orchestrator,'executor',None)
    worker=PreferenceAgent(store,gateway,config,executor.registry.snapshot_hash() if executor else '',
                           tool_names=executor.registry.names() if executor else ())
    service.preference_agent=worker
    resources.own('preference worker',worker,worker=True)
    resources.start(worker.start)
