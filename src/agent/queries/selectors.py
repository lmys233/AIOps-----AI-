"""Restricted selector rewriting. Every selector is scoped before backend access."""
import json,re

MATCHER=re.compile(r'\s*([a-zA-Z_][\w.]*)\s*(=~|!~|!=|=)\s*("(?:\\.|[^"\\])*")\s*')
WORDS={'and','or','unless','bool','offset','atan2'}
GROUPS={'by','without','on','ignoring','group_left','group_right'}
AGGREGATES={'sum','avg','min','max','count','stddev','stdvar','topk','bottomk','count_values','quantile','group'}


def matchers(text):
    text=text.strip();out=[];at=0
    while at<len(text):
        m=MATCHER.match(text,at)
        if not m:raise ValueError('unsupported or malformed label selector')
        out.append((m[1],m[2],json.loads(m[3])));at=m.end()
        if at<len(text):
            if text[at]!=',':raise ValueError('invalid selector separator')
            at+=1
    return out


def scoped_selector(text,scope,names):
    entries=matchers(text);picked=set(names);rest=[]
    expected={'observed_id':scope.observed_id,'tenant':scope.tenant,'env':scope.env}
    if scope.cluster:expected['cluster']=scope.cluster
    for key,op,value in entries:
        if key in expected:
            if op!='=' or value!=expected[key]:raise PermissionError('explicit query scope mismatch')
        elif key=='service':
            if op=='=':
                if value not in names:raise PermissionError('unregistered query service')
                picked&={value}
            elif op=='=~':
                if len(value)>256:raise ValueError('service pattern too large')
                if re.fullmatch(r'[\w.:-]+',value) and value not in names:raise PermissionError('unregistered query service')
                try:picked&={n for n in names if re.fullmatch(value,n)}
                except re.error:raise ValueError('invalid service pattern') from None
            else:raise ValueError('negative service selectors are unsupported')
        else:rest.append(key+op+json.dumps(value,ensure_ascii=False))
    if not picked:raise PermissionError('query matches no registered services')
    rest.extend(k+'='+json.dumps(v) for k,v in expected.items())
    if len(picked)==1:rest.append('service='+json.dumps(next(iter(picked))))
    else:rest.append('service=~'+json.dumps('|'.join(re.escape(n) for n in sorted(picked))))
    return '{'+','.join(rest)+'}'


def quoted_end(text,at):
    end=at+1
    while end<len(text):
        if text[end]=='\\':end+=2;continue
        if text[end]==text[at]:return end+1
        end+=1
    raise ValueError('unterminated quote')


def brace_end(text,at):
    end=at+1
    while end<len(text):
        if text[end]=='"':end=quoted_end(text,end);continue
        if text[end]=='}':return end+1
        if text[end]=='{':raise ValueError('nested selector')
        end+=1
    raise ValueError('unterminated selector')


def group_end(text,at):
    end=at;depth=0
    while end<len(text):
        if text[end]=='(':depth+=1
        elif text[end]==')':
            depth-=1
            if not depth:return end+1
        end+=1
    raise ValueError('unterminated grouping')


def range_end(text,at):
    """Range/subquery durations are syntax, never metric selectors (e.g. :30s)."""
    end=text.find(']',at+1)
    duration=r'(?:\d+(?:ms|s|m|h|d|w|y))+'
    if end<0 or not re.fullmatch(r'\s*'+duration+r'\s*(?::\s*(?:'+duration+r')?\s*)?',text[at+1:end]):
        raise ValueError('unsupported range or subquery duration')
    return end+1


def rewrite(query,scope,names,*,logs=False):
    if not isinstance(query,str) or not query.strip() or len(query)>12000:raise ValueError('query size invalid')
    if re.search(r'\b(label_replace|label_join|label_format|line_format)\b',query):raise ValueError('label rewriting is unsupported in scoped queries')
    out=[];at=0;selectors=0
    while at<len(query):
        char=query[at]
        if char=='#':raise ValueError('query comments are unsupported in scoped expressions')
        if char in ('"',"'",'`'):
            end=quoted_end(query,at);out.append(query[at:end]);at=end;continue
        if char=='{':
            end=brace_end(query,at);out.append(scoped_selector(query[at+1:end-1],scope,names));at=end;selectors+=1;continue
        if char=='[' and not logs:
            end=range_end(query,at);out.append(query[at:end]);at=end;continue
        number=re.match(r'\d+(?:\.\d+)?(?:[a-zA-Z]+)?',query[at:])
        if number:out.append(number[0]);at+=len(number[0]);continue
        word=re.match(r'[a-zA-Z_:][a-zA-Z0-9_:]*',query[at:])
        if word and not logs:
            name=word[0];end=at+len(name);follow=end
            while follow<len(query) and query[follow].isspace():follow+=1
            if name in GROUPS and follow<len(query) and query[follow]=='(':
                end=group_end(query,follow);out.append(query[at:end]);at=end;continue
            if name not in WORDS|AGGREGATES and (follow==len(query) or query[follow] not in '({'):
                out.append(name+scoped_selector('',scope,names));selectors+=1
            else:out.append(name)
            at=end;continue
        out.append(char);at+=1
    if not selectors:raise ValueError('query must read scoped observations')
    return ''.join(out)
