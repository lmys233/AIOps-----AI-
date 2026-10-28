"""One tool declaration visible to the model and used by the workflow."""
from ..tools.definition import define_tool
from ..tools.policy import CAP_ACTIVE_HTTP
from ..common.enums import ToolPermission
from ..tools.sources import RawOutcome
from ..tools.summarize import SUMMARIZERS
from .models import RequestPlan
from .targets import directory
from .runner import run

DESCRIPTION = '''对当前已登记系统的业务HTTP接口发出真实请求（主动流量，绝非被动查询）。
target_kind默认business，由模型选择业务观察；sdk模式只接受代码安排的SDK只读核验许可，模型不得自行使用sdk模式或伪造许可。
业务请求与SDK核验使用独立的6/min工具限额，仍共享任务总预算，SDK只允许单请求。
只能使用endpoint_inventory_read返回的service/instance/endpoint_id/endpoint_revision，不接受URL、任意headers或凭据，不跟随重定向。
probe是低频核验；load是接口所有者显式允许的有界负载，支持max_requests(<=200)、duration_sec(<=30)、concurrency(<=16)、rate_per_sec(<=50)、timeout_sec(<=10)，以owner更严限额为准。
path_params/query/body须满足登记schema，必需参数不能臆造。写接口必须展示完整参数后由用户确认本次方案，模型不能自行授权。
无自动重试；失败或中断保留台账，写请求超时可能已经生效。结果含每次request_id、状态、错误、耗时、实际QPS与窗口；响应体只留大小/hash。
few-shot：查系统状况且历史无错误、用户指向只读查询接口→probe一次，再读新SDK事件；问QPS在并发条件下怎样→owner允许时load，再读指标并说明合成流量；问知识定义/仅解释缺口→不用此工具；参数不全/多个写目标→反问而非猜测。'''


def summarize(data):
    if data.get('origin')=='sdk_verification':
        return {**data,'summary':'SDK具体链路核验：'+data['scope']['service']+' GET '+data['route']+'，HTTP 200且登记身份匹配；仅证伪该检查时刻当前接口仍不可达，不否定历史失败或证明业务正常'}
    return {**data,'summary':f"主动{data['mode']}：发送{data['sent']}，完成{data['completed']}，网络/服务端异常{data['errors']}；HTTP分布{data['http_status_counts']}，非2xx {data['non_success_responses']}；客户端完成率{data['achieved_qps']}次/秒；{data['limitations']}"}


def register(registry):
    SUMMARIZERS['summarize_web_probe']=summarize
    schema=RequestPlan.model_json_schema();schema['properties']['run_id']={'type':'string','pattern':'^probe_[a-f0-9]{32}$'}
    # Executor tool schema uses its supported subset; route/body validation uses JSON Schema.
    schema['properties']['body']={'type':['object','array','string','number','boolean','null'],'default':None}
    schema['required']=list(schema.get('required',[]))+['run_id']
    for k in ('mode','max_requests','duration_sec','concurrency','rate_per_sec','timeout_sec','path_params','query','body'):
        if k not in schema['required']:schema['required'].append(k)
    async def query(ctx,params):return RawOutcome(data=await run(params),raw_ref='web_query:'+params['run_id'])
    registry.register(define_tool(name='web_query',description=DESCRIPTION,parameters=schema,
        capabilities=(CAP_ACTIVE_HTTP,),granted_roles=('probe_agent',),permission=ToolPermission.ACTIVE_HTTP,
        idempotent=False,timeout_sec=45,rate_limit='6/min',risk_level='L1',summarizer='summarize_web_probe')(query))
    async def listing(ctx,params):
        rows=await directory()
        if params.get('service'):rows=[r for r in rows if r['service']==params['service']]
        return RawOutcome(data={'instances':rows},raw_ref='registered_endpoint_inventory')
    registry.register(define_tool(name='endpoint_inventory_read',description='只读当前环境/集群中已注册业务接口及owner探测政策；不发送业务请求',
        parameters={'type':'object','properties':{'service':{'type':'string'}},'additionalProperties':False},
        capabilities=(CAP_ACTIVE_HTTP,),granted_roles=('probe_agent',),timeout_sec=5)(listing))
    SUMMARIZERS['summarize_probe_observations']=lambda data:data
    async def observations(ctx,params):
        from .observations import read
        return RawOutcome(data=await read(params),raw_ref='probe_observations:'+params['run_id'])
    registry.register(define_tool(name='probe_observations_read',description='主动探测后回取同run的SDK新观测并验证身份/签名来源、留存PG；空结果是缓冲/覆盖未知，不证明正常',
        parameters={'type':'object','required':['service','instance','boot_id','run_id'],'additionalProperties':False,
            'properties':{'service':{'type':'string'},'instance':{'type':'string'},'boot_id':{'type':'string'},
                'poll':{'type':'integer','minimum':0,'maximum':1,'default':0},'run_id':{'type':'string','pattern':'^probe_[a-f0-9]{32}$'}}},
        capabilities=(CAP_ACTIVE_HTTP,),granted_roles=('probe_agent',),timeout_sec=4,
        summarizer='summarize_probe_observations')(observations))
