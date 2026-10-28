"""Explain existing proof obligations, without fetching or inventing evidence."""
from ..tools.availability import check_availability
from ..common.enums import ToolPermission

FIELDS={
 'client_disconnect_cancelled_request':'断开协议事件时间、取消发起方、取消实际接受结果及与终端任务的关联',
 'configured_deadline_aborted_request':'已配置期限标识/到达时间、expired、终端异常与期限事件的关联',
 'framework_shutdown_cancelled_request':'原框架停机分支记录、实际取消时间/结果和取消计数',
 'runtime_target_verified':'实际连接的库/模式/实例及配置来源；不要提供密码或连接凭据',
 'migration_not_applied':'迁移标识、执行结果与对象版本、记录时间',
 'ddl_removed_before_fault':'对象全名、DDL 审计操作/结果/时间及可信来源',
 'transaction_wait_cycle':'数据库会话、事务、锁对象和等待环记录',
 'conflicting_lock_order':'同事务的实际加锁顺序、锁对象、会话及时间'}

def requirements(p,decision):
    out=[]
    for c in p['candidates']:
        if decision.candidate_id and c['candidate_id']!=decision.candidate_id:continue
        for r in c['requirements']:
            if decision.requirement_key and r['key']!=decision.requirement_key:continue
            out.append((c,r))
    return out

def gaps(service,p,decision):
    grouped={};registry=getattr(getattr(service.orchestrator,'executor',None),'registry',None)
    for c,r in requirements(p,decision):
        if c['status']!='candidate':continue
        if r['state'] in ('satisfied','refuted'):continue
        tool=registry.get(r['tool']) if registry and r['tool'] else None
        category='需要人工提供原记录或接入适配器';ready=None
        readable=tool and tool.spec.permission==ToolPermission.READ_ONLY
        if readable:
            ready=check_availability(tool.availability,dict(r['params']))
            category='可自动定向核查' if ready.ready else '来源未接入/不可用：'+ready.reason
        if r['state']=='insufficient_coverage':category='已查但缺关联或历史触发见证'
        if r['state']=='query_failed':category='查询失败，需要恢复来源'
        if '预算' in r.get('reason',''):category='预算阻止继续取证'
        key=(r['signal'],r['tool'],r['predicate'],repr(r['expected']),r['state'],repr(sorted(r['params'].items())));entry=grouped.setdefault(key,{
            'signal':r['signal'],'tool':r['tool'],'question':r['question'],'category':category,
            'reason':r.get('reason',''),'candidates':[],'candidate_statements':[],'required_fields':fields(p,r['signal']),
            'how':('通过已注册只读工具 '+r['tool'] if readable else '由实际运行框架/数据库/部署系统提供原记录；目前没有自动读取适配'),
            'remedy':ready.remedy if ready and not ready.ready else '',
            'priority':0})
        entry['candidates'].append(c['candidate_id']);entry['candidate_statements'].append(c['statement']);entry['priority']+=int(r['required'])
    return sorted(grouped.values(),key=lambda g:(-g['priority'],g['signal']))

def fields(p,signal):
    values=['服务/环境、实例/boot、发生时间与时区']
    values.append('目标='+p['service']+'/'+p.get('env','')+'；实例/boot='+
        (p.get('instance') or '未留存')+'/'+(p.get('boot_id') or '未留存')+'；首次样本时间='+str(p.get('first_seen','未留存')))
    if p.get('request_id'):values.append('同请求 request_id='+p['request_id'])
    if p.get('object_name'):values.append('对象全名='+p['object_name'])
    values.append(FIELDS.get(signal,'原始来源记录、相关操作/查询结果及与当前问题的关联；不是只提供错误名称'))
    return '；'.join(values)

def explain(p,items,action):
    if action=='explain_result':
        head='已观测事实：'+p['fact']['statement']+'。形成原因：'+(p['cause']['statement'] or '尚未确认')+'。单次成功不能否定历史故障，也不能证明全量恢复。'
        rows=[{'label':'依据','value':'、'.join(p['evidence_ids'])},
            {'label':'确认边界','value':'原因是否确认以上述状态及引用为准；支持需同请求、实例和时间的可验证记录。'},
            {'label':'恢复判定','value':'需故障之后的同接口/实例覆盖、持续成功与错误率、流量和采集完整性；单次成功只证明该样本，不能把缺少新错误当作恢复。'}]
        return head,rows
    if action=='compare_candidates':
        rows=[{'label':c['statement'],'value':{'candidate':'待验证','confirmed':'已确认','excluded':'有可靠反证'}[c['status']]+
            '；'+c.get('reason','')+'；'+ '；'.join(r['question']+'：'+r.get('reason','') for r in c['requirements'] if r['state']!='satisfied')} for c in p['candidates']]
        return '以下比较已有候选；没有新证据，不改变原判断。未观测到某事件不能单独排除它。',rows
    if not items:
        if not p['candidates']:
            return '当前异常还没有可验证的原因候选，原因仍未知。先补充同事件原始错误和阶段记录，帮助提出可验证假设。',[
                {'label':'先补原始事件','value':fields(p,'unclassified_failure_context')},
                {'label':'覆盖边界','value':'没有候选不等于没有问题；当前未完成原因枚举，其他可能未排除。'}]
        return '已有候选的已证实条件与反证见下方；本轮无需重补它们，清单之外的可能仍未排除。',[]
    rows=[{'label':('优先补充 '+str(n+1) if n<3 else '其他缺口')+' · '+g['question'],
           'value':g['category']+'；用途：核实'+ '、'.join(g['candidate_statements'])+'；需提供：'+g['required_fields']+'；获取方式：'+g['how']+
               ('；接入：'+g['remedy'] if g['remedy'] else '')} for n,g in enumerate(items)]
    head='当前协作对象：'+p['service']+' '+p['error_type']+' '+p['problem_id']+'。'
    head+='这个编号是待解释的异常诊断记录；选定目标不会选中任何原因，也不代表形成原因已确认。'
    head+=('本次仅展开你指定候选的数据需求。' if p.get('_focused_candidate') else
           '未指定候选时，下面逐项覆盖所有已留存及适用候选；当前清单保持开放，未列出不等于排除。')
    head+='需要补充可关联到这次问题的原记录，优先看下面前 '+str(min(3,len(items)))+' 项。'
    if p['cause']['status']=='confirmed':
        head='该问题已确认形成原因：'+p['cause']['statement']+'。下面是其他未验证候选的剩余条件，不代表推翻已证实结论，也不证明全量恢复。'
    elif p['error_type']=='HTTP_REQUEST_CANCELLED':head+=' 当前取消来源未明确；可先核实是否人为/内部任务中止，再检查断开、期限及停机见证。'
    rows.append({'label':'资料边界','value':'可以回复“补充日志：”或“补充数据：”并粘贴原记录。无需发送凭据或重新提供全部指标。新开启采集不能补造过去的事件；人工说明会先记为未核验，不能直接确认原因。'})
    rows.append({'label':'确认与排除','value':'原记录需与该问题的请求、实例和时间对应，能够见证触发事件及其传递到异常的过程。排除候选需要同范围的可靠反证；缺少日志或用户选择问题编号只能保留未知。'})
    if p['error_type']=='HTTP_REQUEST_CANCELLED':
        rows.append({'label':'取消发起方资料','value':'如有框架、网关、客户端或运维脚本原记录，请提供取消发起方、触发时间、同请求关联和实际中止结果；可用于核查内部/人为取消、客户端断开、已配置期限或停机来源。资料先保持未核验，只有错误名称或业务账单取消说明不足以确认 HTTP 处理任务中断的来源。'})
    return head,rows
