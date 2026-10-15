"""Consultation preparation and contextual after-sales review."""
import re
from typing import Literal
from decimal import Decimal
from fastapi import Depends
from pydantic import BaseModel, Field
from . import services as svc
from .domain import now, money


class SuggestionInput(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    region: Literal['US','EU'] = 'US'
    order_id: str | None = None


def suggestions(store, db, actor, p):
    text = p.message
    candidates = [x for x in svc.active(store, db, 'products', p.region, now())
                  if re.search(r'(?<![A-Za-z0-9_-])'+re.escape(x['id'])+r'(?![A-Za-z0-9_-])', text, re.I)]
    if not candidates:
        watts = re.findall(r'(?i)\b(\d+)\s*w\b', text)
        lengths = re.findall(r'(?i)\b(\d+)\s*m\b', text)
        if watts and lengths and re.search(r'(?i)cable|线材|数据线', text):
            candidates = [x for x in svc.active(store, db, 'products', p.region, now())
                          if x.get('category') == 'cable' and str(x.get('power_w')) == watts[0]
                          and re.search(r'(?i)(?<!\d)' + re.escape(lengths[0]) + r'\s*m\b', x['name'])]
        elif watts and re.search(r'(?i)charger|充电器', text):
            candidates = [x for x in svc.active(store, db, 'products', p.region, now())
                          if x.get('category') == 'charger' and str(x.get('power_w')) == watts[0]]
    order = svc.order_for(store, db, p.order_id, actor) if p.order_id else None
    sku = candidates[0]['id'] if len(candidates) == 1 else None
    if order and not candidates:
        sku = order.get('sku')
    device = re.search(r'(?i)\bTEST-(?:D|T|N)\d+(?![A-Za-z0-9_-])|\biPhone\s+\d+(?:\s+Pro(?:\s+Max)?)?', text)
    # A mixed request stays explicit; selecting a use case is still required before lookup.
    use_case = 'video' if re.search(r'(?i)video|视频', text) and not re.search(r'(?i)charg|充电', text) else 'data' if re.search(r'(?i)data transfer|传输数据', text) else 'charging'
    return dict(sku=sku, device_id=device.group(0) if device else None, use_case=use_case,
                candidate_count=len(candidates), needs_confirmation=True)


def install(app, store, identity, envelope):
    @app.post('/api/consultation-suggestions')
    def suggest(p: SuggestionInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(suggestions(store, db, actor, p))

    @app.get('/api/action-proposals/{key}/context')
    def context(key: str, actor=Depends(identity)):
        with store.transaction() as db:
            proposal = svc.proposal_for(store, db, actor, key)
            ticket = svc.ticket_for(store, db, proposal['ticket_id'], actor)
            order = svc.order_for(store, db, proposal['order_id'], actor)
            basis = svc.policy_result(store, db, order, ticket['region'], now()) if proposal.get('type') != 'reship' else dict(result='manual_review', evidence_refs=[])
            if proposal.get('type') == 'reship':
                for policy in svc.active(store, db, 'policies', ticket['region'], now(), kind='reships', channel=ticket['channel']):
                    basis['evidence_refs'].append(svc.evidence(store, db, policy['source_id']))
            return envelope(dict(ticket=ticket, order=order, policy=basis,
                                 refundable=money(Decimal(order['paid'])-Decimal(order['refunded'])),
                                 reviewed_policy=proposal['policy_basis']))
