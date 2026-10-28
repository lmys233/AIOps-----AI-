"""OpenAI 兼容协议的 `LLMGateway` 实现（DeepSeek / 阿里百炼 / OpenAI / vLLM 通用）。

--------------------------------------------------------------------------------
★ 为什么押注"OpenAI 兼容"这一种协议
--------------------------------------------------------------------------------

要求是"主力模型可替换（API ↔ 本地），不得硬编码模型名"。
而 DeepSeek、阿里云百炼（`compatible-mode`）、OpenAI、vLLM / Ollama 都提供
**同一套** `/chat/completions` 协议 —— 押注它，就能做到"换供应商只改两行配置"：

  · DeepSeek ：`https://api.deepseek.com`
  · 阿里百炼 ：`https://dashscope.aliyuncs.com/compatible-mode/v1`
  · 本地 vLLM：`http://127.0.0.1:8001/v1`

★ `base_url` 带不带 `/v1` 都能用（见 `_chat_endpoint()` 的归一化）——
  这不是"宽容"，而是因为这两家官方文档给的写法不一致，而**配置是给人写的**。

--------------------------------------------------------------------------------
★ 结构化输出：优先 function calling，被拒则退回 JSON mode
--------------------------------------------------------------------------------

要求是"走约束解码，不接受自由文本返回"。在 OpenAI 兼容协议里，
最接近"约束解码"的是 **function calling**：把 schema 作为工具的 `parameters` 传进去，
模型必须按它返回参数（DeepSeek / Qwen 都支持）。若供应商拒了这个字段（HTTP 400/422），
自动退回 **JSON mode**（`response_format={"type":"json_object"}`）+ 把 schema 写进提示词。

★ 无论走哪条路，**最终判据都是 `schema.model_validate_json()`** ——
  协议层说"我保证"不算数（`response_format` 只是"多半会"，不是"一定"）。
  这与本项目"SDK 说自己对不算数"是同一条口径。

--------------------------------------------------------------------------------
★ 离线可测
--------------------------------------------------------------------------------

`transport` 可注入（`httpx.MockTransport`），所以"重试 / 降级 / 校验失败"
这些分支**不需要外网**就能验（见 `tests/agent_unit.py`）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, ValidationError

from ..common.enums import ErrorCode
from .gateway import (
    DEFAULT_TIMEOUT_SEC,
    MAX_SCHEMA_RETRIES,
    MAX_TIMEOUT_RETRIES,
    MAX_TOKENS_CEILING,
    MAX_TRUNCATION_RETRIES,
    LLMCallRecord,
    LLMError,
    LLMGateway,
    UsageSink,
)

__all__ = ["OpenAICompatGateway"]

# 结构化输出的两种模式（sticky：一旦 discovery 出可用模式就固定下来）。
# ★ 三态而非两态：`None` = 还没探明（**默认走 function calling**，与 `_MODE_FUNCTION` 同支），
#   `_MODE_FUNCTION` = 探明并固定，`_MODE_JSON` = 供应商拒了 tools 之后的回退态。
_MODE_FUNCTION = "function"
_MODE_JSON = "json"


from ..execution.runtime import durable_model
from .context import contextual,DEFAULT_CONTEXT_TOKENS
from .call_policy import Policy,bounded,retry,CURRENT as CALL_POLICY,ModelFailure


class OpenAICompatGateway(LLMGateway):
    """走 `/chat/completions` 的通用网关。"""
    supports_witness_batches=True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: Optional[str],
        model: str,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        usage_sink: Optional[UsageSink] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None,
        context_tokens: int = DEFAULT_CONTEXT_TOKENS,
        retry_policy: Optional[Policy] = None,
        review_timeout_sec: float = 45,
        role_options: Optional[dict] = None,
        structured_mode: Optional[str] = None,
    ) -> None:
        if not str(base_url).strip():
            raise ValueError("base_url 不能为空（模型可替换，但必须显式配置）")
        if not str(model).strip():
            raise ValueError("model 不能为空（不得硬编码模型名，必须来自配置）")

        self.base_url = str(base_url).strip().rstrip("/")
        self.api_key = (api_key or "").strip() or None
        self.default_model = str(model).strip()
        self.timeout_sec = float(timeout_sec)
        if isinstance(context_tokens,bool) or not isinstance(context_tokens,int) or context_tokens<1024:
            raise ValueError('context_tokens 必须为至少1024的应用上下文上限')
        self.context_tokens=context_tokens
        self.usage_sink = usage_sink
        self.retry_policy=retry_policy or Policy()
        if not 0<review_timeout_sec<=45:raise ValueError('review timeout must be finite and <=45s')
        self.review_timeout_sec=float(review_timeout_sec)
        from .role_options import validate
        import hashlib
        if set(role_options or {})-{'command','attribution_candidates','qa','qa_repair','qa_review','metric_analyst','task_dialogue'}:raise ValueError('unsupported role model options')
        self.role_options={role:validate(value) for role,value in (role_options or {}).items()}
        self.prompt_version='model-policy-v1:'+str(self.retry_policy)+':review='+str(self.review_timeout_sec)
        self.prompt_version+=':options='+hashlib.sha256(json.dumps(self.role_options,sort_keys=True).encode()).hexdigest()[:16]

        # ★ 允许注入 client / transport：离线测试用 MockTransport，
        #   生产用默认（连接池复用，避免每次调用都重新握手）。
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_sec),
            transport=transport,
            follow_redirects=False,
        )
        self._owns_client = client is None
        from urllib.parse import urlsplit
        if structured_mode not in (None,'auto','function','json'):raise ValueError('invalid structured mode')
        # Official thinking-mode API rejects forced named tool_choice. Choose its
        # documented JSON format before dispatch; retain full schema validation.
        self._mode=(structured_mode if structured_mode in ('function','json') else
            _MODE_JSON if urlsplit(self.base_url).hostname=='api.deepseek.com' else None)
        self.prompt_version+=':structured='+str(self._mode or 'auto')

    # ------------------------------------------------------------------
    #  端点与请求头
    # ------------------------------------------------------------------
    def _chat_endpoint(self) -> str:
        """把配置里的 base_url 归一成 `/chat/completions` 的完整地址。

        `https://api.deepseek.com` → `…/v1/chat/completions`
        `https://dashscope.aliyuncs.com/compatible-mode/v1` → `…/v1/chat/completions`
        `http://127.0.0.1:8001/v1/chat/completions` → 原样
        """
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _models_endpoint(self) -> str:
        """探活端点。相比"发一条真请求"，它**不消耗 token**。"""
        base = self.base_url
        if base.endswith("/v1") or base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")] if base.endswith("/chat/completions") else base
        else:
            base = f"{base}/v1"
        return f"{base}/models"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # ------------------------------------------------------------------
    #  探活
    # ------------------------------------------------------------------
    async def health(self) -> tuple[bool, str]:
        """`GET /models` 探活。**不消耗 token**、不依赖具体模型是否可用。"""
        if not self.api_key:
            return False, "未配置 AIOPS_LLM_API_KEY（模型那两级将不启用）"
        try:
            resp = await self._client.get(self._models_endpoint(), headers=self._headers())
        except httpx.TimeoutException:
            return False, f"探活超时（{self.timeout_sec:g}s）：{self._models_endpoint()}"
        except httpx.HTTPError as exc:
            return False, f"探活失败：{type(exc).__name__}"

        if resp.status_code == 200:
            names = _model_names(resp)
            hit = self.default_model in names if names else True
            detail = f"HTTP 200（模型 {'在' if hit else '**不在'} 列表里：{self.default_model}）"
            return True, detail
        return False, f"HTTP {resp.status_code}：{_short(resp.text)}"

    async def list_models(self) -> list[str]:
        """供应商声明的模型列表（`GET /models`）。

        ★ 为什么要有它：模型名是**供应商的事实**，不是我们能猜的。
          实测这一家列出来的是 `deepseek-flash` / `deepseek-v4-pro`，
          而"deepseek-chat"这类常见名**不在列表里却仍能调通**（别名/网关）。
          所以它只用来**提示**，不用来判定可用性 —— 真正的判据是一次真实调用。
        """
        try:
            resp = await self._client.get(self._models_endpoint(), headers=self._headers())
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            return []
        return _model_names(resp)

    # ------------------------------------------------------------------
    #  生成
    # ------------------------------------------------------------------
    def context_body(self,prompt,schema,role,max_tokens,model=None,temperature=0):
        body={'model':model or self.default_model,'messages':_build_messages(prompt,schema,role,json_mode=self._mode==_MODE_JSON),
              'temperature':temperature,'max_tokens':max_tokens}
        if self._mode in (None,_MODE_FUNCTION):
            body.update(tools=[_tool_for(schema)],tool_choice={'type':'function','function':{'name':_tool_name(schema)}})
        else:body['response_format']={'type':'json_object'}
        import copy
        body.update(copy.deepcopy(self.role_options.get(role,{})));return body

    def policy_for(self,role):
        from dataclasses import replace
        timeout=self.review_timeout_sec if role=='qa_review' else 25 if role in ('qa','qa_repair','intent_slow','intent_review','web_probe','probe_dialogue','command') else self.retry_policy.timeout
        return replace(self.retry_policy,timeout=min(timeout,self.retry_policy.timeout))

    @bounded
    @contextual
    @durable_model
    async def generate(self,prompt:str,schema:type[BaseModel],*,role:str,
                       model:Optional[str]=None,temperature:float=0.0,max_tokens:int=1024)->BaseModel:
        if not isinstance(schema,type) or not issubclass(schema,BaseModel):
            raise TypeError("schema 必须是 Pydantic BaseModel 的子类")
        from .accounting import CallMeter
        import asyncio
        meter=CallMeter();target=(model or self.default_model).strip()
        timeouts=schemas=truncations=0
        previous=correction=fallback="";ok=False;error=None;failure_kind=None
        try:
            while True:
                invocation=CALL_POLICY.get()
                if len(meter.rows)>=invocation.policy.max_attempts:
                    raise ModelFailure('attempt_limit','本逻辑调用物理尝试已达到上限',len(meter.rows))
                messages=_build_messages(prompt,schema,role,json_mode=self._mode==_MODE_JSON,
                    previous=previous,correction=correction)
                body={"model":target,"messages":messages,"temperature":temperature,"max_tokens":max_tokens}
                if self._mode in (None,_MODE_FUNCTION):
                    body.update(tools=[_tool_for(schema)],tool_choice={"type":"function","function":{"name":_tool_name(schema)}})
                else:body['response_format']={"type":"json_object"}
                import copy
                body.update(copy.deepcopy(self.role_options.get(role,{})))
                from .context import limit_request
                limit_request(body,self.context_tokens)
                row,ticket=await meter.begin(body,self.context_tokens)
                try:
                    resp=await self._client.post(self._chat_endpoint(),headers=self._headers(),json=body)
                except asyncio.CancelledError:
                    await meter.finish(row,ticket,'cancelled');raise
                except httpx.TimeoutException as exc:
                    row['failure_kind']='connect_timeout' if isinstance(exc,httpx.ConnectTimeout) else 'read_timeout' if isinstance(exc,httpx.ReadTimeout) else 'transport_timeout'
                    await meter.finish(row,ticket,'timeout')
                    if timeouts<MAX_TIMEOUT_RETRIES:
                        timeouts+=1;await retry(meter,row,'transport_timeout');continue
                    raise LLMError(ErrorCode.LLM_TIMEOUT,f"模型调用超时（{self.timeout_sec:g}s，已重试 {timeouts} 次）",
                        role=role,model=target,attempts=len(meter.rows)) from None
                except httpx.HTTPError as exc:
                    await meter.finish(row,ticket,'transport_error')
                    raise LLMError(ErrorCode.INTERNAL_ERROR,'模型供应商不可达：'+type(exc).__name__,
                        role=role,model=target,attempts=len(meter.rows),failure_kind='transport_error') from None
                try:data=_safe_json(resp.text)
                except Exception:
                    if resp.status_code>=400:data={}
                    else:
                        await meter.finish(row,ticket,'invalid_json');raise
                row['http_status']=resp.status_code
                if resp.status_code in (400,404,422) and self._mode!=_MODE_JSON:
                    await meter.finish(row,ticket,'format_rejected',data)
                    self._mode=_MODE_JSON;fallback=f"HTTP {resp.status_code}：结构化模式被拒，改用JSON模式"
                    await retry(meter,row,'format_fallback');continue
                if resp.status_code in (429,500,502,503,504):
                    await meter.finish(row,ticket,'http_transient',data)
                    await retry(meter,row,'http_transient',resp);continue
                if resp.status_code>=400:
                    await meter.finish(row,ticket,'http_error',data)
                    raise LLMError(ErrorCode.INTERNAL_ERROR,f"模型接口返回 HTTP {resp.status_code}：{_short(resp.text)}",
                        role=role,model=target,attempts=len(meter.rows),failure_kind='permission_denied' if resp.status_code in (401,403) else 'http_nonretryable')
                if self._mode is None:self._mode=_MODE_FUNCTION
                try:content=_extract_structured(data);finish=_finish_reason(data)
                except Exception:
                    await meter.finish(row,ticket,'protocol_error',data);raise
                row['finish_reason']=finish
                try:row['reasoning_tokens']=_reasoning_tokens(data)
                except (TypeError,ValueError):row['reasoning_tokens']=None
                if finish=='length':
                    await meter.finish(row,ticket,'truncated',data)
                    if truncations<MAX_TRUNCATION_RETRIES and max_tokens<MAX_TOKENS_CEILING:
                        truncations+=1;max_tokens=min(max_tokens*4,MAX_TOKENS_CEILING)
                        await retry(meter,row,'output_truncated');continue
                    raise LLMError(ErrorCode.LLM_SCHEMA_INVALID,f"模型输出被 max_tokens={max_tokens} 截断；reasoning_tokens={row['reasoning_tokens']}",
                        role=role,model=target,attempts=len(meter.rows),failure_kind='output_truncated')
                try:result=schema.model_validate_json(content)
                except (ValidationError,ValueError) as exc:
                    await meter.finish(row,ticket,'schema_invalid',data)
                    if schemas<MAX_SCHEMA_RETRIES:
                        schemas+=1;previous=content;correction=_short(str(exc))
                        await retry(meter,row,'schema_invalid');continue
                    raise LLMError(ErrorCode.LLM_SCHEMA_INVALID,f"模型返回无法通过 {schema.__name__} 校验（已重试 {schemas} 次）：{_short(str(exc))}",
                        role=role,model=target,attempts=len(meter.rows)) from None
                await meter.finish(row,ticket,'accepted',data)
                ok=True;return result
        except BaseException as exc:
            error=exc.code.value if isinstance(exc,LLMError) else type(exc).__name__
            failure_kind=getattr(exc,'failure_kind',None) or ('stage_deadline' if isinstance(exc,asyncio.CancelledError) and invocation.timeout_scope.expired() else
                'cancelled' if isinstance(exc,asyncio.CancelledError) else 'schema_invalid' if isinstance(exc,LLMError) and exc.code==ErrorCode.LLM_SCHEMA_INVALID else
                'transport_timeout' if isinstance(exc,LLMError) and exc.code==ErrorCode.LLM_TIMEOUT else 'model_unavailable')
            raise
        finally:
            if meter.rows:
                totals=meter.totals()
                self._record(role,target,totals['input_tokens'],totals['output_tokens'],meter.started,len(meter.rows),ok,error,
                    extra={'attempt_records':meter.rows,'fallback_reason':fallback,
                           'failure_kind':failure_kind,
                           'retry_policy':{'timeout':invocation.policy.timeout,'max_attempts':invocation.policy.max_attempts,
                                           'backoff':invocation.policy.backoff,'max_backoff':invocation.policy.max_backoff},
                           'context':getattr(prompt,'context_audit',{})},call_id=meter.call_id,
                    usage_known=totals['usage_known'],reserved_tokens=totals['reserved_tokens'])

    def _record(self,role,model,input_tokens,output_tokens,started,attempts,ok,error_code,extra=None,
                *,call_id='',usage_known=True,reserved_tokens=0):
        record=LLMCallRecord(role=role,model=model,input_tokens=input_tokens,output_tokens=output_tokens,
            latency_ms=(time.perf_counter()-started)*1000,attempts=attempts,ok=ok,error_code=error_code,
            extra=dict(extra or {}),call_id=call_id,usage_known=usage_known,reserved_tokens=reserved_tokens)
        _span_attributes(record)
        from .accounting import CALL_RECORDS,REQUEST_RECORDS
        local=CALL_RECORDS.get()
        if local is not None:local.append(record)
        if self.usage_sink is not None:
            try:self.usage_sink(record)
            except Exception:pass
        request=REQUEST_RECORDS.get()
        if request is not None and not any(r is record for r in request):request.append(record)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# ----------------------------------------------------------------------
#  模块级小工具
# ----------------------------------------------------------------------
def _short(text: str, limit: int = 200) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _safe_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _model_names(resp: httpx.Response) -> list[str]:
    data = _safe_json(resp.text)
    items = data.get("data")
    if not isinstance(items, list):
        return []
    return [str(it.get("id")) for it in items if isinstance(it, dict) and it.get("id")]


def _usage(data: dict[str, Any]) -> tuple[int, int]:
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def _reasoning_tokens(data: dict[str, Any]) -> int:
    """思考型模型的推理 token 数（`completion_tokens_details.reasoning_tokens`）。

    ★ 单独取出来是为了**成本可解释**：一次调用花 100 个 token 里可能有 80 个是"思考"，
      看不到这个数就解释不了"为什么这么贵/为什么 content 是空的"。
    """
    usage = data.get("usage") or {}
    details = (usage.get("completion_tokens_details") or {}) if isinstance(usage, dict) else {}
    if not isinstance(details, dict):
        return 0
    return int(details.get("reasoning_tokens") or 0)


def _finish_reason(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    return str((choices[0] or {}).get("finish_reason") or "")


def _extract_structured(data: dict[str, Any]) -> str:
    """从响应里取出"结构化结果"的 JSON 文本。

    · function calling：`choices[0].message.tool_calls[0].function.arguments`
    · JSON mode：`choices[0].message.content`
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        fn = (tool_calls[0] or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            return args
        if isinstance(args, dict):          # 少数实现直接给对象
            return json.dumps(args, ensure_ascii=False)
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _tool_name(schema: type[BaseModel]) -> str:
    return f"emit_{schema.__name__.lower()}"


def _tool_for(schema: type[BaseModel]) -> dict[str, Any]:
    """把 Pydantic schema 变成 function calling 的 `parameters`。

    ★ 用 `model_json_schema()` 而不是手写 JSON Schema —— 手写必然与模型漂移。
    """
    return {
        "type": "function",
        "function": {
            "name": _tool_name(schema),
            "description": f"按要求返回 {schema.__name__} 结构",
            "parameters": schema.model_json_schema(),
        },
    }


def _build_messages(
    prompt: str,
    schema: type[BaseModel],
    role: str,
    *,
    json_mode: bool = False,
    previous: str = "",
    correction: str = "",
) -> list[dict[str, str]]:
    """system 提示词：角色 + 输出契约（+ 上一次的错误，供模型自修）。

    ★ JSON mode 下**必须**把 schema 写进提示词 —— 那种模式只保证"是合法 JSON"，
      不保证字段对不对；约束力全靠提示词 + 我们的二次校验。

    ★ 这段文字里出现 `JSON` 是有用的**副作用**：供应商要求
      "用 `response_format=json_object` 时提示词里必须出现 json 字样"，
      缺了就直接 400。
    """
    schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    if json_mode:
        instruction = (
            f"你必须只输出一个 JSON 对象（不要 markdown 代码块、不要解释文字），"
            f"且严格符合这个 JSON Schema：\n{schema_text}"
        )
    else:
        instruction = (
            f"你必须调用工具 `{_tool_name(schema)}` 来返回结果，参数严格符合它的 schema。"
        )
    messages = [
        {"role": "system", "content": f"角色：{role}。{instruction}"},
        {"role": "user", "content": prompt},
    ]
    if previous:
        # ★ 把上一轮的产物与校验错误一起喂回：模型能据此**定向修正**，
        #   而不是"原样再来一次"（那样能不能过只能靠运气）
        messages.append({"role": "assistant", "content": previous})
        messages.append({
            "role": "user",
            "content": (f"上次的返回不符合要求的 JSON Schema，校验错误：\n{correction}\n"
                        f"请按错误修正后，只输出符合 schema 的 JSON。"),
        })
    return messages


def _span_attributes(record: LLMCallRecord) -> None:
    """把记账写进 OTel span 属性。

    ★ 用 `set_attribute` 而不是"新建一个 span"：调用方（如意图层）通常已经有
      自己的 span，这里只是**补充属性**；凭空多一层 span 会让链路图变噪。
      OpenTelemetry 不可用时静默跳过 —— 链路是增强，不能拖垮调用。
    """
    try:
        from opentelemetry import trace  # noqa: PLC0415

        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        span.set_attribute("llm.role", record.role)
        span.set_attribute("llm.model", record.model)
        span.set_attribute("llm.input_tokens", record.input_tokens)
        span.set_attribute("llm.output_tokens", record.output_tokens)
        span.set_attribute("llm.attempts", record.attempts)
        span.set_attribute("llm.usage_known", record.usage_known)
        span.set_attribute("llm.reserved_tokens", record.reserved_tokens)
        span.set_attribute("llm.latency_ms", record.latency_ms)
        if record.error_code:
            span.set_attribute("llm.error_code", record.error_code)
    except Exception:  # noqa: BLE001
        return
