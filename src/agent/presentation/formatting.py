"""Pure detail formatting shared by wording adapters and final report."""
import json


def details(rows,kind='table'):
    items=[(str(r.get('label') or ''),str(r.get('value') or '')) for r in rows]
    if not items:return ''
    if str(kind or 'table').lower()=='json':
        return json.dumps({k:v for k,v in items},ensure_ascii=False,indent=2)
    lines=['| 项 | 内容 |','|---|---|']
    for label,value in items:lines.append('| '+label+' | '+value.replace('|','\\|')+' |')
    return '\n'.join(lines)
