"""Explicit chunker registry; web CRUD selects data, never executable plugin code."""
import re
from dataclasses import dataclass
from typing import Protocol
from .models import digest
from .text import clean,metadata


@dataclass(frozen=True)
class Chunk:
    id:str
    parent_id:str
    text:str
    parent_text:str
    heading_path:tuple[str,...]
    ordinal:int


class Chunker(Protocol):
    identity:str
    def split(self,text:str)->list[Chunk]:...


def sections(text):
    path=[];lines=[];fenced=False
    for line in clean(text).splitlines():
        heading=re.match(r'^(#{1,6})\s+(.+)$',line) if not fenced else None
        if heading:
            if lines:yield tuple(path),'\n'.join(lines).strip();lines=[]
            level=len(heading[1]);path=path[:level-1]+[heading[2].strip()]
        else:lines.append(line)
        if line.lstrip().startswith(('```','~~~')):fenced=not fenced
    if lines:yield tuple(path),'\n'.join(lines).strip()


def paragraphs(text):
    lines=[];fenced=False
    for line in text.splitlines():
        if not line.strip() and not fenced:
            if lines:yield '\n'.join(lines);lines=[]
        else:lines.append(line)
        if line.lstrip().startswith(('```','~~~')):fenced=not fenced
    if lines:yield '\n'.join(lines)


def sentence_units(paragraph,size):
    # Preserve an intact code block when it fits; oversized code is split at lines first.
    code=paragraph.lstrip().startswith(('```','~~~'))
    units=paragraph.splitlines(keepends=True) if code else re.split(r'(?<=[。！？!?])|(?<=\.)\s+(?=[A-Z])',paragraph)
    for unit in units:
        for offset in range(0,len(unit),size):
            part=unit[offset:offset+size].rstrip('\r\n') if code else unit[offset:offset+size].strip()
            if part:yield part


def parent_groups(paragraph,size):
    current=[];length=0
    for unit in sentence_units(paragraph,size):
        if current and length+len(unit)+1>size:
            yield '\n'.join(current);current=[];length=0
        current.append(unit);length+=len(unit)+1
    if current:yield '\n'.join(current)


class HierarchicalChunker:
    def __init__(self,size=600,overlap=80,parent_size=2400):
        if not 200<=size<=1200 or not 0<=overlap<size//2 or not size<=parent_size<=6000:
            raise ValueError('invalid chunk limits')
        self.size,self.overlap,self.parent_size=size,overlap,parent_size
        self.identity=f'hierarchical-v1:{size}:{overlap}:{parent_size}'

    def split(self,text):
        out=[]
        for path,section in sections(text):
            for paragraph in paragraphs(section):
                for parent in parent_groups(paragraph,self.parent_size):
                    pid=digest('\n'.join(path)+'\n'+parent)
                    current=[];length=0
                    for unit in sentence_units(parent,self.size):
                        if length+len(unit)+1>self.size and current:
                            self.append(out,path,pid,parent,current)
                            tail=current[-1] if len(current[-1])<=self.overlap else ''
                            current=[tail] if tail else [];length=len(tail)
                            if length+len(unit)+1>self.size:current=[];length=0
                        current.append(unit);length+=len(unit)+1
                    if current:self.append(out,path,pid,parent,current)
        if len(out)>256:raise ValueError('文档分块超过256块，请分成多个知识条目')
        return out

    def append(self,out,path,pid,parent,units):
        text='\n'.join(units);cid=digest(pid+':'+str(len(out))+':'+text)
        out.append(Chunk(cid,pid,text,parent,path,len(out)))


CHUNKERS={'hierarchical':HierarchicalChunker}


def register(name,factory):
    if name in CHUNKERS:raise ValueError('chunker already registered')
    CHUNKERS[name]=factory


def build(name='hierarchical',**options):
    if name not in CHUNKERS:raise ValueError('unknown chunker plugin')
    return CHUNKERS[name](**options)


def records(document,chunker):
    out=[]
    for chunk in chunker.split(document['content']):
        prefix='\n'.join([document['title'],*chunk.heading_path])
        context=prefix[:120]+'\n'+chunk.text
        meta={**metadata(chunk.parent_text),'document_id':document['id'],'document_version':document['version'],
              'parent_id':chunk.parent_id,'heading_path':list(chunk.heading_path),'chunker':chunker.identity,
              'title':document['title'],'category':document['category'],'tags':document['tags'],
              'uploader':document['uploader'],'created_at':str(document['created_at']),
              'updated_at':str(document['updated_at']),'observed_id':document['observed_id'],
              'content_hash':digest(context)}
        meta['embedding_prefix_truncated']=len(prefix)>120
        out.append(dict(id=chunk.id,parent_id=chunk.parent_id,text=chunk.text,parent_text=chunk.parent_text,
                        ordinal=chunk.ordinal,embedding_text=context,metadata=meta))
    return out
