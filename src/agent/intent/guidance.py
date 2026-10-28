"""Ambiguous fault vocabulary in a general-method question needs semantics."""
import re
from ..common.enums import Intent


METHOD=re.compile(r'注意事项|注意什么|分类原则|最佳实践|设计.{0,12}(?:重试|容错|恢复|退避)|'
                  r'(?:哪些|什么).{0,18}(?:可以|适合|允许|不宜|应该|不能).{0,12}(?:重试|尝试|恢复|忽略)|'
                  r'best practices|what.{0,16}(?:errors|failures).{0,18}(?:retried|retry)',re.I)
INCIDENT=re.compile(r'(?:已经|正在|当前|刚才|刚刚|今天|线上).{0,24}(?:报错|故障|异常|失败|超时|死锁|出错)|'
                    r'(?:发生了|出现了|检测到|报了).{0,20}(?:错误|异常|故障|死锁)|'
                    r'(?:请|帮我|麻烦).{0,8}(?:排查|诊断|定位)|investigate|diagnose',re.I)


def needs_semantics(text,hit):
    return hit.intent is Intent.INCIDENT_RCA and bool(METHOD.search(text)) and not INCIDENT.search(text)
