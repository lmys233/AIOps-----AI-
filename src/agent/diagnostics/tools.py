"""Source-specific read capabilities; no SQL, command or URL parameters."""
from uuid import uuid4
from ..tools.definition import define_tool
from ..tools.policy import CAP_DATABASE,CAP_RUNTIME,CAP_LIFECYCLE,CAP_RAW_TELEMETRY
from ..tools.sources import RawOutcome
from ..tools.summarize import SUMMARIZERS
from .reader import DiagnosticReader,KINDS,summarize

SCHEMA={'type':'object','properties':{
    'service':{'type':'string','minLength':1,'maxLength':128},
    'env':{'type':'string','minLength':1,'maxLength':128},
    'cluster':{'type':'string','maxLength':128},
    'instance':{'type':'string','maxLength':128},
    'request_id':{'type':'string','minLength':1,'maxLength':128},
    'start':{'type':'string','maxLength':64},'end':{'type':'string','maxLength':64},
    'object_name':{'type':'string','maxLength':128},
    'limit':{'type':'integer','minimum':1,'maximum':200},
    'before':{'anyOf':[{'type':'integer','minimum':1},{'type':'string','maxLength':256}]}},
    'required':['service','env','start','end'],'additionalProperties':False}


def role_for(name):
    if name.startswith('db_'):return 'database_agent',CAP_DATABASE
    if name=='process_event_read':return 'lifecycle_agent',CAP_LIFECYCLE
    return 'runtime_agent',CAP_RUNTIME


def register(registry,reader):
    if reader.store is not None:
        registry.owned_resources=getattr(registry,'owned_resources',[])+[reader.store]
    SUMMARIZERS['summarize_diagnostics']=summarize
    for name in KINDS:
        role,capability=role_for(name)
        async def handle(ctx,params,tool=name):
            result=await reader.read(tool,params)
            return RawOutcome(data=result,raw_ref='diagnostic_archive:'+tool+':'+uuid4().hex)
        definition=define_tool(name=name,description='按目标和时间窗只读查询 '+name,
            parameters=SCHEMA,capabilities=(CAP_RAW_TELEMETRY,capability),granted_roles=(role,),
            summarizer='summarize_diagnostics',timeout_sec=8,rate_limit='60/min',
            availability=lambda p,tool=name:reader.availability(tool,p),verification_records=True)(handle)
        registry.register(definition,actor='system',reason='来源隔离的应用诊断工具')
