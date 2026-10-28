"""Generate -> rules/review -> one repair -> rules/review, or truthful quotations."""
import asyncio,time
from uuid import uuid4
from ..domain.presentation import digest
from .quality_rules import inventory,rule_check,validate_review
from . import quality_review as model


def event(emit,phase,name,params,**result):
    if emit:
        emit('tool',{'phase':phase,'name':name,'title':{'qa_generate':'生成知识答案','qa_rules':'核对答案引用与精确值',
            'qa_review':'评审忠实度、资料一致性与完整性','qa_repair':'修订知识答案'}.get(name,name),
            'agent_role':'qa_agent','params':params,**result})


async def operation(emit,name,params,run):
    cid='quality_'+uuid4().hex;started=time.perf_counter()
    from ..llm.accounting import REQUEST_RECORDS
    records=REQUEST_RECORDS.get();before=len(records) if records is not None else 0
    def attempts():
        rows=[r for r in (records[before:] if records is not None else []) if r.role in ('qa','qa_review','qa_repair')]
        return {'physical_attempts':sum(r.attempts for r in rows),'attempt_records':[
            a for r in rows for a in r.extra.get('attempt_records',[])]} if rows else {}
    event(emit,'start',name,params,call_id=cid)
    try:
        value=await run()
        data=value.model_dump() if hasattr(value,'model_dump') else value
        event(emit,'done',name,params,call_id=cid,ok=True,result=data,elapsed_ms=(time.perf_counter()-started)*1000,**attempts())
        return value
    except asyncio.CancelledError:
        event(emit,'done',name,params,call_id=cid,ok=False,result='操作取消，未取得有效校验结果',error_code='QUALITY_CANCELLED')
        raise
    except Exception as exc:
        event(emit,'done',name,params,call_id=cid,ok=False,result='未完成：'+type(exc).__name__,
              error_code='QUALITY_UNAVAILABLE',failure_kind=getattr(exc,'failure_kind',
                  'stage_deadline' if isinstance(exc,TimeoutError) else 'stage_unavailable'),elapsed_ms=(time.perf_counter()-started)*1000,**attempts())
        raise


async def assess(gateway,budget,query,sources,answer,emit,attempt,usage=None,audit=None):
    inv=inventory(answer,query)
    async def rules():return {'issues':rule_check(inv,sources),'units':len(inv['units'])}
    checked=await operation(emit,'qa_rules',{'attempt':attempt,'answer_hash':inv['answer_hash']},rules)
    record={'attempt':attempt,'inventory':inv,'rule_issues':checked['issues'],'review':None,'issues':checked['issues']}
    if checked['issues']:
        record.update(status='failed');return record
    try:
        async def reviewed():
            result=await model.review(gateway,budget,inv,sources,usage,audit)
            issues=validate_review(result,inv,sources)
            return {'review':result.model_dump(),'issues':issues,'status':'failed' if issues else 'passed'}
        result=await operation(emit,'qa_review',{'attempt':attempt,'answer_hash':inv['answer_hash'],
            'units':inv['units'],'questions':inv['questions']},reviewed)
        record.update(result)
    except asyncio.CancelledError:raise
    except Exception as exc:
        record.update(status='incomplete',failure_kind=getattr(exc,'failure_kind','') or ('stage_deadline' if isinstance(exc,TimeoutError) else 'stage_unavailable'),
            issues=[{'code':'REVIEW_UNAVAILABLE','unit_id':'','detail':type(exc).__name__}])
    return record


def final_state(quality,record):
    quality['status']=record['status']
    quality['issues']=record['issues']
    if record['status']=='passed':
        quality['faithfulness']='supported'
        quality['accuracy']='consistent_with_sources'
        limited=any(c['status']=='insufficient_disclosed' for c in record['review']['coverage'])
        quality['completeness']='limitations_disclosed' if limited else 'covered'
    else:
        quality.update(faithfulness='not_approved',accuracy='not_approved',completeness='not_approved')


async def verified(gateway,budget,query,sources,emit=None,usage=None,context_truncated=False):
    quality={'version':1,'status':'incomplete','attempts':[],'repairs':0,'issues':[],
        'query_hash':digest(query),'sources_hash':digest(sources),
        'accuracy_scope':'仅核对给定引用资料，未进行外部事实验证',
        'faithfulness':'not_reviewed','accuracy':'not_reviewed','completeness':'not_reviewed',
        'sources_truncated':bool(context_truncated) or any(s.get('truncated') for s in sources),'model_calls':[]}
    answer=None;stage='qa_generate'
    try:
        answer=await operation(emit,stage,{'question':query,'sources_hash':quality['sources_hash']},
            lambda:model.generate(gateway,budget,query,sources,usage=usage,audit=quality['model_calls']))
        for attempt in (1,2):
            checked=await assess(gateway,budget,query,sources,answer,emit,attempt,usage,quality['model_calls'])
            quality['attempts'].append(checked);final_state(quality,checked)
            if checked['status']=='passed':
                quality['status']='repaired' if attempt==2 else 'passed'
                return answer,quality
            if checked['status']=='incomplete' or attempt==2:break
            stage='qa_repair';quality['repairs']=1
            answer=await operation(emit,stage,{'issues':checked['issues'],'answer_hash':checked['inventory']['answer_hash']},
                lambda:model.generate(gateway,budget,query,sources,answer,checked['issues'],usage,quality['model_calls']))
    except asyncio.CancelledError:raise
    except Exception as exc:
        quality['status']='incomplete';quality['issues'].append({'code':'QUALITY_UNAVAILABLE','unit_id':'','detail':stage+': '+type(exc).__name__})
    quality['fallback']='original_quotes';return None,quality


def rows(quality):
    passed=quality['status'] in ('passed','repaired')
    labels={'passed':'已通过','repaired':'修订一次后通过','failed':'未通过','incomplete':'未完成'}
    result=[{'label':'答案质量校验','value':labels.get(quality['status'],'未完成')+
        '；忠实度：'+('引用支持' if passed else '未获核准')+'；准确性：'+('与引用资料一致' if passed else '未获核准')+
        '；完整性：'+('已说明资料不足' if quality['completeness']=='limitations_disclosed' else '已覆盖提问' if passed else '未获核准')+
        '；'+quality['accuracy_scope']+('；摘录有截断，仅评价可见资料' if quality['sources_truncated'] else '')}]
    issues=quality.get('issues') or []
    for issue in issues[:8]:
        result.append({'label':'校验问题 '+str(issue.get('unit_id') or issue['code']),
                       'value':str(issue['detail'])[:500]})
    return result
