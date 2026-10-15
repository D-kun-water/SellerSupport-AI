"""Persist reservations; incomplete usage stays reserved rather than billed as zero."""
from decimal import Decimal
from .store import uid
from .domain import DomainError, now


class Budget:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def reserve(self, messages):
        # UTF-8 bytes + framing is a conservative token allowance, not measured usage.
        import json
        allowance = len(json.dumps(messages,ensure_ascii=False).encode('utf-8')) + 1024
        if allowance > 20000:
            raise DomainError('CONTEXT_LIMIT', '模型上下文超过当前请求上限', 422)
        cap = (Decimal(allowance)*self.config.input_price + Decimal(self.config.max_output_tokens)*self.config.output_price)/Decimal(1_000_000)
        with self.store.transaction() as db:
            ledger = self.store.all(db, 'model_calls')
            used = sum((Decimal(x['charged_or_reserved_cny']) for x in ledger),Decimal(0))
            if used+cap > self.config.budget_cny:
                raise DomainError('BUDGET_EXCEEDED', '实验预算不足，保留人工路径', 429)
            row = dict(id=uid('CALL'), state='reserved', reserved_cny=str(cap), charged_or_reserved_cny=str(cap),
                       observed_at=now(), currency='CNY', pricing_kind='list_price_estimate', requested_model=self.config.model)
            self.store.put(db,'model_calls',row['id'],row)
            return row['id']

    def settle(self, key, metrics):
        with self.store.transaction() as db:
            row=self.store.get(db,'model_calls',key)
            measured = metrics.get('cost')
            if measured is not None:
                row.update(state='usage_recorded', charged_or_reserved_cny=measured)
            elif metrics.get('llm_calls') == 0:
                row.update(state='not_sent', charged_or_reserved_cny='0')
            else:
                row['state']='usage_unknown_reserved'
            row['metrics']=metrics
            self.store.put(db,'model_calls',key,row)

    def summary(self):
        with self.store.transaction() as db:
            rows=self.store.all(db,'model_calls')
            measured=[r for r in rows if r['state']=='usage_recorded']
            return dict(currency='CNY', limit=str(self.config.budget_cny), calls=len(rows),
                        usage_based_estimate=str(sum((Decimal(r['charged_or_reserved_cny']) for r in measured),Decimal(0))),
                        held_or_estimated=str(sum((Decimal(r['charged_or_reserved_cny']) for r in rows),Decimal(0))),
                        unknown_usage_calls=sum(r['state'] in {'reserved','usage_unknown_reserved'} for r in rows))
