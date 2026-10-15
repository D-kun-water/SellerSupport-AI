import asyncio
import hashlib
import time
from .assist import assist
from .services import ticket_for, version
from .domain import DomainError, now
from .store import uid, encode
from .provider import DeepSeekProvider, ProviderError
from .budget import Budget
from .quality import build_messages, check_draft, PROMPT_VERSION, SCORER_VERSION


class LiveAssist:
    def __init__(self,store,config,provider=None,budget_store=None):
        self.store,self.config=store,config
        self.provider=provider or DeepSeekProvider(config)
        self.budget=Budget(budget_store or store,config)
        self.slots=asyncio.Semaphore(2)
        self.waiting=0
        self.busy_tickets=set()

    def load_for_editing(self,actor,key,run_id,expected_version):
        from .pilot import require_model_running
        require_model_running(self.store)
        with self.store.transaction() as db:
            ticket=ticket_for(self.store,db,key,actor)
            version(ticket,expected_version)
            trace=self.store.get(db,'runs',run_id)
            if trace['ticket_id']!=key:
                raise DomainError('FORBIDDEN','候选不属于此工单',403)
            if trace.get('execution_mode')!='live_model' or trace.get('status')=='failed' or trace.get('quality',{}).get('status')!='REVIEW':
                raise DomainError('CANDIDATE_REJECTED','候选未通过检查，请重新生成',409)
            if trace['context_version']!=ticket['context_version']:
                raise DomainError('STALE_CONTEXT','工单事实已变化，请重新生成候选',409)
            payload=dict(version=ticket['version'],as_of=trace['as_of'])
            try:
                fresh=assist(self.store,db,actor,key,payload,persist=False)
            except DomainError:
                raise DomainError('STALE_EVIDENCE','依据不可用，请重新生成候选',409)
            snapshots={field:trace[field] for field in ('missing_fields','evidence_refs','item_results','partial')}
            snapshots.update(status=trace['baseline_status'],reply_draft=trace['baseline_reply_draft'])
            if any(encode(fresh[field])!=encode(value) for field,value in snapshots.items()):
                raise DomainError('STALE_EVIDENCE','依据或订单已变化，请重新生成候选',409)
            candidate,quality=check_draft(trace['raw_model_content'],fresh)
            if quality['status']!='REVIEW':
                raise DomainError('CANDIDATE_REJECTED','候选未通过当前检查器，请重新生成',409)
            self.store.audit(db,actor,'candidate_loaded_for_editing',key,
                             dict(run_id=run_id,scorer_version=SCORER_VERSION,human_review='required'))
            # Loading returns text for manual editing; it does not overwrite or send a draft.
            return dict(run_id=run_id,reply_draft=candidate['reply_draft'],quality=quality,human_review='required')

    async def run(self,actor,key,payload,force_component=False):
        from .pilot import require_model_running
        require_model_running(self.store)
        start=time.perf_counter()
        accepted_at=now()
        with self.store.transaction() as db:
            ticket=ticket_for(self.store,db,key,actor)
            version(ticket,payload['version'])
            baseline=assist(self.store,db,actor,key,payload,persist=False)
        if key in self.busy_tickets or self.waiting>=22:
            raise DomainError('BUSY','运行槽已满或该工单已有生成任务',429)
        self.busy_tickets.add(key)
        self.waiting+=1
        trace=dict(**baseline)
        trace.update(id=uid('RUN'),accepted_at=accepted_at,provider='deepseek',requested_model=self.config.model,
                     ticket_version=ticket['version'],
                     resolved_model=None,prompt_version=PROMPT_VERSION,scorer_version=SCORER_VERSION,
                     execution_mode='live_model',status='failed',reply_draft='',queue_ms=0,
                     provider_ttft_ms=None,provider_total_ms=None,usage_complete=False,metrics=None,
                     draft_adoptable=False,quality=None,cancelled=False)
        held=False
        call_id=None
        try:
            await asyncio.wait_for(self.slots.acquire(),timeout=self.config.deadline_seconds)
            held=True
            require_model_running(self.store)
            trace['queue_ms']=round((time.perf_counter()-start)*1000,3)
            if not force_component and baseline['status']=='escalated':
                trace.update(**{k:baseline[k] for k in ('status','reply_draft','summary','item_results','evidence_refs','missing_fields')})
                trace.update(execution_mode='rules_bypass',provider='deterministic',requested_model=None,llm_calls=0,
                             quality=dict(status='PASS',scope='deterministic_bypass'),draft_adoptable=True)
            else:
                if not self.config.api_key:
                    raise ProviderError('NOT_CONFIGURED','本地模型Key未配置',dict(llm_calls=0))
                messages=build_messages(ticket,baseline)
                trace['prompt_hash']=hashlib.sha256(encode(messages).encode()).hexdigest()
                trace['evidence_hash']=hashlib.sha256(encode(baseline['evidence_refs']).encode()).hexdigest()
                call_id=self.budget.reserve(messages)
                remaining=self.config.deadline_seconds-(time.perf_counter()-start)
                if remaining<=0:
                    raise ProviderError('PROVIDER_TIMEOUT','排队和准备已超过总截止时间',dict(llm_calls=0))
                result=await self.provider.complete(messages,remaining)
                trace.update(result.metrics)
                self.budget.settle(call_id,result.metrics)
                parsed,quality=check_draft(result.content,baseline)
                trace.update(quality=quality,raw_model_content=result.content,baseline_status=baseline['status'],
                             baseline_reply_draft=baseline['reply_draft'],candidate=parsed)
                if quality['status']=='FAIL':
                    trace.update(status='failed',error_code='DRAFT_VALIDATION_FAILED',error_message='模型草稿未通过硬检查，保留原人工草稿')
                else:
                    trace.update(status=parsed['status'],reply_draft=parsed['reply_draft'],summary=parsed['summary'],
                                 claims=parsed['claims'],draft_adoptable=False,human_review='required')
        except (ProviderError,DomainError) as exc:
            metrics=getattr(exc,'metrics',dict(llm_calls=0))
            if call_id:
                self.budget.settle(call_id,metrics)
            trace.update(metrics)
            trace.update(status='failed',error_code=exc.code,error_message=exc.message,
                         quality=dict(status='ERROR',issues=[exc.code]))
        except asyncio.TimeoutError:
            trace.update(status='failed',error_code='QUEUE_TIMEOUT',quality=dict(status='ERROR',issues=['QUEUE_TIMEOUT']))
        except asyncio.CancelledError:
            # The request may already have reached the provider. Keep the reservation
            # until usage can be reconciled; a cancelled client is never a free call.
            metrics=dict(llm_calls=1 if call_id else 0,cost=None,usage_complete=False)
            if call_id:
                self.budget.settle(call_id,metrics)
            trace.update(metrics)
            trace.update(status='failed',cancelled=True,error_code='RUN_CANCELLED',
                         quality=dict(status='ERROR',issues=['RUN_CANCELLED']))
        finally:
            if held:self.slots.release()
            self.waiting-=1
            self.busy_tickets.discard(key)
        trace['total_ms']=round((time.perf_counter()-start)*1000,3)
        trace['observed_at']=now()
        with self.store.transaction() as db:
            current=ticket_for(self.store,db,key,actor)
            if current['version']!=ticket['version'] or current['context_version']!=ticket['context_version']:
                trace.update(status='failed',draft_adoptable=False,error_code='STALE_CONTEXT',
                             quality=dict(status='ERROR',issues=['STALE_CONTEXT']))
            elif trace['quality']['status']=='REVIEW':
                # A product, policy, source, or order may have changed while the model
                # was running even though the ticket version stayed the same.
                fields=('status','missing_fields','summary','reply_draft','evidence_refs','item_results','partial')
                try:
                    fresh=assist(self.store,db,actor,key,payload,persist=False)
                    stale=any(encode(fresh[field])!=encode(baseline[field]) for field in fields)
                except DomainError:
                    stale=True
                if stale:
                    trace.update(status='failed',draft_adoptable=False,error_code='STALE_EVIDENCE',
                                 quality=dict(status='ERROR',issues=['STALE_EVIDENCE']))
            self.store.put(db,'runs',trace['id'],trace)
            # Live candidates are separate from the customer's editable draft until reviewed.
            self.store.audit(db,actor,'model_candidate_completed',key,dict(run_id=trace['id'],status=trace['status']))
        return trace
