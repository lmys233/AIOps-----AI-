"""恢复中断：澄清续跑 + 审批三条红线 + 确认门。"""

from __future__ import annotations
from ..observed.context import session_key,current


from ..common.enums import (APPROVAL_TTL_MINUTES, CHAT_MESSAGE_MAX, ChatStatus, ErrorCode,
                            Intent)
from typing import Any, Mapping, Optional
import uuid
from .approvals import PendingApproval
from .constants import logger
from .contract_body import empty_contract_body
from .errors import INVALID_PARAMS, ResumeError
from .requests import ChatContext, ChatOptions, ChatRequest, ChatUser
#: ★ 回填 `intent` 时**翻成白话**再拼进 message —— 规则层认的是人话（`排查`/`故障`/
#:   `趋势`…），不是契约里的枚举名。这张表**只有一份**（`tasks.py`），两处共用它。
from .tasks import INTENT_PHRASE as _INTENT_PHRASE
from .tasks import clear_parked, degraded_sink


class ResumeMixin:
    """恢复中断：澄清续跑 + 审批三条红线 + 确认门。"""

    async def resume(self, payload: Mapping[str, Any], *, trace_id: str) -> dict[str, Any]:
        """处理 `POST /v1/chat/resume`：澄清恢复 / 审批恢复。"""
        resume_type = str(payload.get("resume_type") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if resume_type not in ("clarification", "approval"):
            raise ResumeError(422, INVALID_PARAMS,
                              "resume_type 必须是 clarification 或 approval")
        if not session_id:
            raise ResumeError(422, INVALID_PARAMS, "session_id 不能为空")
        if resume_type == "clarification":
            return await self._resume_clarification(payload, session_id=session_id,
                                                    trace_id=trace_id)
        return await self._resume_approval(payload, session_id=session_id, trace_id=trace_id)

    async def maybe_plain_confirmation(self, request: Any, session: Any, *,
                                       trace_id: str) -> Optional[dict[str, Any]]:
        """把**普通对话框**里回「确认」的输入接到**既有的确认流程**上。

        ★ 为什么不能靠"重跑原话"：重跑会**再问一次确认**（死循环），
          而确认本来就有一条成熟链路（`_finish_confirmation` + 待确认动作存储，
          写操作受控与人工审批两道红线都在那里）。
        ★ fail-closed：**没有待确认动作**时返回 `None`（不假装用户确认过什么），
          交给正常流程 —— 那时"确认"两个字确实什么都不算。

        ★ 判据**只能**是"有待确认动作"：原先还要求"台账里记着 `confirm` 这个缺口"，
          但台账的 gaps 来自**澄清问题的 `slot` 字段**，而确认门的问题未必带 `confirm` 这个名字
          （L2 预案走的是 `risk`/`action` 那条路）—— 于是用户回「确认」被这条**多余的前置条件**
          挡掉，又落回越界拒答。多一个前置条件只会多一种"明明在等我我却装不认识"的失效。
        """
        if not self._is_confirmation({"confirm": request.message}):
            return None
        if await self._pending.get(session_key(request.session_id)) is None:
            return None
        body = await self._finish_confirmation(request.session_id, trace_id=trace_id,
                                               answers={"confirm": True})
        await clear_parked(session, request.session_id, degraded=degraded_sink(body))
        return body

    async def _resume_clarification(self, payload: Mapping[str, Any], *,
                                    session_id: str, trace_id: str) -> dict[str, Any]:
        """把澄清答案合并进槽位，然后**重跑**（同一条管线，不另写一套）。"""
        clarification = payload.get("clarification")
        answers = dict(clarification.get("answers") or {}) \
            if isinstance(clarification, Mapping) else {}
        # ★★ `plugin_id` 决定**答案怎么回填**。
        #   以前一律"把答案拼成一句话再重跑"，于是用户答的 `env=staging` 只能碰运气被
        #   正则抽出来 —— 现在按插件声明的槽位**直接回填**（env/cluster 走请求上下文，
        #   其余拼进 message 让正常的槽位抽取接手）。
        plugin_id = (str(clarification.get("plugin_id") or "").strip()
                     if isinstance(clarification, Mapping) else "")
        if not answers:
            raise ResumeError(422, INVALID_PARAMS,
                              "clarification.answers 不能为空")
        # ★ 如果这次"澄清"其实是在回答**破坏性动作的确认门**，就走审批/拒绝那条路
        if self._is_confirmation(answers) \
                and await self._pending.get(session_key(session_id)) is not None:
            return await self._finish_confirmation(session_id, trace_id=trace_id,
                                                   answers=answers)
        from ..adapters.task_runtime import enabled
        if enabled(self):
            from ..adapters.task_resume import clarification as task_clarification
            try:return await task_clarification(self,payload,session_id,trace_id,answers,plugin_id)
            except (ValueError,PermissionError) as exc:
                raise ResumeError(422,INVALID_PARAMS,str(exc)) from exc
        patch = self._apply_clarification(plugin_id, answers)
        # ★★ **必须带上用户原来那句话** ——
        #   以前 resume 只把回填结果拼成一句新消息（"…已按 plugin_id 回填 service=order…"），
        #   原问题就此丢失 → 重跑时意图层面对一个陌生的句子，很容易再落回兜底
        #   （实测：用户答了 `service=order` 之后仍然 `OUT_OF_SCOPE`，等于"答了也没用"）。
        #   原问题在**服务端**就有（会话记忆），不该让客户端重发。
        original = await self._last_user_message(session_id)
        # ★ 认不出 `plugin_id`（旧路径）时仍走 `_describe_answers` —— 那时**没有 patch**
        #   可渲染（曾经漏判 `patch is None`，实测把旧路径打成了 500）。
        message = (self._describe_answers(answers) if patch is None
                   else self._describe_patch(patch, answers, original=original))
        request = ChatRequest(
            session_id=session_id, message=message[:CHAT_MESSAGE_MAX],
            user=ChatUser(id=str(payload.get("user_id") or "human:unknown")),
            # ★ env/cluster 是**请求上下文事实**（路由的"上下文优先"会直接采信它），
            #   其余槽位靠 message 里的自然语言让抽取层接手（不假装能直接改槽位）。
            context=ChatContext(observed_id=current().observed_id if current() else payload.get('observed_id'),env=None if patch is None else patch.to_dict().get("env"),
                                cluster=None if patch is None
                                else patch.to_dict().get("cluster")),
            options=ChatOptions())
        body = await self.handle(request, trace_id=trace_id)
        body["resumed"] = {"resume_type": "clarification", "answers": answers,
                          "note": "澄清答案已作为新一轮输入的上下文（同一条管线）"}
        return body

    async def _last_user_message(self, session_id: str) -> str:
        """从**会话记忆**里取用户最近一句原话（取不到就空串 —— 不编）。"""
        session = getattr(self._orchestrator, "session", None)
        if session is None or not session_id:
            return ""
        try:
            turns = list(await session.history(session_id))
        except Exception:  # noqa: BLE001 —— 记忆不可用不该让 resume 失败
            return ""
        for turn in reversed(turns):
            text = str(getattr(turn, "user_message", "") or "").strip()
            if text:
                return text
        return ""

    def _apply_clarification(self, plugin_id: str, answers: Mapping[str, Any]) -> Any:
        """按 `plugin_id` 把答案变成槽位回填；认不出来 → `None`（走旧路径，**不猜**）。"""
        if not plugin_id:
            return None
        hub = getattr(self._router, "clarify", None)
        if hub is None:                       # 注入的假路由（测试常用）没有 hub
            return None
        value = answers.get(plugin_id)
        if value is None:
            declared = getattr(hub.registry.by_id(plugin_id), "applies_to", ())
            for gap in declared:
                if gap in answers:
                    value = answers[gap]
                    break
        if value is None:
            value = next(iter(answers.values()))
        return hub.apply(plugin_id, value)

    @staticmethod
    def _describe_patch(patch: Any, answers: Mapping[str, Any],
                        original: str = "") -> str:
        """把回填过的答案也**拼进 message**：留痕（审计上要能看到用户答了什么）。

        三件事一起做 ——
          ① **带上原问题**（`original`）：不然重跑时面对的是一个陌生的句子；
          ② 回填值进 message（抽取层接手，不假装能直接改槽位）；
          ③ `intent` 这种**枚举值要翻成白话**（`INCIDENT_RCA` → 「排查故障」）——
             规则层认的是人话，不是契约里的枚举名。
        """
        values = patch.to_dict()
        parts = [f"{name}={value}" for name, value in values.items()]
        answered = "、".join(str(value) for value in answers.values())
        phrase = _INTENT_PHRASE.get(str(values.get("intent") or ""), "")
        head = f"{original}\n" if str(original).strip() else ""
        return (f"{head}（澄清回答：{answered or '（空）'}；已按 plugin_id 回填 "
                f"{', '.join(parts)}）{'请' + phrase if phrase else '继续'}："
                + "、".join(str(value) for name, value in values.items() if name != "intent"))

    @staticmethod
    def _describe_answers(answers: Mapping[str, Any]) -> str:
        return "；".join(f"{key}={value}" for key, value in answers.items())

    @staticmethod
    def _is_confirmation(answers: Mapping[str, Any]) -> bool:
        """判断 `answers` 里有没有**显式的确认**：只认 `confirm` 这一个键。

        ★ 以前只要**任意**一个答案值是 `"yes"/"true"/"确认"` 就返回 True ——
          于是"这个服务的 SLO 目标是不是 99.9%？"这种无关澄清被回答"是"时，
          会**误触**破坏性动作的确认门（把一次澄清变成一次审批）。确认门必须显式：
          只有 `{"confirm": true}`（或该键的等价真值字符串）才算。
        ★ 也不接受 `{"confirm": "否"}` 这类"键在但值否定"的写法 —— 非真值即未确认。
        """
        if not answers:
            return False
        value = answers.get("confirm")
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value == 1
        return (isinstance(value, str)
                and value.strip().lower() in ("true", "yes", "y", "1", "确认", "是", "同意"))

    async def _finish_confirmation(self, session_id: str, *, trace_id: str,
                                   answers: Mapping[str, Any]) -> dict[str, Any]:
        """按风险等级处理用户确认的破坏性动作（四种结局都不执行任何变更）。

        | 等级 | 结局 |
        |---|---|
        | **L4** | `rejected`：禁止类动作，Agent 一律不得执行（记录审计） |
        | **L3** | 登记待批动作 → `clarify` + `approval_required`（等 `human:` 审批） |
        | **L2 及以下** | 需要执行端（尚未接入）→ `failed`：**没有应用任何变更** |
        | `options.allow_write=false` | `rejected`：只输出预案不执行 |

        ★ 四种结局**都不执行任何变更** —— 这不是"还没做"，而是当前阶段的**正确行为**：
          一个"看起来执行了"的假成功比明确的拒绝危险得多。
        ★ 响应体从 `empty_contract_body` 起手（**字段一个都不能少**），再把上一轮留存的
          incident 覆盖上来；incident 已被淘汰时，剩下的就是一份诚实的空值。
        """
        stored = await self._pending.pop(session_key(session_id))
        action, risk = stored["action"], stored["risk"]
        allow_write = bool(stored.get("allow_write"))
        body = empty_contract_body(
            session_id, trace_id=trace_id,
            incident_id=str(stored.get("incident_id") or ""),
            # ★ 兜底意图取 REMEDIATION：确认门处理的只可能是"处置预案"这一族
            intent_name=Intent.REMEDIATION.value, write_allowed=allow_write)
        base = await self.get_incident(stored.get("incident_id"))
        if base:
            body.update(base)
        body.update({"session_id": session_id, "trace_id": trace_id,
                     "proposed_action": action, "risk": risk,
                     "write_allowed": allow_write})
        # ★ 判据与文案一个字未改，**顺序也没变**
        #   （L4 → allow_write=false → 需审批 → 执行端未接入）。
        outcome = (self._confirm_l4(body, action)
                   or self._confirm_write_disabled(body, action, risk, allow_write)
                   or self._confirm_needs_approval(body, stored, session_id, action, risk)
                   or _confirm_executor_missing(body, action, risk))
        outcome()
        await self._store_incident(body)
        return body

    @staticmethod
    def _confirm_l4(body: dict[str, Any], action: Mapping[str, Any]) -> Any:
        """判定并记录 L4 **禁止类**动作：一律不得执行，只记录审计。"""
        if body["risk"]["level"] != "L4":
            return None

        def _apply() -> None:
            body.update({
                "status": ChatStatus.REJECTED.value,
                "reject_reason": (f"{action['action_class']} 属 **L4 禁止类**："
                                  f"Agent 一律不得执行，只记录审计"),
                "report": (f"已**拒绝**执行 {action['action_class']} @ "
                           f"{action['target']['id']}：L4 是禁止类动作。"
                           f"{action.get('rollback_plan') or ''}")})

        return _apply

    @staticmethod
    def _confirm_write_disabled(body: dict[str, Any], action: Mapping[str, Any],
                                risk: Mapping[str, Any], allow_write: bool) -> Any:
        """判定 `options.allow_write=false` 的结局：只输出预案、不执行。"""
        if allow_write:
            return None

        def _apply() -> None:
            body.update({
                "status": ChatStatus.REJECTED.value,
                "reject_reason": ("options.allow_write=false → 即使意图是 REMEDIATION "
                                  "也只输出预案、不执行"),
                "report": (f"已生成 {risk['level']} 预案但**未执行**（写能力未开启）："
                           f"{action['action_class']} @ {action['target']['id']}。"
                           f"如需落地，请开启 options.allow_write 并走人工审批。")})

        return _apply

    def _confirm_needs_approval(self, body: dict[str, Any], stored: Mapping[str, Any],
                                session_id: str, action: Mapping[str, Any],
                                risk: Mapping[str, Any]) -> Any:
        """判定需审批的结局：登记待批 + `clarify` + `approval_required`（L3 或标了 `requires_approval`）。"""
        if not (risk.get("requires_approval") or risk["level"] == "L3"):
            return None

        def _apply() -> None:
            pending = self.register_approval(
                incident_id=str(stored.get("incident_id") or ""), session_id=session_id,
                action_class=str(action["action_class"]),
                target=str(action["target"]["id"]),
                action_hash=str(stored.get("action_hash") or ""),
                risk_level=str(risk["level"]),
                diff_preview=str(action.get("dry_run_diff") or ""))
            body.update({
                "status": "clarify",
                "clarification": {
                    "questions": [{"slot": "approval",
                                   "question": (f"{risk['level']} 动作需要人工审批："
                                                f"{action['action_class']} @ "
                                                f"{action['target']['id']}")}],
                    "reason": "L3 高危动作强制人工审批",
                    "approval_required": True},
                "approval_required": {
                    "request_id": pending.request_id, "action_hash": pending.action_hash,
                    "level": pending.risk_level, "diff_preview": pending.diff_preview,
                    "expires_at": round(pending.expires_in(), 1)},
                "report": (f"预案已就绪（{risk['level']}）：{action['action_class']} @ "
                           f"{action['target']['id']}。**等待人工审批** —— "
                           f"请用 POST /v1/chat/resume（resume_type=approval，"
                           f"approver 必须 human: 开头）提交结论。")})

        return _apply

    async def _resume_approval(self, payload: Mapping[str, Any], *,
                               session_id: str, trace_id: str) -> dict[str, Any]:
        """落定审批恢复：**红线的检查顺序就是这里**（先人、再存在性、再哈希）。"""
        approval = payload.get("approval")
        if not isinstance(approval, Mapping):
            raise ResumeError(422, INVALID_PARAMS, "approval 段缺失")
        approver = str(approval.get("approver") or "").strip()
        request_id = str(approval.get("request_id") or "").strip()
        action_hash = str(approval.get("action_hash") or "").strip()
        approved = bool(approval.get("approved"))

        # ---- ① 审批人必须是 human:*（Agent 不能自己批自己）----
        if not approver:
            raise ResumeError(422, INVALID_PARAMS, "approval.approver 必填")
        if not approver.startswith("human:"):
            raise ResumeError(
                403, ErrorCode.TOOL_FORBIDDEN.value,
                f"审批人必须以 human: 开头，实际 {approver!r} —— "
                f"agent: 前缀一律拒绝（审批人不能是 Agent 自身）")
        # ---- ② request_id 必须存在且未过期 ----
        if not request_id:
            raise ResumeError(422, INVALID_PARAMS, "approval.request_id 必填")
        pending = self._require_pending(request_id)
        # ---- ③ action_hash 必须与发起时一致（防"批 A 做 B"）----
        if not action_hash:
            raise ResumeError(422, INVALID_PARAMS,
                              "approval.action_hash 必填")
        if action_hash != pending.action_hash:
            logger.warning("审批哈希不一致：request_id=%s 期望=%s 实际=%s —— 安全告警",
                           request_id, pending.action_hash, action_hash)
            raise ResumeError(
                409, ErrorCode.APPROVAL_HASH_MISMATCH.value,
                f"action_hash 与发起时不一致（期望 {pending.action_hash}，"
                f"实际 {action_hash}）→ 拒绝执行并告警（防越权替换）")
        self.approvals.clear(request_id)
        logger.info("审批通过：request_id=%s approver=%s approved=%s",
                    request_id, approver, approved)
        return await self._finish_approval(pending, approved=approved, approver=approver,
                                           reason=str(approval.get("reason") or ""),
                                           trace_id=trace_id)

    def _require_pending(self, request_id: str) -> PendingApproval:
        """取待批条目；不存在 / 过期 / **被容量淘汰** → 404（三者的话术与 detail 不同）。

        ★ **"被容量挤掉"与"过期"是两件事**。以前两者都报"不存在或已过期"，
          于是用户以为"我等太久了"，实际是服务端把这条挤掉了
          （`APPROVAL_MAX_PENDING` 满时淘汰最老的）—— 他会一直重试同一份审批。
          码仍是契约里的 `APPROVAL_EXPIRED`（错误码集合不能自己扩），
          但 `detail` 给出**机器可读**的区分。
        """
        pending = self.approvals.get(request_id)
        if pending is not None and not pending.expired():
            return pending
        evicted = self.approvals.was_evicted(request_id)
        raise ResumeError(
            404, ErrorCode.APPROVAL_EXPIRED.value,
            (f"审批请求 {request_id!r} 已被**容量淘汰**（待批条数超过上限，"
             f"它被当作最老的挤掉了）—— 请重新发起该变更预案"
             if evicted else
             f"审批请求 {request_id!r} 不存在或已过期"
             f"（有效期 {APPROVAL_TTL_MINUTES} 分钟）")
            + " —— request_id 必须存在且未过期",
            detail="evicted_by_capacity" if evicted else "absent_or_expired")

    async def _finish_approval(self, pending: PendingApproval, *, approved: bool,
                               approver: str, reason: str,
                               trace_id: str) -> dict[str, Any]:
        """落定审批之后的结局。★ **执行端没接入就说没接入**，绝不假装执行成功。"""
        body = empty_contract_body(
            pending.session_id, trace_id=trace_id, incident_id=pending.incident_id,
            # ★ 待批动作只在写能力开启时才会登记 → 这里的事实就是 True（不是猜）
            intent_name=Intent.REMEDIATION.value, write_allowed=True)
        base = await self.get_incident(pending.incident_id)
        if base:
            body.update(base)
        body["trace_id"] = trace_id
        body["approval"] = {"request_id": pending.request_id, "approved": approved,
                            "approver": approver, "reason": reason,
                            "action_hash": pending.action_hash}
        if not approved:
            body["status"] = ChatStatus.REJECTED.value
            body["reject_reason"] = (f"{approver} 拒绝了 {pending.risk_level} 动作 "
                                     f"{pending.action_class}（{reason or '未说明理由'}）")
            body["report"] = (f"已按人工审批结论**拒绝执行**：{pending.action_class} "
                              f"@{pending.target}（{pending.risk_level}）。"
                              f"没有任何变更被应用；如需继续请重新发起。")
            await self._store_incident(body)
            return body
        # ★ 批准了，但**执行端未接入**（ChangeChannel）——如实说，不假装成功
        body["status"] = "failed"
        body["error"] = {
            "code": ErrorCode.TOOL_UNAVAILABLE.value,
            "message": ("审批已通过，但**变更执行端尚未接入**（GitOps/Argo CD）"
                        "→ 本次没有应用任何变更，也不得盲目重试"),
        }
        body["report"] = (f"审批通过（{approver}），但变更执行端未接入 → "
                          f"**没有应用任何变更**。动作：{pending.action_class} "
                          f"@{pending.target}（{pending.risk_level}，"
                          f"action_hash={pending.action_hash}）。")
        await self._store_incident(body)
        return body

    def register_approval(self, *, incident_id: str, session_id: str, action_class: str,
                          target: str, action_hash: str, risk_level: str,
                          diff_preview: str = "") -> PendingApproval:
        """登记一条待批动作（`change_write` 的执行器闸门会调它）。"""
        return self.approvals.register(PendingApproval(
            request_id=f"apr_{uuid.uuid4().hex[:16]}", incident_id=incident_id,
            session_id=session_id, action_hash=action_hash, risk_level=risk_level,
            action_class=action_class, target=target, diff_preview=diff_preview))


def _confirm_executor_missing(body: dict[str, Any], action: Mapping[str, Any],
                              risk: Mapping[str, Any]) -> Any:
    """落定 L0/L1/L2 的结局：不需要审批，但**执行端没接入** → `failed`。

    ★ 它是四种结局里唯一一个「**什么都没做**」的 —— 所以文案必须明说
      "没有应用任何变更"，而不是给一个看起来成功的响应。
    """
    del risk  # 这一支只用 action（风险等级已由调用方写进 body）

    def _apply() -> None:
        body.update({
            "status": "failed",
            "error": {"code": ErrorCode.TOOL_UNAVAILABLE.value,
                      "message": ("变更执行端尚未接入（GitOps/Argo CD）"
                                  "→ 本次**没有应用任何变更**，也不得盲目重试")},
            "report": (f"已确认 {body['risk']['level']} 动作 {action['action_class']} @ "
                       f"{action['target']['id']}，但执行端未接入 → "
                       f"**没有应用任何变更**。回滚方案：{action.get('rollback_plan')}")})

    return _apply

