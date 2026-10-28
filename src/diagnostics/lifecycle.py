"""Independent, read-only lifecycle observation; never automatically restart."""
import json
import os
from pathlib import Path
import re
import subprocess
import time
from .contract import event


class Journal:
    def __init__(self,path,max_bytes=2*1024*1024):
        self.path=Path(path);self.max_bytes=max_bytes;self.dropped=0
        self.path.parent.mkdir(parents=True,exist_ok=True)

    def write(self,row):
        data=json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n'
        if len(data.encode())>65536:self.dropped+=1;return
        if self.path.exists() and self.path.stat().st_size+len(data.encode())>self.max_bytes:
            previous=self.path.with_suffix(self.path.suffix+'.1')
            if previous.exists():previous.unlink()
            self.path.replace(previous)
        with self.path.open('a',encoding='utf-8',newline='\n') as f:f.write(data)


class LifecycleObserver:
    def __init__(self,identity,sink):
        self.identity=dict(identity);self.sink=sink
        self.errors=0

    def record(self,kind,payload):
        row=event(self.identity,kind,'process_observer',payload)
        try:self.sink(row)
        except Exception:self.errors+=1
        return row

    def run(self,command,cwd=None,env=None):
        from uuid import uuid4
        self.identity['boot_id']=uuid4().hex
        self.record('observer_start',{'historical_coverage':'unknown','previous_offline_interval':'not_observed'})
        child_env=dict(os.environ if env is None else env)
        child_env.update(AIOPS_SDK_BOOT_ID=self.identity['boot_id'],
                         AIOPS_SDK_INSTANCE=self.identity['instance'])
        try:process=subprocess.Popen(command,cwd=cwd,env=child_env)
        except OSError as exc:
            self.record('process_start_failed',{'exception_type':type(exc).__name__})
            raise
        started=time.time()
        self.record('process_start',{'pid':process.pid,'observer_started_at':started,
            'version':child_env.get('AIOPS_APPLICATION_VERSION','unknown')})
        while True:
            try:
                code=process.wait(timeout=5);break
            except subprocess.TimeoutExpired:
                self.record('process_heartbeat',{'pid':process.pid,'observed_alive':True,
                    'observer_started_at':started,'historical_coverage':'snapshot_only'})
        self.record('process_exit',dict(pid=process.pid,exit_code=code,
            signal=-code if os.name!='nt' and code<0 else None,
            termination_reason='normal_exit' if code==0 else 'unknown',restart_performed=False))
        return code


def container_state(container):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',container):raise ValueError('invalid configured container')
    result=subprocess.run(['docker','inspect','--format','{{json .State}}',container],
                          capture_output=True,timeout=5,check=True)
    if len(result.stdout)>65536:raise ValueError('container state too large')
    return json.loads(result.stdout)


def container_event(identity,state):
    exited=state.get('Status')=='exited'
    payload=dict(exit_code=state.get('ExitCode'),pid=state.get('Pid'),
        termination_reason='oom' if state.get('OOMKilled') is True else
            'normal_exit' if exited and state.get('ExitCode')==0 else 'unknown',
        platform_oom_confirmed=state.get('OOMKilled') is True,
        started_at=state.get('StartedAt'),finished_at=state.get('FinishedAt'))
    return event(identity,'process_exit' if exited else 'process_state','platform_event',payload)
