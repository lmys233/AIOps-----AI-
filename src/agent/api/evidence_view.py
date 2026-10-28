"""Read saved evidence only; summaries come from the saved AnswerView."""
from fastapi.responses import JSONResponse
from ..presentation.references import SOURCES,structured,clean_item,reference,detail,index,lookup


def install(app,service):
    @app.get('/v1/incidents/{incident_id}/evidence')
    async def listing(incident_id:str):
        body=await service.get_incident(incident_id)
        if body is None:return JSONResponse({'code':'INCIDENT_NOT_FOUND','message':'事故记录未留存或已过期'},404)
        return {'incident_id':incident_id,'references':index(body),'content_source':'saved_incident'}

    @app.get('/v1/incidents/{incident_id}/evidence/{evidence_id}')
    async def read(incident_id:str,evidence_id:str):
        body=await service.get_incident(incident_id);result=lookup(body,evidence_id) if body else None
        if result is None:return JSONResponse({'code':'EVIDENCE_NOT_FOUND',
            'message':'该事故没有留存这条证据正文，不能根据引用重建或猜测内容'},404)
        return result
