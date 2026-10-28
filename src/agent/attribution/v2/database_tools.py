"""数据库检查扩展只授权给变更取证角色，归因/处置角色不能直接查库。"""
from ...common.enums import ToolPermission
from ...tools.definition import define_tool
from ...tools.policy import CAP_CHANGE
from ...tools.sources import RawOutcome
from .database import DatabaseReader
from .database_config import DATABASE_TOOLS
from ...tools.availability import Availability
from uuid import uuid4

SCHEMA={'type':'object','properties':{'service':{'type':'string','minLength':1},
    'object_name':{'type':'string','maxLength':127}},
    'required':['service','object_name'],'additionalProperties':False}


def register_database_tools(registry,reader=None):
    for name in DATABASE_TOOLS:
        def readiness(params,tool=name):
            source=reader or DatabaseReader.from_env()
            probe=getattr(source,'availability',None)
            return probe(tool,params) if probe else Availability()
        async def handle(ctx,params,tool=name):
            source=reader or DatabaseReader.from_env()
            rows=await source.read(tool,params)
            return RawOutcome(data=[r.model_dump(mode='json') for r in rows],
                raw_ref='database_check: '+tool+' '+params['service']+' '+params.get('object_name','')+' '+uuid4().hex)
        definition=define_tool(name=name,description='命名只读核查 '+name,
            parameters=SCHEMA,capabilities=(CAP_CHANGE,),granted_roles=('change_agent',),
            permission=ToolPermission.READ_ONLY,timeout_sec=10,availability=readiness,
            verification_records=True)(handle)
        registry.register(definition,actor='system',reason='V2 具体验证条件只读取证')
