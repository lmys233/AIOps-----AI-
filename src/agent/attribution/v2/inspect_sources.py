"""部署侧接入入口：生成不含凭据的映射、预检或显式只读查询。"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from ...common.envfile import load_env_file
from ...tools.availability import check_availability, safe_detail
from .database import DatabaseReader
from .database_config import DATABASE_TOOLS


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--map',required=True,help='服务检查映射路径')
    p.add_argument('--service',required=True)
    p.add_argument('--object',default='',dest='object_name')
    p.add_argument('--init',action='store_true',help='创建单服务模板，已有文件拒绝覆盖')
    p.add_argument('--driver',default='postgres')
    p.add_argument('--dsn-env',default='BUSINESS_INSPECTION_DSN')
    p.add_argument('--query',action='store_true',help='仅对预检通过的来源执行命名只读查询')
    return p


async def inspect(args):
    load_env_file()
    path=Path(args.map).resolve()
    if args.init:
        if args.query:raise ValueError('创建模板与查询不能同时进行')
        mapping={args.service:{'driver':args.driver,'dsn_env':args.dsn_env,
            'env':'','cluster':''}}
        with path.open('x',encoding='utf-8') as f:json.dump(mapping,f,ensure_ascii=False,indent=2)
        print('已创建接入模板；连接身份、迁移及审计来源须另行核实')
        return
    os.environ['AIOPS_DB_INSPECTION_MAP']=str(path)
    reader=DatabaseReader.from_env();params={'service':args.service,'object_name':args.object_name}
    results=[]
    for tool in DATABASE_TOOLS:
        ready=check_availability(lambda p:reader.availability(tool,p),params)
        row={'tool':tool,'status':ready.status,'reason':ready.reason,'remedy':ready.remedy,'queried':False}
        if args.query and ready.ready:
            row['queried']=True
            try:row['observations']=[r.model_dump(mode='json') for r in await reader.read(tool,params)]
            except Exception as exc:
                row.update(status='query_failed',reason=safe_detail(str(exc)))
        results.append(row)
    print(json.dumps(results,ensure_ascii=False,indent=2))


def main():
    try:asyncio.run(inspect(parser().parse_args()))
    except Exception as exc:raise SystemExit('接入检查失败：'+safe_detail(str(exc))) from None


if __name__=='__main__':main()
