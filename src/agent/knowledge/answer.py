from .quality_models import Section,KnowledgeAnswer
from .quality_flow import verified,rows


async def write(gateway,budget,query,sources,emit=None,usage=None,context_truncated=False):
    answer,quality=await verified(gateway,budget,query,sources,emit,usage,context_truncated)
    if answer:
        sections=[{'label':'回答 '+str(n+1),'value':section.text+' '+' '.join('['+cid+']' for cid in section.citations)}
                  for n,section in enumerate(answer.sections)]
        summary=answer.summary+' '+' '.join('['+cid+']' for cid in answer.summary_citations);source='knowledge_llm'
    else:
        summary='找到了相关的生效知识，但综合答案质量校验'+('未通过' if quality['status']=='failed' else '未完成')+'；以下仅展示原文摘录与出处。'
        sections=[{'label':'知识摘录 '+s['citation'],'value':s['excerpt']+' ['+s['citation']+']'} for s in sources]
        source='knowledge_quotes'
    return {'source':source,'summary':summary+' 这些是知识参考，不能据此确认当前系统的故障原因。',
            'details':sections+rows(quality),'details_kind':'table','quality':quality,
            'note':'综合答案须通过断言支持与问题覆盖校验；准确性仅指与引用资料一致'}


def references(sources):
    rows=[]
    for s in sources:
        title=s['title'].replace('[','').replace(']','').replace('\n',' ')
        rows.append({'label':'来源 '+s['citation'],'value':
            f"[📚 {title} · v{s['version']}]({s['url']})；分类 {s['category']}；上传者 {s['uploader']}；更新时间 {s['updated_at']}；"
            +'实体 '+', '.join(s['metadata'].get('entities',[])[:12])+'；时间 '+', '.join(s['metadata'].get('times',[])[:8])+
            ('；摘录已按预算截断' if s['truncated'] else '')})
    return rows
