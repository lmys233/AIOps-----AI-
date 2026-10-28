"""Lossless source catalog; semantic reviewer chooses exact, version-bound witnesses."""
import re
from .quality_models import Review

VERSION='witness-v1'


def prepare(sources):
    rows=[];index={}
    for source in sources:
        row={k:source[k] for k in ('citation','document_id','version','title','updated_at','truncated') if k in source}
        metadata=source.get('metadata') or {}
        row['metadata']={k:metadata[k] for k in ('entities','times','code_terms','heading_path') if k in metadata}
        row['witnesses']=[]
        for field in ('excerpt','title','updated_at'):
            value=str(source.get(field,''))
            # Retain all characters, including original whitespace, in source order.
            pieces=re.findall(r'[^。！？\n]*[。！？\n]|[^。！？\n]+$',value)
            offset=0
            for part in pieces:
                for start in range(0,len(part),240):
                    text=part[start:start+240];wid='W'+str(len(index)+1)
                    if len(text)>=2 and text.strip():
                        index[wid]={'citation':source['citation'],'field':field,'quote':text,
                            'document_id':source.get('document_id'),'version':source.get('version'),
                            'start':offset+start,'end':offset+start+len(text)}
                    # Even a 1-character tail is preserved in the input; it cannot
                    # independently support a verdict (old Support requires >=2).
                    row['witnesses'].append({'id':wid if wid in index else '', 'field':field,'text':text})
                offset+=len(part)
        rows.append(row)
    return rows,index


def expand(result,index):
    if isinstance(result,Review):return result # explicit injected v1 gateway compatibility
    data=result.model_dump(mode='json')
    for unit in data['units']:
        supports=[]
        for wid in unit['supports']:
            if wid not in index:raise ValueError('REVIEW_WITNESS_UNKNOWN')
            supports.append({k:index[wid][k] for k in ('citation','field','quote')})
        unit['supports']=supports
    return Review.model_validate(data)
