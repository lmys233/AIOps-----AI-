"""Workspace export ownership; legacy unscoped exports retain their original bytes."""
from .context import body_scope
from .workspace import authorize


async def check(service,payload,body):
    oid=payload.get('observed_id')
    if not oid:
        if not body_scope(body):return None
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422,content={'code':'INVALID_PARAMS','message':'导出已分系统事故需提供 observed_id'})
    try:
        await authorize(service,oid)
        if body_scope(body)!=oid or body.get('session_id')!=payload.get('session_id'):
            raise KeyError('incident does not belong to workspace and session')
    except (KeyError,ValueError,PermissionError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404,content={'code':'INCIDENT_NOT_FOUND','message':'该事故不属于当前系统和会话'})
