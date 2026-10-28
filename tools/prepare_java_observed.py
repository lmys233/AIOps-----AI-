"""Issue one scoped Java example enrollment through the existing platform API; never print tokens."""
import argparse,asyncio,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import httpx
from agent.common.envfile import load_env_file
from diagnostics.observed import base_url,system_id

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent-url',default='http://127.0.0.1:8020')
    parser.add_argument('--name',default='springboot-example');parser.add_argument('--service',default='java-catalog')
    parser.add_argument('--env',default='dev');parser.add_argument('--tenant',default='default')
    parser.add_argument('--sdk-origin',default='http://127.0.0.1:8185');parser.add_argument('--business-origin',default='http://127.0.0.1:8085')
    parser.add_argument('--output',type=Path,default=ROOT/'.run/java-sdk/enrollment.json')
    args=parser.parse_args();load_env_file();base_url(args.agent_url)
    headers={'Authorization':'Bearer '+os.environ['AIOPS_AGENT_TOKEN']} if os.getenv('AIOPS_AGENT_TOKEN') else {}
    if args.output.exists():
        saved=json.loads(args.output.read_text(encoding='utf-8'));system_id(saved['observed_id'])
        assert (saved['name'],saved['service'],saved['env'],saved['tenant'])==(args.name,args.service,args.env,args.tenant),'Existing enrollment scope differs; choose a new private output path'
        print(json.dumps({'observed_id':saved['observed_id'],'enrollment_file':str(args.output),'reused':True}));return
    async with httpx.AsyncClient(timeout=5,follow_redirects=False) as client:
        response=await client.post(args.agent_url+'/v1/observed-enrollments',headers=headers,json={
            'tenant':args.tenant,'observed_name':args.name,'services':[args.service],
            'origins':[base_url(args.sdk_origin),base_url(args.business_origin)],'envs':[args.env]})
        response.raise_for_status();result=response.json();system_id(result['observed_id'])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({**result,'name':args.name,'service':args.service,'env':args.env,'tenant':args.tenant,
        'agent_url':args.agent_url,'sdk_origin':args.sdk_origin,'business_origin':args.business_origin},indent=2),encoding='utf-8')
    print(json.dumps({'observed_id':result['observed_id'],'enrollment_file':str(args.output),'reused':False}))

if __name__=='__main__':asyncio.run(main())
