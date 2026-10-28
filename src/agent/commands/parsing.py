"""Only syntax is parsed here. Names, dates and content are interpreted by a model."""
import re
from ..intent.routing_policy import routing_input


def has_commands(text):return bool(re.search(r'@/[A-Za-z_]\w*',text))


def parse(text):
    text=routing_input(text)
    if len(text)>4000:raise ValueError('命令链最多4000字符')
    parts=[];buf=[];quote=None;escaped=False
    for char in text:
        if escaped:buf.append(char);escaped=False;continue
        if char=='\\':buf.append(char);escaped=True;continue
        if char in ('"',"'",'`'):
            if quote==char:quote=None
            elif quote is None:quote=char
        if char=='|' and quote is None:parts.append(''.join(buf).strip());buf=[]
        else:buf.append(char)
    if quote:raise ValueError('引号未闭合；文字中的 | 请放在引号内')
    parts.append(''.join(buf).strip())
    if len(parts)>8 or any(not p for p in parts):raise ValueError('命令链需为1~8个非空步骤')
    result=[]
    for part in parts:
        hits=list(re.finditer(r'@/([A-Za-z_]\w*)\b',part))
        if len(hits)!=1:raise ValueError('每步仅允许一个 @/命令，步骤用 | 分隔')
        hit=hits[0]
        result.append(dict(command=hit.group(1),text=part,argument=part[hit.end():].strip()))
    return result
