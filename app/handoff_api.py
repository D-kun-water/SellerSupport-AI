from typing import Literal
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from . import handoffs as svc, services


class Input(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)


class Create(Input):
    version:int=Field(ge=1)
    assignee:str
    reason:str=Field(min_length=3,max_length=3000)
    attempted_steps:str=Field(min_length=3,max_length=5000)
    pending_checks:str=Field(min_length=3,max_length=5000)


class Action(Input):
    version:int=Field(ge=1)
    action:Literal['accept','note','close','reassign','reopen']
    note:str=Field(min_length=3,max_length=5000)
    reference:str|None=Field(default=None,min_length=3,max_length=3000)
    assignee:str|None=None
    ticket_version:int|None=Field(default=None,ge=1)

    @model_validator(mode='after')
    def validate_action(self):
        if self.action=='close' and (not self.reference or not self.ticket_version):
            raise ValueError('结案需凭据及当前工单版本')
        if self.action=='reassign' and not self.assignee:raise ValueError('重新指派需负责人')
        return self


class Reconcile(Input):
    version:int=Field(ge=1)
    order_version:int=Field(ge=1)
    result:Literal['success','failed','still_unknown']
    amount:str|None=None
    items:list[dict]|None=None
    currency:str
    receipt:str=Field(min_length=3,max_length=3000)
    reason:str=Field(min_length=3,max_length=3000)
    idempotency_key:str=Field(min_length=3,max_length=200)
    execution_mode:Literal['manual_record']


def install(app,store,identity,envelope,team):
    @app.get('/api/handoff-team')
    def members(ticket_id: str | None = None, actor=Depends(identity)):
        with store.transaction() as db:
            ticket=services.ticket_for(store,db,ticket_id,actor) if ticket_id else None
            return envelope([dict(id=m['id'],role=m['role'],label=m.get('label',m['id']))
                for m in (team(db) if callable(team) else team).values()
                if not ticket or not ticket.get('order_id') or ticket['order_id'] in m['orders']])

    @app.get('/api/handoffs')
    def inventory(actor=Depends(identity)):
        with store.transaction() as db:
            visible={t['id'] for t in store.all(db,'tickets') if services.can_view_ticket(t,actor)}
            return envelope([h for h in store.all(db,'handoffs') if h['ticket_id'] in visible])

    @app.post('/api/tickets/{key}/handoffs')
    def create(key:str,payload:Create,actor=Depends(identity)):
        with store.transaction() as db:return envelope(svc.create(store,db,actor,key,payload.model_dump(),team(db) if callable(team) else team))

    @app.post('/api/handoffs/{key}/actions')
    def action(key:str,payload:Action,actor=Depends(identity)):
        with store.transaction() as db:return envelope(svc.action(store,db,actor,key,payload.model_dump(),team(db) if callable(team) else team))

    @app.post('/api/action-proposals/{key}/reconcile')
    def reconcile(key:str,payload:Reconcile,actor=Depends(identity)):
        with store.transaction() as db:return envelope(svc.reconcile(store,db,actor,key,payload.model_dump()))
