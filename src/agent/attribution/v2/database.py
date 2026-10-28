"""命名 PostgreSQL 目录检查；所有语句固定，事务只读，不执行模型 SQL。"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from ...tools.sources import SourceUnavailable, QueryRejected
from .verification import Observation
from .database_config import availability, resolve_paths

IDENTIFIER=re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}(?:\.[A-Za-z_][A-Za-z0-9_]{0,62})?$')
CATALOG_SQL="""SELECT n.nspname AS schema, c.relname AS name
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE c.relname=$1 AND ($2::text IS NULL OR n.nspname=$2)
AND c.relkind IN ('r','p','v','m','f') ORDER BY n.nspname LIMIT 50"""
IDENTITY_SQL="""SELECT current_database() AS database, current_user AS role,
current_setting('transaction_read_only') AS read_only,
current_setting('search_path') AS search_path,
COALESCE(inet_server_addr()::text,'local') || ':' || inet_server_port()::text AS instance_id"""


def read_json(path):
    p=Path(path)
    if p.stat().st_size>512_000:raise ValueError('元数据文件超过上限')
    return json.loads(p.read_text(encoding='utf-8'))


async def inspect_postgres(mapping, object_name):
    import asyncpg
    dsn=os.environ.get(mapping.get('dsn_env',''), '')
    if not dsn:raise SourceUnavailable('未配置该服务的检查连接')
    conn=None
    try:
        conn=await asyncpg.connect(dsn,timeout=5,command_timeout=5)
        async with conn.transaction(readonly=True):
            identity=dict(await conn.fetchrow(IDENTITY_SQL))
            schema,name=(object_name.split('.',1) if '.' in object_name else (None,object_name))
            relations=[dict(r) for r in await conn.fetch(CATALOG_SQL,name,schema)] if name else []
            if identity['read_only']!='on':raise SourceUnavailable('只读事务未生效')
            return identity | {'relations':relations}
    except SourceUnavailable:raise
    except Exception as exc:raise SourceUnavailable('数据库目录检查失败：'+type(exc).__name__) from None
    finally:
        if conn is not None:await conn.close()


class DatabaseReader:
    def __init__(self,mappings=None,inspector=inspect_postgres,clock=None,inspectors=None):
        self.mappings=mappings or {}
        self.inspector=inspector
        self.custom_inspector=inspector is not inspect_postgres
        self.inspectors={'postgres':inspector,**(inspectors or {})}
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    @classmethod
    def from_env(cls):
        path=os.environ.get('AIOPS_DB_INSPECTION_MAP','')
        try:return cls(resolve_paths(read_json(path),Path(path).resolve().parent) if path else {})
        except Exception:raise SourceUnavailable('数据库检查映射不可读') from None

    def availability(self,tool,params):return availability(self,tool,params)

    async def read(self,tool,params):
        name=params.get('object_name','')
        if name and not IDENTIFIER.fullmatch(name):raise QueryRejected('不支持的对象名；不接收 SQL')
        service=params['service']
        mapping=self.mappings.get(service)
        from ...observed.context import current
        scope=current()
        if scope:
            mapping=self.mappings.get(scope.observed_id,{}).get(service)
            if mapping is not None and mapping.get('observed_id')!=scope.observed_id:
                raise SourceUnavailable('数据库检查映射缺少可信系统归属')
        if mapping is None:raise SourceUnavailable('未配置 '+service+' 的只读数据库检查映射')
        if tool in ('migration_read','ddl_audit_read'):
            return self._source(mapping,service,name,tool)
        inspector=self.inspectors.get(mapping.get('driver','postgres'))
        if inspector is None:raise SourceUnavailable('UnsupportedAdapter','未接入对应只读数据库适配器')
        data=await inspector(mapping,name)
        if data.get('read_only')!='on':raise SourceUnavailable('ReadonlyRequired','适配器未确认只读检查')
        return self._catalog(mapping,service,name,data,tool)

    def _identity(self,mapping):
        path=mapping.get('runtime_identity_file')
        if not path:return None
        try:
            data=read_json(path)
            from ...observed.context import current
            if current() and data.get('observed_id')!=current().observed_id:return None
            if data.get('verification_method')!='verified_human':return None
            return data
        except Exception:return None

    def _catalog(self,mapping,service,name,data,tool):
        now=self.clock();runtime=self._identity(mapping)
        scope=bool(runtime and runtime.get('service')==service and
            runtime.get('database')==data['database'] and runtime.get('instance_id')==data['instance_id'])
        common=dict(service=service,object_name=name,env=mapping.get('env',''),
            cluster=mapping.get('cluster',''),time_range=(now,now),complete=True,scope_verified=scope)
        if tool=='db_object_read':
            paths=runtime.get('schemas',[]) if runtime else []
            visible=[r for r in data['relations'] if '.' in name or r['schema'] in paths]
            return [Observation(signal='object_absent',value=not visible if scope else None,
                reason='当前检查连接的目录；缺可靠运行期身份时不能推断应用解析路径',**common)]
        mismatch=bool(scope and (runtime['database']!=mapping.get('expected_database',data['database'])
            or runtime.get('schemas',[])!=mapping.get('expected_schemas',runtime.get('schemas',[]))))
        if scope:
            try:common['time_range']=tuple(datetime.fromisoformat(v) for v in runtime['time_range'])
            except Exception:common['complete']=False
        return [Observation(signal=k,value=(v if scope else None),
            reason='检查连接身份与经人工核实的应用身份分别记录；未核实即缺口',**common)
            for k,v in [('runtime_target_verified',not mismatch),('runtime_target_mismatch',mismatch)]]

    def _source(self,mapping,service,name,tool):
        key='migration_file' if tool=='migration_read' else 'ddl_audit_file'
        path=mapping.get(key)
        if not path:raise SourceUnavailable('未配置 '+key+'，没有记录不等于没有变更')
        try:
            source=read_json(path)
            from ...observed.context import current
            scope=current()
            if scope and source.get('observed_id')!=scope.observed_id:raise ValueError('source system mismatch')
            if source.get('verification_method')!='verified_human':raise ValueError('来源未核实')
            window=tuple(datetime.fromisoformat(v) for v in source['time_range'])
            rows=[r for r in source.get('records',[]) if r.get('service')==service and r.get('object_name')==name]
            if scope and any(r.get('observed_id')!=scope.observed_id for r in rows):raise ValueError('record system mismatch')
            if not rows:raise ValueError('目标记录不足，不能以空清单反证')
            return [Observation(signal='migration_not_applied' if tool=='migration_read' else 'ddl_removed_before_fault',
                service=service,object_name=name,env=mapping.get('env',''),cluster=mapping.get('cluster',''),
                time_range=window,event_time=datetime.fromisoformat(r['event_time']) if r.get('event_time') else None,
                complete=bool(source.get('complete')) and (tool!='migration_read' or
                    all(type(r.get(k)) is bool for k in ('required_by_deployed_version','applied'))),
                scope_verified=bool(r.get('target_verified')),
                value=(r.get('required_by_deployed_version') is True and r.get('applied') is False)
                    if tool=='migration_read' else r.get('operation') in ('drop','rename')) for r in rows]
        except Exception:raise SourceUnavailable('来源记录的作用域/完整性不可验证') from None
