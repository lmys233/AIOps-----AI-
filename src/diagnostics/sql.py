"""Bounded SQL shape only: values and comments never become telemetry labels."""
import hashlib
import re


def sql_shape(query):
    if len(str(query))>20000:return dict(query_shape='<oversize>',query_fingerprint='oversize')
    text=strip_literals(str(query))
    text=re.sub(r'\b\d+(?:\.\d+)?\b','?',text)
    text=' '.join(text.split())[:2000]
    return dict(query_shape=text,query_fingerprint=hashlib.sha256(text.encode()).hexdigest()[:24])


def strip_literals(text):
    out=[];i=0
    while i<len(text):
        if text.startswith('--',i):
            end=text.find('\n',i);i=len(text) if end<0 else end;out.append(' ');continue
        if text.startswith('/*',i):
            i=comment_end(text,i)
            if i<0:return '<unparsed>'
            out.append(' ');continue
        if text[i]=="'":
            match=re.match(r"'(?:''|\\.|[^'\\])*'",text[i:],re.S)
            if not match:return '<unparsed>'
            i+=len(match[0]);out.append('?');continue
        if text[i]=='$':
            tag=re.match(r'\$(?:[A-Za-z_][\w]*)?\$',text[i:])
            if tag:
                end=text.find(tag[0],i+len(tag[0]))
                if end<0:return '<unparsed>'
                i=end+len(tag[0]);out.append('?');continue
        if text[i]=='"':
            match=re.match(r'"(?:""|[^"])*"',text[i:])
            if not match:return '<unparsed>'
            out.append(match[0]);i+=len(match[0]);continue
        out.append(text[i]);i+=1
    return ''.join(out)


def comment_end(text,start):
    depth=1;i=start+2
    while i<len(text):
        if text.startswith('/*',i):depth+=1;i+=2
        elif text.startswith('*/',i):
            depth-=1;i+=2
            if not depth:return i
        else:i+=1
    return -1
