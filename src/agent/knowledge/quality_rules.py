"""Deterministic units/anchors, then validate the semantic judge's witnesses."""
import re
from ..domain.presentation import digest

LIMITATION=re.compile(r'缺少|不足|未(?:说明|提供|包含|覆盖)|没有.{0,10}(?:依据|资料|说明|提供)|无法.{0,12}(?:确认|判断|回答)|不能.{0,12}(?:确认|判断)|待核实|not (?:provided|covered)|insufficient',re.I)
SCOPE=re.compile(r'摘录|资料|来源|知识|文档|证据|provided|sources|excerpts',re.I)


def display_citations(text):
    prose=''.join(s for s in re.split(r'(```[\s\S]*?```|`[^`\n]+`)',text) if not s.startswith('`'))
    return re.findall(r'\[(K[1-9][0-9]*)\]',prose)


def pieces(text):
    # Decimal points and SQL punctuation remain intact; code lines are separate units.
    out=[]
    for block in re.split(r'(```[\s\S]*?```)',text):
        chunks=[block] if block.startswith('```') else re.split(r'(?<=[。！？!?])\s*|\n+',block)
        out.extend(s.strip() for s in chunks if s.strip())
    return out


def inventory(answer,query):
    units=[];origins={};presentation=[]
    groups=[('summary',answer.summary,answer.summary_citations)]
    groups.extend(('section_'+str(n),s.text,s.citations) for n,s in enumerate(answer.sections,1))
    for group,text,citations in groups:
        seen={}
        for n,part in enumerate(pieces(text),1):
            # Only display markers outside code are removed. Unknown inline IDs
            # are still checked even when they constitute a display-only piece.
            presentation.append({'text':part,'citations':list(citations)})
            segments=re.split(r'(```[\s\S]*?```|`[^`\n]+`)',part)
            clean=''.join(s if s.startswith('`') else re.sub(r'\[K[1-9][0-9]*\]','',s) for s in segments).strip()
            if not clean:continue
            key=(clean,tuple(citations));uid=seen.get(key)
            if uid is None:
                uid=group+'_'+str(n);seen[key]=uid
                units.append({'id':uid,'text':clean,'citations':list(dict.fromkeys(citations))})
            origins.setdefault(uid,[]).append({'group':group,'position':n,'original':part})
    parts=[p.strip() for p in re.split(r'[？?\n]|以及|并且|同时|另外',query) if p.strip()]
    questions=[{'id':'Q'+str(n),'text':p} for n,p in enumerate(parts,1)]
    return {'units':units,'questions':questions,'unit_origins':origins,'presentation':presentation,
            'answer_hash':digest(answer.model_dump()),'query_hash':digest(query)}


def anchors(text):
    patterns=[r'\d{4}-\d{2}-\d{2}(?:T[\d:.+Z-]+)?',r'`([^`\n]+)`',
              r'(?<![A-Za-z0-9_])[A-Z0-9_]{4,32}(?![A-Za-z0-9_])',
              r'(?:表|字段|对象|函数|服务|错误码|SQLSTATE)\s*[=:：]?\s*[`"\']?([A-Za-z_][A-Za-z0-9_./:-]{1,100}|[0-9][A-Z0-9]{4})',
              r'(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])',
              r'(?<![A-Za-z0-9_])\d+(?:\.\d+)?\s*(?:%|毫秒|秒|分钟|小时|天|次|轮|维|ms|s)']
    values=[]
    for pattern in patterns:values.extend(re.findall(pattern,text))
    return list(dict.fromkeys(values))


def rule_check(inv,sources):
    known={s['citation']:s for s in sources};issues=[]
    if not inv['units']:issues.append({'code':'ANSWER_EMPTY','unit_id':'','detail':'只有引用编号，没有可评审回答'})
    for item in inv.get('presentation',[]):
        inline=display_citations(item['text'])
        if any(cid not in known or cid not in item['citations'] for cid in inline):
            issues.append({'code':'INVALID_CITATION','unit_id':'','detail':'展示引用未知或未声明'})
    if len(inv['units'])>32 or len(inv['questions'])>16 or sum(len(u['text']) for u in inv['units'])>8000:
        issues.append({'code':'ANSWER_TOO_LARGE','unit_id':'','detail':'答案/问题超过校验预算，需压缩而非跳过断言'})
    for unit in inv['units']:
        ids=unit['citations'];inline=display_citations(unit['text'])
        if not ids or any(cid not in known for cid in ids+inline) or not set(inline)<=set(ids):
            issues.append({'code':'INVALID_CITATION','unit_id':unit['id'],'detail':'缺少、未知或未声明的引用'})
            continue
        supported='\n'.join(str(known[c].get(k,'')) for c in ids for k in ('excerpt','title','updated_at'))
        # Inventory already removes display markers outside code. Code literals
        # must remain available to exact-value checks, including [K999] variables.
        for value in anchors(unit['text']):
            present=value.casefold() in supported.casefold()
            if re.fullmatch(r'\d+(?:\.\d+)?',value):
                present=bool(re.search(r'(?<![A-Za-z0-9_.])'+re.escape(value)+r'(?![A-Za-z0-9_.])',supported))
            measure=re.fullmatch(r'(\d+(?:\.\d+)?)\s*(%|毫秒|秒|分钟|小时|天|次|轮|维|ms|s)',value)
            if measure:
                # Normalize only number/unit spacing, never SQL/code whitespace,
                # decimal digits, sign, units, or source witness text.
                present=bool(re.search(r'(?<![A-Za-z0-9_.-])'+re.escape(measure[1])+r'\s*'+re.escape(measure[2])+r'(?![A-Za-z])',supported))
            if not present:
                issues.append({'code':'EXACT_VALUE_UNSUPPORTED','unit_id':unit['id'],'detail':'引用未含精确实体/日期/代码/数值：'+value[:150]})
    return issues


def judge_units(review,inv,sources):
    known={s['citation']:s for s in sources};units={u['id']:u for u in inv['units']};issues=[]
    ids=[u.unit_id for u in review.units]
    if len(ids)!=len(set(ids)) or set(ids)!=set(units):
        issues.append({'code':'REVIEW_COVERAGE_INVALID','unit_id':'','detail':'评审遗漏/重复/虚构断言'})
    for check in review.units:
        unit=units.get(check.unit_id)
        if not unit:continue
        valid=bool(check.supports)
        for support in check.supports:
            source=known.get(support.citation)
            if not source or support.citation not in unit['citations'] or support.quote not in str(source.get(support.field,'')):
                valid=False
        limitation=check.verdict=='limitation' and bool(LIMITATION.search(unit['text']) and SCOPE.search(unit['text']))
        if check.verdict not in ('supported','limitation') or check.accuracy=='inconsistent':
            issues.append({'code':'SEMANTIC_UNSUPPORTED','unit_id':check.unit_id,'detail':check.reason})
        elif not limitation and (not valid or check.accuracy!='consistent'):
            issues.append({'code':'SUPPORT_WITNESS_INVALID','unit_id':check.unit_id,'detail':'支持片段不可回查或资料一致性未确认'})
        elif check.verdict=='limitation' and not limitation:
            issues.append({'code':'LIMITATION_INVALID','unit_id':check.unit_id,'detail':'未明确限定给定资料的缺口'})
    return issues


def judge_coverage(review,inv):
    questions={q['id'] for q in inv['questions']};units={u['id']:u for u in inv['units']};issues=[]
    ids={c.question_id for c in review.coverage}
    if ids!=questions:
        issues.append({'code':'QUESTION_COVERAGE_INVALID','unit_id':'','detail':'问题覆盖项遗漏或虚构'})
    for c in review.coverage:
        selected=[units[uid] for uid in c.unit_ids if uid in units]
        if c.status=='missing' or not selected or len(selected)!=len(c.unit_ids):
            issues.append({'code':'QUESTION_UNANSWERED','unit_id':'','detail':c.requirement+'：'+c.reason})
        elif c.status=='insufficient_disclosed' and not any(LIMITATION.search(u['text']) and SCOPE.search(u['text']) for u in selected):
            issues.append({'code':'GAP_NOT_DISCLOSED','unit_id':'','detail':c.requirement+'：资料不足未在答案中说明'})
    return issues


def validate_review(review,inv,sources):
    issues=judge_units(review,inv,sources)+judge_coverage(review,inv)
    # Conflicting sources can be answered only with an explicit conflict/uncertainty disclosure.
    if review.source_conflicts and not any(re.search(r'冲突|矛盾|不一致|conflict',u['text'],re.I) for u in inv['units']):
        issues.append({'code':'SOURCE_CONFLICT_UNDISCLOSED','unit_id':'','detail':'资料冲突未在答案中披露'})
    return issues
