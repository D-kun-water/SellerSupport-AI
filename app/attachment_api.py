import base64
from typing import Literal
from fastapi import Depends,Response
from pydantic import BaseModel,ConfigDict,Field,field_validator
from . import attachments as svc, services
from .domain import timestamp

class Input(BaseModel):model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class File(Input):
    filename:str=Field(min_length=1,max_length=200)
    content_base64:str=Field(min_length=1,max_length=6990508)
class Preview(File):version:int=Field(ge=1)
class Version(Input):version:int=Field(ge=1)
class Review(Version):
    decision:Literal['confirm','reject']
    text:str=Field(default='',max_length=200000)
    reason:str=Field(min_length=3,max_length=3000)
class Message(Input):
    role:Literal['customer','agent']
    text:str=Field(min_length=1,max_length=12000)
    received_at:str
    @field_validator('received_at')
    @classmethod
    def aware(cls,v):timestamp(v);return v
class Conversation(File):
    external_id:str=Field(min_length=1,max_length=200)
    source_type:Literal['local']
    order_id:str|None=None
    sku:str|None=None
    device_id:str|None=None
    use_case:Literal['charging','data','video']|None=None
    region:Literal['US','EU']='US'

def install(app,store,identity,envelope):
    @app.get('/api/tickets/{key}/attachments')
    def inventory(key:str,actor=Depends(identity)):
        with store.transaction() as db:
            services.ticket_for(store,db,key,actor)
            return envelope([svc.summary(a) for a in store.all(db,'attachments') if a['ticket_id']==key])
    @app.post('/api/tickets/{key}/attachments/preview')
    def preview(key:str,p:Preview,actor=Depends(identity)):
        with store.transaction() as db:return envelope(svc.summary(svc.preview(store,db,actor,key,p.model_dump())))
    @app.post('/api/attachments/{key}/commit')
    def commit(key:str,p:Version,actor=Depends(identity)):
        with store.transaction() as db:return envelope(svc.summary(svc.commit(store,db,actor,key,p.model_dump())))
    @app.post('/api/attachments/{key}/review')
    def review(key:str,p:Review,actor=Depends(identity)):
        with store.transaction() as db:return envelope(svc.summary(svc.review(store,db,actor,key,p.model_dump())))
    @app.get('/api/attachments/{key}/download')
    def download(key:str,actor=Depends(identity)):
        with store.transaction() as db:
            a=svc.attachment_for(store,db,actor,key)
            from urllib.parse import quote
            return Response(base64.b64decode(a['original_base64']),media_type='application/octet-stream',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(a['filename']), 'X-Content-Type-Options':'nosniff'})
    @app.post('/api/conversation-imports/preview')
    def import_preview(p:Conversation,actor=Depends(identity)):
        with store.transaction() as db:return envelope(svc.summary(svc.import_preview(store,db,actor,p.model_dump())))
    @app.post('/api/conversation-imports/{key}/commit')
    def import_commit(key:str,actor=Depends(identity)):
        with store.transaction() as db:return envelope(svc.summary(svc.import_commit(store,db,actor,key)))
