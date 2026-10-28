"""实现告警接入的**执行侧**：同步入队 + 有界并发的后台排查。"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from ..common.enums import CHAT_MESSAGE_MAX
import asyncio
import uuid
import os
from dataclasses import replace
from .alerts import ALERT_MAX_BATCH, AlertDecision
from .constants import logger
from .errors import InvalidAlerts
from .requests import ChatContext, ChatOptions, ChatRequest, ChatUser
from ..sessions import ALERT_SESSION_PREFIX


class AlertIngestMixin:
    """同步入队 + 有界并发的后台排查（告警接入的执行侧）。"""

    async def submit_alerts(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """`POST /v1/alerts`：**同步入队、异步排查**（202 不阻塞 Alertmanager）。

        ★ 为什么异步：排查要跑几十秒，同步做会让告警系统自己超时。
        ★ 被抑制的告警**照样计账**（`suppressed=True`），不静默吞掉。
        """
        from ..alerts.contracts import normalize
        from ..alerts.receipts import store_of,receipt,ReceptionUnavailable
        source,capped,overflow=normalize(payload)
        # ★★ **先封顶**再入队。每个不同 fingerprint = 一次真实排查
        #   （数十秒 + 多次模型调用）。不限长度时，一个连通端口的人就能把账单打上去；
        #   而这是**设计上就该被 Alertmanager 从网络调用**的端点，
        #   所以"只在回环上跑"这个前提在这里不成立。
        from ..observed.alerts import scoped_alerts
        capped=await scoped_alerts(getattr(self,'observed_catalog',None),capped)
        tenant=os.getenv('AIOPS_TENANT','default');receipts=store_of(self)
        # Persist before claiming the dedup key: a database failure must not consume
        # admission and suppress the sender's retry without starting a diagnosis.
        tickets=[]
        for row in capped:
            item=receipt(AlertDecision(row['fingerprint'],'',False),row,'received')
            tickets.append(item)
        try:await receipts.put_many(tenant,tickets)
        except Exception:raise ReceptionUnavailable('告警接收记录未确认保存；未启动排查，请稍后重发') from None
        auto=payload.get('auto_diagnose',False)
        firing=[row for row in capped if row['status']=='firing' and auto]
        admitted=iter(await self.alerts.submit(firing))
        decisions=[];degraded=[];results=[]
        started: list[str] = []
        for raw,item in zip(capped,tickets):
            decision=(next(admitted) if raw['status']=='firing' and auto else
                AlertDecision(raw['fingerprint'],await receipts.prior_incident(tenant,item['observed_id'],raw['fingerprint']),
                    False,reason=('已记录发送方恢复通知；未重新排查，未据此确认服务恢复' if raw['status']=='resolved' else '已保存告警；引用后再排查，未调用模型')))
            decisions.append(decision)
            item.update(incident_id=decision.incident_id,state=('queued' if decision.accepted else
                'suppressed' if decision.suppressed else 'resolved_notice' if raw['status']=='resolved' else 'rejected' if auto else 'recorded'))
            try:await receipts.decide(tenant,item)
            except Exception:
                # Admission already succeeded. Start it even if the receipt update
                # fails; never turn a persistence problem into lost execution.
                degraded.append('告警状态更新失败；已接收记录仍在，实际诊断将继续')
            result=decision.to_dict();result.update(receipt_id=item['receipt_id'],state=item['state'],
                session_id=item['session_id'],observed_id=item['observed_id'])
            from ..alerts.query import warning_id
            result['warning_id']=warning_id(tenant,item['observed_id'],item['fingerprint'])
            if item['observed_id'] and decision.incident_id:
                from urllib.parse import quote
                result['conversation_url']='/webui/#system='+item['observed_id']+'&session='+quote(item['session_id'],safe='')
                result['incident_url']='/v1/observed-systems/'+item['observed_id']+'/incidents/'+decision.incident_id
            results.append(result)
            if not decision.accepted:
                continue
            task = asyncio.create_task(self._run_alert(decision, raw, item))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            started.append(decision.incident_id)
        return {
            'recorded':len(tickets),'auto_diagnose':auto,
            "accepted": sum(1 for item in decisions if item.accepted),
            "suppressed": sum(1 for item in decisions if item.suppressed),
            "results": results,
            "started": started,
            "source": source,
            "resolved":sum(row['status']=='resolved' for row in capped),
            "sender_truncated":payload.get('truncatedAlerts',0),
            "degraded":degraded+([] if getattr(receipts,'dsn',None) else ['告警接收记录仅在当前进程；未配置历史数据库']),
            "delivery_semantics":"202为已接收；后台队列在当前进程，尚未执行的任务不保证跨重启自动恢复",
            # ★ 被截掉的必须**明说**（不静默丢）—— 与"降级要响"同一条口径
            "truncated": overflow > 0,
            "dropped_over_limit": overflow,
            "max_batch": ALERT_MAX_BATCH,
            "note": (f"本批超过 {ALERT_MAX_BATCH} 条，已只处理前 {ALERT_MAX_BATCH} 条"
                     f"（其余 {overflow} 条**未入队**，请分批重发）" if overflow else "")+
                     (f"；发送方另截掉{payload['truncatedAlerts']}条，AIOps未收到这些告警" if payload.get('truncatedAlerts') else ''),
        }

    async def _run_alert(self, decision: AlertDecision,
                         raw: Mapping[str, Any], ticket=None) -> Optional[dict[str, Any]]:
        """把一条告警变成一次排查（**复用 `/v1/chat` 的同一条管线**）。"""
        labels = raw.get("labels") if isinstance(raw.get("labels"), Mapping) else {}
        from ..alerts.contracts import request_message
        from ..alerts.context import use as use_alert
        from ..alerts.receipts import store_of
        from ..observed.context import use as use_scope
        from ..observed.chat import resolve
        tenant=os.getenv('AIOPS_TENANT','default');receipts=store_of(self)
        metadata=dict(raw['record'],receipt_id=(ticket or {}).get('receipt_id',''),
            received_at=raw['received_at'],time_window=raw['time_window'],
            coverage_limited=raw['coverage_limited'],evidence_role='sender_claim_not_observation')
        request = ChatRequest(
            session_id=f"{ALERT_SESSION_PREFIX}{decision.fingerprint}",
            message=request_message(raw),
            user=ChatUser(id="alertmanager", name=str(raw.get("source") or "alert")),
            context=ChatContext(env=labels.get('env'),cluster=labels.get('cluster'),observed_id=labels.get('observed_id')),
            options=ChatOptions())
        trace_id=uuid.uuid4().hex;scope=None
        async def update(state,error=''):
            if not ticket:return
            try:await receipts.update(tenant,ticket['receipt_id'],state,error)
            except Exception:logger.exception('告警状态留存失败 receipt_id=%s',ticket['receipt_id'])
        try:
            # ★★ 后台排查走**有界信号量** —— `create_task` 本身不受任何约束，
            #   1000 条告警会同时起 1000 次排查（每次都打模型网关）。
            #   信号量把"同时在跑的告警排查"限死，超出的排队等（不丢、不并发烧钱）。
            async with self._alert_semaphore:
                await update('running')
                if request.context.observed_id:
                    scope=await resolve(self,request)
                    # Bound to the sender's service, while retaining the system vocabulary.
                    scope=replace(scope,services=(labels['service'],))
                with use_alert(metadata),use_scope(scope):
                    body=await self.handle(request, trace_id=trace_id,incident_id=decision.incident_id)
                await update('failed' if body.get('status')=='failed' else 'completed',
                    str((body.get('error') or {}).get('code') or ''))
                return body
        except asyncio.CancelledError:
            await update('interrupted','后台执行被中止；未确认完成，不自动重发主动请求')
            raise
        except Exception:  # noqa: BLE001 —— 后台任务绝不能把异常抛到无人接管的地方
            logger.exception("告警触发的排查失败（fingerprint=%s）", decision.fingerprint)
            await update('failed','ALERT_EXECUTION_FAILED；trace_id='+trace_id)
            # Keep failures queryable in the same incident/history paths as successful runs.
            from .contract_body import empty_contract_body
            from ..alerts.context import stamp as stamp_alert
            from ..observed.context import stamp as stamp_scope
            from ..presentation.projection import finish
            body=empty_contract_body(request.session_id,trace_id=trace_id,status='failed',
                incident_id=decision.incident_id,intent_name='INCIDENT_RCA',write_allowed=False)
            from .idempotency import ensure_turn_id
            body['turn_id']=ensure_turn_id(request)
            body['report']='告警已接收，但后台排查未完成；不能据此判断服务正常或异常。请查看告警接入状态及 trace_id。'
            body['error']={'code':'INTERNAL_ERROR','message':'告警后台排查失败，已记录 trace_id'}
            body['explanation']['answer']={'source':'alert_ingest','summary':body['report'],'details':[],'details_kind':'table'}
            try:
                with use_alert(metadata),use_scope(scope):
                    # A revoked target remains attributable but cannot acquire query permission.
                    if scope:stamp_scope(body)
                    elif request.context.observed_id:
                        body['explanation']['observed_system']={'tenant':tenant,'observed_id':request.context.observed_id,
                            'env':labels.get('env'),'cluster':labels.get('cluster'),'registered_services':[]}
                    stamp_alert(body);finish(body)
                    await self._save_history(request,body,{})
                    await self._store_incident(body);await self._note_turn(request,body)
            except Exception:logger.exception('告警失败档案留存失败 trace_id=%s',trace_id)
            return None
