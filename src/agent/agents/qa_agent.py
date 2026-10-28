"""A bounded knowledge Agent with no telemetry, management or write capabilities."""
import json
from uuid import uuid4
from ..common.enums import Intent
from ..common.steps import emit_step
from ..common.tool_budget import tool_budget_scope
from ..orchestration.state import TerminationDecision
from ..knowledge.query import rewrite
from ..knowledge.context import context
from ..knowledge.answer import write,references


class QaAgent:
    role='qa_agent'

    async def call(self,owner,state,tool,params,title):
        cid='qa_'+uuid4().hex
        payload={'call_id':cid,'name':tool,'title':title,'params':params,'agent_role':self.role}
        owner._emit(state,'tool',{**payload,'phase':'start'})
        with tool_budget_scope(state.budget):result=await owner.executor.call(self.role,tool,params)
        encoded=json.dumps(result.data,ensure_ascii=False,default=str) if result.ok else result.summary
        clipped=encoded[:24_000]+(f'（展示截断，完整工具结果原始引用：{result.raw_ref}）' if len(encoded)>24_000 else '')
        owner._emit(state,'tool',{**payload,'phase':'done','ok':result.ok,'result':clipped,
            'error_code':result.error_code,'elapsed_ms':result.elapsed_ms,'raw_ref':result.raw_ref})
        if not result.ok:
            state.degraded_detail.append(tool+'：'+result.summary)
            return None
        data=result.data
        return data['records'][0] if isinstance(data,dict) and 'records' in data else data

    async def run(self,owner,state):
        self.emit=lambda e,d:owner._emit(state,e,d)
        self.emit('agent_start',{'role':self.role,'mode':'knowledge'})
        state.flags['qa_result']={'outcome':'no_sources'}
        turns=[]
        try:
            memory=owner._session()
            if memory:turns=await memory.history(state.session_id)
        except Exception as exc:state.add_note('知识query近期上下文读取失败：'+type(exc).__name__)
        query=rewrite(state.flags.get('original_user_message') or state.user_message,turns)
        state.flags['qa_result']['query']=query
        emit_step(lambda e,d:owner._emit(state,e,d),'qa_query','准备知识检索问题',
                  {'original_query':query['original_query'],'rewrite_source':query['rewrite_source'],'slots':query['slots']},
                  result=query.get('reason') or query['rewritten_query'])
        if query['ambiguous']:
            return self.close(state,'近期讨论包含多个对象，请明确这次问题的指代：'+'、'.join(query.get('choices') or []),'needs_reference')
        if owner.executor is None:return self.close(state,'知识工具执行器未接入，本轮没有完成知识检索。')
        found=await self.call(owner,state,'kb_search',{'query':query['original_query'],
            'rewritten_query':query['rewritten_query'],'filters':query['slots'],'top_k':10},'混合检索知识库')
        if found is None:return self.close(state,'知识检索未完成，不能据此判断知识库没有相关内容。')
        hits=found['hits'];state.flags['qa_result'].update(retrieval=found)
        state.degraded_detail.extend(found.get('degraded') or [])
        if not hits and found.get('degraded'):return self.close(state,'已完成的检索路径未命中，但部分知识检索没有完成；不能据此判断知识库没有相关内容。')
        if not hits:return self.close(state,'当前范围没有检索到匹配的生效知识；可新增或审核相关知识后再问。','no_match')
        keys=list({(h['document_id'],h['version'],h['parent_id']):None for h in hits})
        refs=[dict(document_id=k[0],version=k[1],parent_id=k[2]) for k in keys]
        recalled=await self.call(owner,state,'kb_document_read',{'references':refs},'去重召回知识原文与父块')
        if recalled is None:return self.close(state,'命中了知识片段，但原文召回未完成，本轮不能生成有依据的答案。')
        prepared=context(recalled,hits,query['rewritten_query']);sources=prepared['sources']
        if not sources:return self.close(state,'命中知识的版本或状态已经变更，请重试检索；未使用失效原文。')
        answer=await write(getattr(owner.router,'gateway',None),state.budget,query['rewritten_query'],sources,
                           self.emit,state.flags.get('usage'),prepared['truncated'])
        answer['details']+=references(sources)
        answer['knowledge']={'query':query,'sources':sources,'context_characters':prepared['characters'],
                             'context_truncated':prepared['truncated'],'ranking':found['ranking'],'quality':answer.pop('quality')}
        state.flags['qa_result'].update(answer=answer,outcome='answered')
        answer['text']=answer['summary']
        state.status='answered';state.termination=TerminationDecision(terminate=True,kind='knowledge_answer',status='answered',reason='已召回当前生效知识及原文，提供知识参考；未进行现场归因')
        owner._emit(state,'agent_done',{'role':self.role,'sources':len(sources),'evidence':0})
        return 'report'

    def close(self,state,summary,outcome='failed'):
        state.status='abstained'
        state.flags['qa_result'].update(outcome=outcome)
        state.flags['qa_result']['answer']={'source':'knowledge_'+outcome,'summary':summary,'text':summary,
            'details':[],'details_kind':'table','note':'知识问答与当前故障证据独立',
            'knowledge':{'query':state.flags['qa_result'].get('query'),'sources':[]}}
        state.termination=TerminationDecision(terminate=True,kind='knowledge_no_answer',status='abstained',reason=summary)
        self.emit('agent_done',{'role':self.role,'sources':0,'outcome':outcome,'evidence':0})
        return 'report'
