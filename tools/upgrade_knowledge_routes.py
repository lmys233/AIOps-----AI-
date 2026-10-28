"""Explicit, CAS-managed description/few-shot upgrade; retain user customizations."""
import asyncio
import json
import os
import sys
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from agent.common.envfile import load_env_file
from agent.intent.catalog_models import defaults, normalized


async def main():
    load_env_file()
    token=os.getenv('AIOPS_AGENT_TOKEN','')
    headers={'Authorization':'Bearer '+token} if token else {}
    report={'updated':[],'preserved':[]}
    originals={'KNOWLEDGE_QA':'抽象概念、通用流程；处理能力仍遵守当前知识能力边界',
               'OUT_OF_SCOPE':'非运维请求或没有足够线索'}
    builtin={r['handler']:r for r in defaults() if r['handler'] in originals}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8020',headers=headers,timeout=30) as client:
        response=await client.get('/v1/intents');response.raise_for_status();catalog=response.json()
        for old in catalog['routes']:
            if old['handler'] not in originals:
                continue
            if not old['enabled'] or old['description'] not in (originals[old['handler']],builtin[old['handler']]['description']):
                report['preserved'].append({'id':old['id'],'reason':'disabled or customized description'})
                continue
            new={**old,'description':builtin[old['handler']]['description']}
            new['examples']=list(dict.fromkeys([normalized(t) for t in old['examples']+builtin[old['handler']]['examples']]))[:24]
            if new==old:
                report['preserved'].append({'id':old['id'],'reason':'already upgraded'})
                continue
            response=await client.put('/v1/intents/'+old['id'],json={'revision':catalog['revision'],'route':new})
            response.raise_for_status();catalog=response.json()
            report['updated'].append({'id':old['id'],'before':old,'after':new,'catalog_revision':catalog['revision']})
            (ROOT/'docs/intent-knowledge-route-upgrade.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        report['final_revision']=catalog['revision']
        (ROOT/'docs/intent-knowledge-route-upgrade.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'updated':len(report['updated']),'preserved':len(report['preserved'])}))


if __name__=='__main__':asyncio.run(main())
