from ..tools.sources import RawOutcome,SourceUnavailable
from ..observed.context import current
from .retrieval import search,recall
from ..tools.errors import ToolRefused
from ..common.enums import ErrorCode
from uuid import uuid4


def service(ctx):
    knowledge=getattr(ctx,'knowledge',None)
    if not knowledge:raise SourceUnavailable('NotConfigured','知识数据库未配置，未完成检索')
    scope=current()
    if scope and scope.tenant!=knowledge.store.tenant:raise ToolRefused(ErrorCode.TOOL_FORBIDDEN.value,'KnowledgeScope','knowledge tenant mismatch')
    return knowledge,scope.observed_id if scope else None


async def kb_search(ctx,params):
    knowledge,oid=service(ctx)
    try:result=await search(knowledge,params,oid)
    except ConnectionError as exc:raise SourceUnavailable('KnowledgeRetrievalFailed','知识检索未完成，请核查数据库与embedding来源') from exc
    return RawOutcome(data=result,raw_ref='knowledge:hybrid:'+uuid4().hex,note='；'.join(result['degraded']))


async def kb_document_read(ctx,params):
    knowledge,oid=service(ctx)
    result=await recall(knowledge.store,params['references'],oid)
    return RawOutcome(data=result,raw_ref='knowledge:parents:'+uuid4().hex,
                      note='原文当前版本与生效状态再次核实；知识内容不是现场故障证据')
