"""Operator migration for this repository's deployments; secrets only in ignored env files."""
import asyncio,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from agent.common.envfile import load_env_file
from agent.observed.store import RegistryStore
from agent.observed.credentials import Credentials
from agent.observed.enrollment import issue


def env_put(path,values):
    original=path.read_text(encoding='utf-8-sig');lines=original.splitlines()
    keys=set(values);lines=[line for line in lines if line.split('=',1)[0].strip() not in keys]
    path.write_text('\n'.join(lines)+'\n'+''.join(k+'='+v+'\n' for k,v in values.items()),encoding='utf-8')


async def enroll(store,creds,name,services,origins,env):
    existing=await store.one('SELECT observed_id FROM agent_observed_systems WHERE tenant=$1 AND name=$2','default',name)
    args=dict(tenant='default',observed_name=name,services=services,origins=origins,envs=[env])
    if existing:args['observed_id']=existing['observed_id']
    return await issue(store,creds,args)


async def main():
    load_env_file();folder=ROOT/'.run/registered-migration';folder.mkdir(exist_ok=True)
    for path in (ROOT/'.env',ROOT/'.run/observed-sdk-deployment.json',ROOT/'deploy/prometheus/prometheus.yml'):
        target=folder/(path.name+'.before')
        if path.exists() and not target.exists():target.write_bytes(path.read_bytes())
    store=RegistryStore(os.environ['AIOPS_DATABASE_URL']);creds=Credentials()
    try:
        names=['gateway','order','payment','inventory'];origins=['http://127.0.0.1:'+str(8080+n) for n in range(4)]
        commerce=await enroll(store,creds,'commerce-demo',names,origins,'prod')
        lab=await enroll(store,creds,'diagnostic-example',['diagnostic-lab'],['http://127.0.0.1:8084'],'dev')
        env_put(ROOT/'.env',{'AIOPS_COMMERCE_REGISTRATION_TOKEN':commerce['registration_token'],
            'AIOPS_LAB_REGISTRATION_TOKEN':lab['registration_token'],'AIOPS_PROMETHEUS_HOST_GATEWAY':'host.docker.internal'})
        path=ROOT/'.run/observed-sdk-deployment.json';profile=json.loads(path.read_text(encoding='utf-8'))
        defaults=profile['defaults']['environment'];defaults.update(AIOPS_OBSERVED_NAME='commerce-demo',
            AIOPS_SDK_REGISTRATION_URL='http://127.0.0.1:8020/v1/observed/register')
        for n,name in enumerate(names):
            section=profile['services'][name];section.setdefault('environment',{})['AIOPS_SDK_ADVERTISE_URL']=origins[n]
            section.setdefault('env_refs',{})['AIOPS_SDK_REGISTRATION_TOKEN']='AIOPS_COMMERCE_REGISTRATION_TOKEN'
        path.write_text(json.dumps(profile,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        report={'systems':[{'name':'commerce-demo','observed_id':commerce['observed_id'],'services':names},
            {'name':'diagnostic-example','observed_id':lab['observed_id'],'services':['diagnostic-lab']}],
            'credential_locations':'ignored .env; deployment profile stores env refs only',
            'legacy_history':'schema 1 retained immutable; no implicit reassignment by service name'}
        (folder/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))
    finally:await store.close()


if __name__=='__main__':asyncio.run(main())
