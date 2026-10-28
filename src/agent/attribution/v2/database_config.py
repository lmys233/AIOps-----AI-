"""部署映射和本地来源预检；不把平台数据库或服务清单当业务连接。"""
import os
from datetime import datetime
from pathlib import Path
from ...tools.availability import Availability

DATABASE_TOOLS=('db_object_read','db_context_read','migration_read','ddl_audit_read')
SOURCE_KEYS={'db_context_read':'runtime_identity_file','migration_read':'migration_file',
             'ddl_audit_read':'ddl_audit_file'}


def resolve_paths(mappings,base):
    if not isinstance(mappings,dict):raise ValueError('检查映射必须为服务映射')
    out={}
    for service,raw in mappings.items():
        if not isinstance(raw,dict):raise ValueError('服务检查配置必须为映射')
        mapping=dict(raw)
        for key in SOURCE_KEYS.values():
            if mapping.get(key):mapping[key]=str((base/str(mapping[key])).resolve())
        out[service]=mapping
    return out


def availability(reader,tool,params):
    service=params.get('service','');mapping=reader.mappings.get(service)
    from ...observed.context import current
    scope=current()
    if scope:
        mapping=reader.mappings.get(scope.observed_id,{}).get(service)
        if mapping and mapping.get('observed_id')!=scope.observed_id:
            return Availability('invalid_source','数据库映射缺少系统归属','按 observed_id/服务配置独立来源')
    remedy='在 AIOPS_DB_INSPECTION_MAP 中配置目标服务及该来源；凭据仅用环境变量引用'
    if mapping is None:return Availability('unconfigured','未配置 '+service+' 的只读检查映射',remedy)
    if tool in ('db_object_read','db_context_read'):
        driver=mapping.get('driver','postgres')
        if driver not in reader.inspectors:
            return Availability('unsupported','未接入数据库适配器 '+str(driver),'注册对应只读适配器；不回退到其他数据库')
        if driver=='postgres' and not reader.custom_inspector:
            key=mapping.get('dsn_env','')
            if not key or not os.environ.get(key):
                return Availability('unconfigured','未配置该服务的检查连接环境变量',remedy)
    key=SOURCE_KEYS.get(tool)
    if not key:return Availability()
    path=mapping.get(key)
    if not path:return Availability('unconfigured','未配置 '+key,'提供经核实且覆盖目标时间的来源文件；不以空清单替代')
    return file_availability(path,key,params)


def file_availability(path,key,params):
    from .database import read_json
    try:
        data=read_json(path)
        from ...observed.context import current
        scope=current()
        if scope and data.get('observed_id')!=scope.observed_id:raise ValueError('source system mismatch')
        if not isinstance(data,dict) or data.get('verification_method')!='verified_human':
            raise ValueError('来源未核实')
        window=[datetime.fromisoformat(v) for v in data['time_range']]
        if len(window)!=2 or any(v.tzinfo is None for v in window) or window[1]<window[0]:
            raise ValueError('来源时间覆盖非法')
        if key!='runtime_identity_file':
            rows=data.get('records',[])
            if scope and any(r.get('observed_id')!=scope.observed_id for r in rows):raise ValueError('record system mismatch')
            if not any(r.get('service')==params.get('service') and
                       r.get('object_name')==params.get('object_name') for r in rows):
                return Availability('invalid_source','来源缺少目标服务/对象记录','补齐有覆盖说明的真实记录；空清单不能作为反证')
        elif data.get('service')!=params.get('service'):
            return Availability('invalid_source','运行期身份不属于目标服务','核实应用实际连接与目标服务，避免跨系统复用')
        elif not all(data.get(k) for k in ('database','instance_id','schemas')):
            raise ValueError('运行期身份字段不足')
        return Availability()
    except Exception as exc:
        return Availability('invalid_source',key+' 不可读或无法核实：'+type(exc).__name__,
                            '检查来源文件格式、权限与核实记录；不补造历史数据')
