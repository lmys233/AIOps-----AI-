"""Server-signed browser identity; ChatUser.id is never an authority."""
import base64,hashlib,hmac,json,os,secrets,time
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit
from fastapi.responses import JSONResponse

CURRENT=ContextVar('preference_principal',default=None)
COOKIE='aiops_subject'


@dataclass(frozen=True)
class Principal:
    tenant:str
    subject:str
    api:bool=False


def sign(subject,key,issued=None):
    value=f'{subject}.{int(issued or time.time())}'
    return value+'.'+hmac.new(key.encode(),value.encode(),hashlib.sha256).hexdigest()


def verify(value,key):
    try:
        subject,issued,mac=value.split('.');stamp=int(issued)
        if len(subject)!=48 or any(c not in '0123456789abcdef' for c in subject):return None
        if not 0<=time.time()-stamp<=365*86400:return None
        if not hmac.compare_digest(sign(subject,key,stamp),value):return None
        return subject
    except (ValueError,TypeError):return None


def same_origin(request):
    origin=request.headers.get('origin')
    if request.headers.get('sec-fetch-site')=='cross-site':return False
    if not origin:return request.headers.get('x-aiops-feedback')=='1'
    parsed=urlsplit(origin)
    return parsed.scheme==request.url.scheme and parsed.netloc==request.url.netloc


def install(app,store):
    secret_cache=[]
    @app.middleware('http')
    async def principal(request,call_next):
        if not (request.url.path.startswith('/v1/') or request.url.path=='/webui/'):
            return await call_next(request)
        sensitive='/feedback' in request.url.path or '/preferences' in request.url.path
        if not secret_cache:
            try:secret_cache.append(await store.secret())
            except Exception:
                if sensitive:return JSONResponse({'code':'FEEDBACK_UNAVAILABLE','message':'反馈存储暂不可用，未保存'},503)
                return await call_next(request)
        key=secret_cache[0];tenant=os.getenv('AIOPS_TENANT','default')
        # Only deployment-owned token-digest mapping can declare a machine subject.
        token=request.headers.get('authorization','').removeprefix('Bearer ')
        try:api_subject=json.loads(os.getenv('AIOPS_PREFERENCE_API_SUBJECTS','{}')).get(hashlib.sha256(token.encode()).hexdigest()) if token else None
        except ValueError:api_subject=None
        subject=api_subject or verify(request.cookies.get(COOKIE,''),key)
        fresh=not subject
        if not subject:subject=secrets.token_hex(24)
        if sensitive and request.method in ('PUT','POST','PATCH','DELETE') and not api_subject and not same_origin(request):
            return JSONResponse({'code':'FEEDBACK_ORIGIN_REJECTED'},403)
        context=CURRENT.set(Principal(tenant,subject,bool(api_subject)))
        try:response=await call_next(request)
        finally:CURRENT.reset(context)
        if fresh and not api_subject:response.set_cookie(COOKIE,sign(subject,key),max_age=365*86400,httponly=True,samesite='strict',secure=request.url.scheme=='https')
        return response
