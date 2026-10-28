"""Local operator launcher. No credentials in process arguments or logs."""
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from agent.common.envfile import load_env_file
from sdk.config import load_config


def main():
    load_env_file()
    # This isolated deployment has its own identity, independent of the
    # existing production example's .env overrides.
    os.environ['AIOPS_ENV']='dev'
    os.environ.pop('AIOPS_CLUSTER',None)
    os.environ['DIAGNOSTIC_LAB_DSN']=os.environ['AIOPS_DATABASE_URL']
    os.environ['AIOPS_SDK_DIAGNOSTICS_URL']='http://127.0.0.1:8020/v1/observations/batches'
    os.environ['AIOPS_SDK_DIAGNOSTICS_TOKEN']=os.environ['AIOPS_DIAGNOSTIC_LAB_TOKEN']
    os.environ['AIOPS_SDK_SUCCESS_SAMPLE_EVERY']='1'
    if os.getenv('AIOPS_LAB_REGISTRATION_TOKEN'):
        os.environ['AIOPS_OBSERVED_NAME']='diagnostic-example'
        os.environ['AIOPS_SDK_REGISTRATION_TOKEN']=os.environ['AIOPS_LAB_REGISTRATION_TOKEN']
        os.environ.setdefault('AIOPS_SDK_REGISTRATION_URL','http://127.0.0.1:8020/v1/observed/register')
        os.environ['AIOPS_SDK_ADVERTISE_URL']='http://127.0.0.1:8084'
    os.environ['AIOPS_SDK_DB_IDENTITY_READ']='on'
    os.environ['AIOPS_SDK_DIAGNOSTICS_BUFFER']=str(ROOT/'.run/diagnostic-lab/outbox-dev')
    from sdk.launcher import run
    return run(str(ROOT/'docs/diagnostic-lab.yaml'),service='diagnostic-lab')


if __name__=='__main__':raise SystemExit(main())
