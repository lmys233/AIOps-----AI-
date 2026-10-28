"""Optional trusted deployment profile; standard library only, no Agent dependency."""
import json
import os
from pathlib import Path
import re
from .config import ConfigError

NAME=re.compile(r'^[A-Z][A-Z0-9_]{0,100}$')
SENSITIVE=('PASSWORD','SECRET','TOKEN','DSN','DATABASE_URL','API_KEY','CREDENTIAL')


def env_file(path,base):
    """Read literal KEY=value only; existing process environment wins."""
    if path.stat().st_size>65536:raise ConfigError('部署环境文件超过大小限制')
    result=dict(base)
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        if '=' not in line:raise ConfigError('部署环境文件必须使用 KEY=value')
        key,value=line.split('=',1);key=key.strip();value=value.strip()
        if not NAME.fullmatch(key):raise ConfigError('部署环境变量名称无效')
        if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"):value=value[1:-1]
        result.setdefault(key,value)
    return result


def merge(section,env,lookup=None):
    if not isinstance(section,dict) or set(section)-{'environment','env_refs'}:
        raise ConfigError('部署服务段仅支持 environment/env_refs')
    values=section.get('environment',{});refs=section.get('env_refs',{})
    if not isinstance(values,dict) or not isinstance(refs,dict):raise ConfigError('部署环境映射必须为对象')
    result=dict(env)
    lookup=env if lookup is None else lookup
    for key,value in values.items():
        if not isinstance(key,str) or not NAME.fullmatch(key) or not isinstance(value,str):raise ConfigError('部署环境映射无效')
        if any(s in key for s in SENSITIVE):raise ConfigError('凭据必须用 env_refs 引用环境变量')
        result[key]=value
    for key,reference in refs.items():
        if not isinstance(key,str) or not NAME.fullmatch(key) or not isinstance(reference,str) or not NAME.fullmatch(reference):
            raise ConfigError('部署环境引用无效')
        if not lookup.get(reference):raise ConfigError('部署引用的环境变量未配置：'+reference)
        result[key]=lookup[reference]
    return result


def deployment_env(path,service,base=None):
    path=Path(path);env=dict(os.environ if base is None else base)
    if path.stat().st_size>65536:raise ConfigError('部署配置超过大小限制')
    try:data=json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError,UnicodeError) as exc:raise ConfigError('部署配置必须为 JSON') from exc
    if not isinstance(data,dict) or type(data.get('version')) is not int or data.get('version')!=1 or set(data)-{'version','env_file','defaults','services'}:
        raise ConfigError('部署配置版本或字段无效')
    services=data.get('services',{})
    if not isinstance(services,dict) or service not in services:raise ConfigError('部署配置未声明目标服务')
    lookup=env
    if data.get('env_file'):
        if not isinstance(data['env_file'],str):raise ConfigError('部署环境文件路径无效')
        # Relative paths follow the profile, never the operator's shell cwd.
        lookup=env_file(path.parent/data['env_file'],env)
    # The file is a reference source, not a wholesale import of platform secrets.
    env=merge(data.get('defaults',{}),env,lookup)
    return merge(services[service],env,dict(lookup,**env))


def apply_profile(path,service):
    try:env=deployment_env(path,service)
    except OSError as exc:raise ConfigError('部署配置或环境文件不可读') from exc
    os.environ.update(env)
