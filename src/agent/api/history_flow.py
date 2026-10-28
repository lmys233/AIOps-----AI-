"""HTTP 同步/流式统一保存最终档案；工作记忆采用最新页回源。"""
from ..history.codec import payload
from ..history.working import ReadThroughWorking
from ..history.store import HistoryDeleted


class HistoryMixin:
    def _bind_history(self,history):
        self.history=history
        memory=getattr(self._orchestrator,'session',None)
        working=getattr(memory,'working',None)
        if history and working and not isinstance(working,ReadThroughWorking):
            memory.working=ReadThroughWorking(working,history)

    async def _save_history(self,request,body,details):
        if not self.history:return True
        try:
            if body.get('explanation',{}).get('task_control'):
                from ..adapters.task_runtime import controller,scope_for
                await controller(self).commit(self,request,scope_for(request),body,details)
            else:
                _,notes=await self.history.save(request.session_id,body['turn_id'],payload(request,dict(body),details))
                body.setdefault('degraded',[]).extend(notes)
            if details.get('overflow'):
                body['degraded'].append('历史步骤超过保存上限；该轮档案已明确标记为不完整')
        except HistoryDeleted:
            memory=getattr(self._orchestrator,'session',None)
            if memory:await memory.clear(request.session_id)
            body.setdefault('degraded',[]).append('该会话已删除，本轮未重新创建历史')
            return False
        except Exception as exc:
            from .constants import logger
            from ..tools.availability import safe_detail
            logger.error('历史写入失败 trace_id=%s type=%s reason=%s',body.get('trace_id',''),type(exc).__name__,safe_detail(str(exc)))
            if body.get('explanation',{}).get('task_control'):
                body['explanation']['task_control']['commit_state']='failed'
            body.setdefault('degraded',[]).append('历史档案写入失败：'+type(exc).__name__+
                '；本轮正文未确认持久化，短期记忆过期后可能无法恢复')
        return True
