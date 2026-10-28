"""Feedback writes authenticate and bind the immutable, actually saved answer."""
from fastapi import Request
from fastapi.responses import JSONResponse
from ..observed.workspace import authorize,store_of
from .identity import CURRENT
from .models import FeedbackInput
from .store import Conflict,answer_ref,decoded


def install(app,service,store):
    from .identity import install as identity
    identity(app,store)
    root='/v1/observed-systems/{oid}'
    @app.put(root+'/sessions/{sid}/turns/{tid}/feedback')
    async def feedback(oid:str,sid:str,tid:str,data:FeedbackInput):
        principal=CURRENT.get()
        if principal is None:return JSONResponse({'code':'SUBJECT_UNAVAILABLE'},503)
        try:
            tenant,_=await authorize(service,oid)
            await store_of(service).owned(tenant,oid,sid)
            if not await service.history.store.readable(sid,oid):raise KeyError('session hidden')
            row=await store.turn(tenant,oid,sid,tid)
            if answer_ref(row)!=data.answer_ref:raise ValueError('answer reference does not match saved body')
            state=await store.feedback(tenant,principal.subject,oid,sid,tid,data,row)
            return {'feedback':state,'saved':True}
        except Conflict as exc:return JSONResponse({'code':'FEEDBACK_REVISION_CONFLICT','feedback':exc.state},409)
        except (KeyError,PermissionError):return JSONResponse({'code':'FEEDBACK_TARGET_NOT_FOUND'},404)
        except ValueError as exc:return JSONResponse({'code':'FEEDBACK_INVALID','message':str(exc)},422)
        except Exception:return JSONResponse({'code':'FEEDBACK_UNAVAILABLE','message':'反馈未保存，请重试'},503)
    @app.get('/v1/preferences')
    async def latest():
        who=CURRENT.get()
        if who is None:return JSONResponse({'code':'SUBJECT_UNAVAILABLE'},503)
        return await store.current(who.tenant,who.subject)
    @app.get('/v1/preferences/versions')
    async def versions(before:int=2**63-1):
        who=CURRENT.get()
        if who is None:return JSONResponse({'code':'SUBJECT_UNAVAILABLE'},503)
        rows=await store.fetch('SELECT version,hash,cutoff,created_at FROM agent_preference_versions WHERE tenant=$1 AND subject=$2 AND version<$3 ORDER BY version DESC LIMIT 20',who.tenant,who.subject,before)
        return {'versions':[dict(r) for r in rows]}
    @app.get('/v1/preferences/versions/{version}')
    async def version(version:int):
        who=CURRENT.get()
        if who is None:return JSONResponse({'code':'SUBJECT_UNAVAILABLE'},503)
        row=await store.one('SELECT profile,hash,cutoff,created_at FROM agent_preference_versions WHERE tenant=$1 AND subject=$2 AND version=$3',who.tenant,who.subject,version)
        return dict(row,profile=decoded(row['profile'])) if row else JSONResponse({'code':'PREFERENCE_VERSION_NOT_FOUND'},404)
    @app.put('/v1/preferences/settings')
    async def settings(request:Request):
        who=CURRENT.get()
        if who is None:return JSONResponse({'code':'SUBJECT_UNAVAILABLE'},503)
        try:data=await request.json()
        except ValueError:return JSONResponse({'code':'INVALID_SETTINGS'},422)
        if not isinstance(data,dict) or set(data)!={'enabled','learning'} or any(type(v)!=bool for v in data.values()):return JSONResponse({'code':'INVALID_SETTINGS'},422)
        await store.fetch('''INSERT INTO agent_preference_heads(tenant,subject,enabled,learning) VALUES($1,$2,$3,$4)
            ON CONFLICT(tenant,subject) DO UPDATE SET enabled=$3,learning=$4 RETURNING version''',who.tenant,who.subject,data['enabled'],data['learning'])
        store.invalidate(who.tenant,who.subject)
        return data


async def overlay(service,oid,sid,page):
    store=getattr(service,'preferences',None);who=CURRENT.get()
    if not store or not who:return page
    try:
        turns=await store.overlay(who.tenant,who.subject,oid,sid,page['turns'])
        return dict(page,turns=turns)
    except Exception:
        # Do not show a stale or another browser's vote when the overlay is unavailable.
        return dict(page,turns=[dict(t,feedback=None,feedback_unavailable=True) for t in page['turns']],
                    degraded=list(page.get('degraded',[]))+['反馈状态暂不可读；原答案仍可查看'])
