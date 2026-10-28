"""Relative system references require an already verified request scope."""
import re
from .context import current

RELATIVE=re.compile(r'(?:当前|这个|这套|本|该|此|所选(?:的)?|选中(?:的)?)\s*(?:业务)?(?:系统|应用|平台)'
    r'|\b(?:this|current|selected)\s+(?:system|application|platform)\b',re.I)
GENERIC=re.compile(r'(?:^|[，。；！？\n])\s*(?:请\s*)?(?:帮我\s*)?'
    r'(?:(?:检查|排查|诊断|查看|查询|查|看)(?:一下)?\s*)?'
    r'(?P<target>(?:业务)?(?:系统|应用|平台))'
    r'(?=$|[\s，。；！？]|有没有|有无|是否|是不是|存在|出现|发生|有|正常|健康|状态|怎样|怎么样|如何|整体)')


def system_reference(text):
    scope=current()
    if not scope:return None
    match=RELATIVE.search(text)
    if match:
        prefix=text[max(0,match.start()-6):match.start()]
        if re.search(r'(?:不是|不要(?:查|看)?|不查|排除)\s*$',prefix):return None
        return match.group(0)
    name=scope.observed_name
    if name and re.search(r'(?<![\w-])'+re.escape(name)+r'(?![\w-])',text):return name
    match=GENERIC.search(text)
    if match:return match.group('target')
    return None


def enumeration_reference(text,pattern,services):
    if services:return None
    match=pattern.search(text)
    return match.group(0) if match else system_reference(text)


def service_reference(text,names):
    """Use the same registered vocabulary/slot extraction for dialogue and routing."""
    if not names:return ''
    from ..intent.slots import ACTIVE_SLOTS,extract_slots
    from .slot_config import vocabulary
    extraction=extract_slots(text,config=vocabulary(ACTIVE_SLOTS,names))
    picked=[n for n in extraction.slots.service_names if n in names]
    return '、'.join(picked) if picked else ''
