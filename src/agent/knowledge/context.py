"""Extractive parent/child summaries preserve metadata and exact code strings."""
import json
from ..summary.agent import deterministic


@deterministic('knowledge_context','knowledge-extractive-v1')
def context(recalled,hits,query,limit=16_000):
    docs={(d['document_id'],d['version']):d for d in recalled['documents']}
    by_parent={}
    for hit in hits:by_parent.setdefault((hit['document_id'],hit['version'],hit['parent_id']),[]).append(hit['text'])
    sources=[];used=0
    for parent in recalled['parents']:
        key=(parent['document_id'],parent['version']);doc=docs[key]
        matched=by_parent.get((*key,parent['parent_id']),[])
        # Matched children go first so clipping never keeps only an irrelevant document lead.
        excerpts=list(dict.fromkeys(matched+[parent['text']]))
        remaining=limit-used
        if remaining<300:break
        meta=parent['metadata']
        summarized={k:meta.get(k) for k in ('document_id','document_version','parent_id','observed_id','content_hash')}
        for name,count in (('entities',12),('times',8),('code_terms',8),('heading_path',4)):
            summarized[name]=[str(v)[:160] for v in (meta.get(name) or [])[:count]]
        summarized['metadata_truncated']=any(len(meta.get(k) or [])>count for k,count in (('entities',12),('times',8),('code_terms',8),('heading_path',4)))
        # Parent already contains most matched children. Retain only children
        # that are not present verbatim, then the entire original parent.
        text='\n'.join([child for child in dict.fromkeys(matched) if child not in parent['text']]+[parent['text']])
        source={'document_id':key[0],'version':key[1],'parent_id':parent['parent_id'],
                'title':doc['title'],'uploader':doc['uploader'],'category':doc['category'],
                'updated_at':doc['updated_at'],'metadata':summarized,'excerpt':'',
                'truncated':False,
                'url':f"/v1/knowledge/{key[0]}/versions/{key[1]}"}
        source['citation']='K'+str(len(sources)+1)
        overhead=len(json.dumps(source,ensure_ascii=False))+4
        if remaining-overhead<100:break
        capacity=min(3200,(remaining-overhead)//2)
        if len(text)>capacity and any(child not in text[:capacity] for child in matched):
            # If the parent must be clipped, keep the actual hit before its
            # parent prefix. Deduplication must not lose a late matching child.
            text='\n'.join(dict.fromkeys(matched+[parent['text']]))
        kept=text[:capacity]
        source.update(excerpt=kept,truncated=len(kept)<len(text))
        size=len(json.dumps(source,ensure_ascii=False))+2
        if size>remaining:break
        sources.append(source);used+=size
    return {'sources':sources,'characters':used,'budget':limit,'truncated':len(sources)<len(recalled['parents']) or any(s['truncated'] for s in sources)}
