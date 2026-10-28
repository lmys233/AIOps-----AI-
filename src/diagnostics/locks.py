"""Opaque PostgreSQL advisory lock keys, never original business identifiers."""
import hashlib
import re


def key_hash(classid,objid,objsubid):
    return hashlib.sha256(f'{classid}:{objid}:{objsubid}'.encode()).hexdigest()[:24]


def advisory_refs(query,args):
    match=re.search(r'\bpg_advisory_xact_lock\s*\(([^()]*)\)',str(query),re.I)
    if not match:return []
    values=[]
    for expression in match[1].split(','):
        expression=expression.strip()
        if re.fullmatch(r'\$\d+',expression):
            index=int(expression[1:])-1
            value=args[index] if 0<=index<len(args) else None
        elif re.fullmatch(r'-?\d+',expression):value=int(expression)
        else:return []
        if type(value) is not int:return []
        values.append(value)
    if len(values)==1:
        value=values[0]%(1<<64)
        return [key_hash(value>>32,value&0xffffffff,1)]
    if len(values)==2:return [key_hash(values[0]&0xffffffff,values[1]&0xffffffff,2)]
    return []


def wait_cycle(detail):
    edges={int(a):int(b) for a,b in re.findall(r'Process (\d+) waits[^;\n]*; blocked by process (\d+)',detail,re.I)}
    for start in edges:
        visited=set();node=start
        while node in edges and node not in visited:
            visited.add(node);node=edges[node]
        if node==start and len(visited)>1:return visited
    return set()
