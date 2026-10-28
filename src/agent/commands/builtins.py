"""Two installed plugins, no implicit access to any other tool or data source."""
from contextvars import ContextVar
from contextlib import contextmanager
from ..tools.definition import define_tool
from ..tools.policy import CAP_ALERTS
from ..tools.sources import RawOutcome
from ..tools.errors import ToolRefused,QueryRejected
from ..observed.context import current
from ..alerts.query import combine,predicate
from .registry import CommandDefinition

BOUND=ContextVar('command_input',default=None)
SCHEMA={'type':'object','properties':{'where':{'type':'object','description':
    '受限条件：{}表示全部；叶子{field,op,value}；组合{all:[条件]}或{any:[条件]}。字段warning_id/name/content/location/service/status/severity/source/occurred_at；eq/contains/in，时间gte/lte/gt/lt且ISO8601带时区。'},
    'limit':{'type':'integer','minimum':1,'maximum':50},
    'input_ref':{'type':'string','description':'执行器绑定的上游集合/步骤/作用域摘要；模型不可设置'}},'required':['where'],'additionalProperties':False}


@contextmanager
def bind(engine,upstream,snapshot):
    token=BOUND.set((engine,upstream,snapshot))
    try:yield
    finally:BOUND.reset(token)


async def invoke(name,params):
    scope=current();bound=BOUND.get()
    if scope is None or bound is None:raise ToolRefused('TOOL_FORBIDDEN','MissingScope','命令缺少服务端作用域')
    engine,upstream,snapshot=bound
    if scope.tenant!=engine.tenant or not await engine.enabled(name):raise ToolRefused('TOOL_FORBIDDEN','CommandDisabled','命令已销毁或租户不一致')
    try:where=predicate(params['where'])
    except (ValueError,TypeError) as exc:raise QueryRejected(str(exc)) from None
    if name=='select' and upstream is None:raise QueryRejected('select需要管道上一步结果；可先用 @/warn')
    if upstream:
        # Scope and complete predicate come from the executor, never model arguments.
        where=combine(upstream['where'],where);snapshot=upstream['snapshot']
    result=await engine.warnings.page(scope,where=where,snapshot=snapshot,limit=params.get('limit',20))
    return RawOutcome(data=result,raw_ref='warnings:'+str(result['snapshot']),note='告警发送方陈述；不是现场诊断证据')


@define_tool(name='command_warn',description='检索当前注册系统/环境/集群的告警：ID用warning_id eq精确查询；名称用name contains模糊查询；空条件取近期告警。结果包含完整集合选择快照、总数和预览，供管道继续筛选。不能换系统，不能发业务请求。',
    parameters=SCHEMA,capabilities=(CAP_ALERTS,),granted_roles=('command_agent',),timeout_sec=5,rate_limit='120/min')
async def warn(ctx,params):return await invoke('warn',params)


@define_tool(name='command_select',description='按自然语言条件筛选管道上一步的完整告警集合，支持内容、服务、位置、严重度、来源、状态、带时区发生时间；all/any表达组合。只能收窄上游集合，不能重置作用域或快照；不是执行SQL/Python。',
    parameters=SCHEMA,capabilities=(CAP_ALERTS,),granted_roles=('command_agent',),timeout_sec=5,rate_limit='120/min')
async def select(ctx,params):return await invoke('select',params)


def validate_arguments(arguments):
    predicate(arguments['where'])


def definitions():return [CommandDefinition('warn','按告警名称或ID查找',warn,validate_arguments),CommandDefinition('select','按时间、位置和内容筛选上步结果',select,validate_arguments)]
