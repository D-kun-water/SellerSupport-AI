import hashlib
import json
import secrets
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .domain import DomainError, now, timestamp
from .store import ROOT, Store, encode, uid
from . import services as svc
from .assist import assist
from .model_config import ModelConfig, local_values, bootstrap_tokens
from .live_assist import LiveAssist
from .translation import Translator, TranslationInput, TranslationPreviewInput
from . import knowledge


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TicketInput(Input):
    message: str = Field(min_length=1, max_length=12000)
    source_type: Literal['local']
    channel: Literal['direct'] = 'direct'
    region: Literal['US', 'EU'] = 'US'
    sku: str | None = None
    device_id: str | None = None
    use_case: Literal['charging', 'data', 'video'] | None = None
    order_id: str | None = None
    external_id: str | None = None


class TicketPatch(Input):
    version: int = Field(ge=1)
    status: str | None = None
    resolution: str | None = Field(default=None, min_length=3)
    message: str | None = Field(default=None, min_length=1, max_length=12000)
    draft: str | None = Field(default=None, max_length=20000)
    sku: str | None = None
    device_id: str | None = None
    use_case: Literal['charging', 'data', 'video'] | None = None
    region: Literal['US', 'EU'] | None = None
    order_id: str | None = None


class RunInput(Input):
    version: int = Field(ge=1)
    as_of: str = Field(default_factory=now)

    @field_validator('as_of')
    @classmethod
    def aware(cls, v):
        timestamp(v)
        return v


class CandidateLoadInput(Input):
    version: int = Field(ge=1)


class KnowledgeRule(Input):
    device_id: str = Field(pattern=r'^[A-Z0-9][A-Z0-9_-]{1,99}$')
    use_case: Literal['charging', 'data', 'video']
    result: Literal['conditional', 'compatible', 'incompatible']
    conditions: list[str] = Field(min_length=1, max_length=10)
    conditions_en: list[str] | None = Field(default=None, min_length=1, max_length=10)

    @model_validator(mode='after')
    def bilingual(self):
        if self.conditions_en is not None and (len(self.conditions_en) != len(self.conditions) or any(not x.strip() for x in self.conditions_en)):
            raise ValueError('中英文适配条件必须逐条对应且不能为空')
        return self


class KnowledgeChanges(Input):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    rules: list[KnowledgeRule] | None = Field(default=None, max_length=30)
    window_days: int | None = Field(default=None, ge=1, le=365)


class KnowledgeDraftInput(Input):
    kind: Literal['products', 'policies']
    object_id: str
    expected_version: str = Field(min_length=1)
    changes: KnowledgeChanges
    source_text: str = Field(min_length=20, max_length=20000)
    reason: str = Field(min_length=3, max_length=1000)
    task_id: str | None = None


class KnowledgeActionInput(Input):
    version: int = Field(ge=1)
    reason: str = Field(default='运行固定规则回归', min_length=3, max_length=1000)


class ReshipItem(Input):
    line_id:str=Field(min_length=1,max_length=200)
    sku:str=Field(min_length=1,max_length=200)
    quantity:int=Field(ge=1,le=1000,strict=True)


class ProposalInput(Input):
    ticket_id: str
    type:Literal['refund','reship']='refund'
    amount: str|None=None
    currency: Literal['USD']='USD'
    items:list[ReshipItem]|None=Field(default=None,min_length=1,max_length=100)
    idempotency_key:str|None=Field(default=None,min_length=8,max_length=128)
    reason: str = Field(min_length=3, max_length=3000)

    @model_validator(mode='after')
    def check_type(self):
        if self.type=='refund' and (self.amount is None or self.items):raise ValueError('退款需要金额，不能含补发行')
        if self.type=='reship' and (not self.items or self.amount is not None or not self.idempotency_key):raise ValueError('补发需要订单行和幂等键，不含退款金额')
        return self


class ProposalPatch(Input):
    version: int = Field(ge=1)
    amount: str|None=None
    items:list[ReshipItem]|None=Field(default=None,min_length=1,max_length=100)
    reason: str = Field(min_length=3, max_length=3000)


class ApprovalInput(Input):
    version: int = Field(ge=1)
    decision: Literal['approve', 'reject']
    reason: str = Field(min_length=3)


class ExecutionInput(Input):
    version: int = Field(ge=1)
    execution_mode: Literal['manual_record']
    result: Literal['success', 'failed', 'unknown']
    receipt: str = Field(min_length=3, max_length=3000)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ConfirmInput(Input):
    version: int = Field(ge=1)
    receipt: str = Field(min_length=3, max_length=3000)
    execution_mode: Literal['manual_record']
    resolve: bool = False


class ModelSwitch(Input):
    enabled: bool


class ImportInput(Input):
    kind: Literal['products', 'tickets']
    rows: list[dict] = Field(min_length=1, max_length=1000)


class ProductInput(Input):
    id: str = Field(pattern=r'^[A-Z0-9][A-Z0-9_-]{1,99}$')
    sku: str = Field(pattern=r'^[A-Z0-9][A-Z0-9_-]{1,99}$')
    shop_id: str = Field(pattern=r'^[A-Z0-9_-]{3,80}$')
    source_type: Literal['local']
    category: Literal['charger', 'cable', 'stand', 'case', 'hub', 'powerbank', 'storage']
    name: str = Field(min_length=1)
    region: Literal['US', 'EU']
    version: str
    review_status: Literal['draft', 'published']
    validation: str
    valid_from: str
    valid_to: str | None
    source_id: str
    power_w: int | None = Field(ge=0)
    connector: str | None
    data_rate_mbps: int | None = Field(ge=0)
    video_support: bool | None
    rules: list[dict]
    specifications: dict[str, str] = Field(default_factory=dict, max_length=15)

    @field_validator('valid_from', 'valid_to')
    @classmethod
    def dates(cls, v):
        if v:
            timestamp(v)
        return v


def envelope(data):
    return dict(data=data, errors=[], source_type='local', observed_at=now())


def create_app(db_path=None, serve_frontend=True, model_config=None, model_provider=None, seed_mode=None):
    app = FastAPI(title='SellerSupport API', version='0.2.2')
    settings=local_values()
    config=model_config or ModelConfig.load()
    app.state.reply_enabled=config.enabled
    configured_db=Path(settings.get('SELLERSUPPORT_DB', 'storage/sellersupport.sqlite3'))
    if not configured_db.is_absolute():
        configured_db=ROOT/configured_db
    store = Store(db_path if db_path is not None else configured_db)
    from . import merchant
    mode=seed_mode or settings.get('SELLERSUPPORT_SEED_MODE','empty')
    if mode not in {'test','empty'}: raise RuntimeError('seed mode must be test or empty')
    with store.transaction() as db:
        configured=store.all(db,'merchant_settings')
    if mode=='test' and (not configured or configured[0]['shop_id']=='TEST-SHOP'):
        store.seed()
        from .reships import seed_policy
        seed_policy(store)
    app.state.store = store
    # Local production, probes and batch evaluations share one project budget ledger.
    budget_store=Store(ROOT/'storage/model-evaluation.sqlite3') if db_path is None else store
    model_runtime=LiveAssist(store,config,model_provider,budget_store)
    app.state.model_runtime=model_runtime
    translator=Translator(model_runtime)
    app.state.translator=translator
    tokens = bootstrap_tokens(store.path, settings)
    identities = [(tokens[role], dict(id='local-'+role, role=role,
                   orders=['TEST-O1', 'TEST-O2', 'TEST-O3', 'TEST-O4'])) for role in ('agent', 'supervisor')]
    if len({token for token, _ in identities}) != len(identities):
        raise RuntimeError('Local identity tokens must be distinct')

    merchant.seed_members(store,identities,empty=mode=='empty')
    merchant.retire_legacy_member(store)

    def identity(authorization: str = Header(default='')):
        if authorization.startswith('Bearer '):
            return merchant.authenticate(store,authorization[7:])
        raise DomainError('FORBIDDEN','需要有效本地凭据',403)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError):
        return JSONResponse(status_code=error.status, content=dict(data=None, errors=[dict(code=error.code, message=error.message)], source_type='unknown' if request.url.path.startswith('/api/s3/') else 'local', observed_at=now()))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Never echo submitted credentials, customer messages or evidence in validation responses.
        return JSONResponse(status_code=422, content=dict(data=None, errors=[dict(
            code='VALIDATION_ERROR', message='请求字段不符合要求',
            fields=[dict(location=list(e['loc']), type=e['type']) for e in error.errors()])],
            source_type='unknown' if request.url.path.startswith('/api/s3/') else 'local', observed_at=now()))

    @app.get('/api/health')
    def health():
        return envelope(dict(status='ok',mode='local',provider='rules_and_optional_deepseek',schema_version=9,
                             translation_api_version=1,workflow_api_version=3,knowledge_catalog_api_version=2,model_experiment_enabled=app.state.reply_enabled,model={**config.public(),'enabled':app.state.reply_enabled}))

    @app.get('/api/model-status')
    def model_status(actor=Depends(identity)):
        probes=[]
        for path in (ROOT/'output/evaluations').glob('PROBE-*.json'):
            try:
                probe=json.loads(path.read_text(encoding='utf-8'))
            except (OSError,ValueError):
                continue
            if probe.get('status')=='success' and probe.get('configuration',{}).get('requested_model')==config.model:
                probes.append(probe['observed_at'])
        return envelope(dict(configuration={**config.public(),'enabled':app.state.reply_enabled},budget=model_runtime.budget.summary(),
                             last_successful_probe_at=max(probes,default=None),
                             note='探针是历史记录，不证明当前连接仍可用；生成质量以报告和人工复核为准'))

    @app.post('/api/model-settings')
    def model_switch(payload: ModelSwitch, actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        if payload.enabled and not config.api_key:
            raise DomainError('MODEL_CONFIG_ERROR','先在本地配置模型密钥，再启用AI回复',409)
        app.state.reply_enabled=payload.enabled
        with store.transaction() as db:
            store.audit(db,actor,'reply_generation_switched','model',{'enabled':payload.enabled})
        return envelope({'enabled':payload.enabled})

    @app.get('/api/session')
    def session(actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(dict(actor=actor['id'], role=actor['role'], shop_id=svc.shop(store,db), mode='local'))

    @app.get('/api/pilot-readiness')
    def pilot_readiness(actor=Depends(identity), authorization: str = Header(default='')):
        svc.role(actor,{'supervisor'})
        real,local=[],[]
        # Historical filesystem reports are not implicitly authorized across merchants.
        real_reference=None
        with store.transaction() as db:
            from .pilot import control
            current_plan=control(store,db).get('plan')
            if current_plan and current_plan['source_type']=='merchant_authorized':
                from .trial import gate
                _,authorized_plan=gate(store,db,actor,authorization)
                real_reference=authorized_plan['authorization_reference']
        for path in (ROOT/'output/pilot').glob('*.json'):
            try:r=json.loads(path.read_text(encoding='utf-8'))
            except (ValueError,OSError):continue
            if not isinstance(r,dict):continue
            if r.get('state') not in {'observations_verified','local_tool_check'}:continue
            if r.get('source_type')=='merchant_authorized' and (not real_reference or r.get('authorization_reference')!=real_reference):continue
            item={k:r.get(k) for k in ('sample_size','net_saved_seconds','baseline_errors','assisted_errors','semantic_uncertain')}
            item['id']=path.stem
            (real if r.get('source_type')=='merchant_authorized' else local).append(item)
        return envelope(dict(real_reports=real,local_reports=local,checklist=[
            '待提供商家名称、授权凭据、负责人和本地离线范围',
            '待提供经商家确认的商品/政策依据与脱敏历史咨询',
            '待记录实际Agent输出、独立人工判断和原人工流程耗时',
            '待记录等待、审核、返工和知识维护耗时；样本不足时不报告推广效果']))

    @app.get('/api/products')
    def products(actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(store.all(db, 'products'))

    @app.get('/api/knowledge-revisions')
    def knowledge_revisions(actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(store.all(db, 'knowledge_revisions'))

    @app.get('/api/sources/{key}')
    def source_detail(key: str, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(store.get(db, 'sources', key))

    @app.post('/api/knowledge-revisions')
    def knowledge_draft(payload: KnowledgeDraftInput, actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        changes = payload.changes.model_dump(exclude_none=True)
        allowed = {'name','rules'} if payload.kind == 'products' else {'window_days'}
        if not set(changes) <= allowed:
            raise DomainError('VALIDATION_ERROR', '字段不适用于此知识类型')
        if payload.kind == 'products':
            for rule in changes.get('rules', []):
                header = f"Rule for {rule['device_id']}/{rule['use_case']}: {rule['result']}."
                from .catalog_text import chinese_rule_header
                if (header not in payload.source_text and chinese_rule_header(rule) not in payload.source_text) or any(not x.strip() or x not in payload.source_text for x in rule['conditions']):
                    raise DomainError('VALIDATION_ERROR', '规则与条件必须在合成来源原文中明确出现')
            pairs=[(x['device_id'],x['use_case']) for x in changes.get('rules',[])]
            if len(pairs)!=len(set(pairs)):
                raise DomainError('VALIDATION_ERROR', '同设备与用途存在重复规则')
        elif 'window_days' in changes and str(changes['window_days']) not in payload.source_text:
            raise DomainError('VALIDATION_ERROR', '政策原文必须明确退货天数')
        with store.transaction() as db:
            if payload.task_id:
                task = store.get(db, 'tasks', payload.task_id)
                if payload.kind != 'products' or task['sku'] != payload.object_id or task['state'] not in {'in_progress', 'delivered'}:
                    raise DomainError('VALIDATION_ERROR', '知识变更必须绑定进行中/已交付任务的同一 SKU')
            row = knowledge.create_revision(store, db, actor, payload.kind, payload.object_id,
                            payload.expected_version, changes, payload.source_text, payload.reason)
            row['task_id'] = payload.task_id
            store.put(db, 'knowledge_revisions', row['id'], row)
            return envelope(row)

    @app.post('/api/knowledge-revisions/{key}/regression')
    def knowledge_regression(key: str, payload: KnowledgeActionInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(knowledge.regression(store, db, actor, key, payload.version))

    @app.post('/api/knowledge-revisions/{key}/publish')
    def knowledge_publish(key: str, payload: KnowledgeActionInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(knowledge.publish(store, db, actor, key, payload.version, payload.reason))

    @app.post('/api/knowledge-revisions/{key}/rollback')
    def knowledge_rollback(key: str, payload: KnowledgeActionInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(knowledge.rollback_draft(store, db, actor, key, payload.version, payload.reason))

    @app.get('/api/products/{key}')
    def product(key: str, actor=Depends(identity)):
        with store.transaction() as db:
            p = store.get(db, 'products', key)
            return envelope(dict(product=p, evidence=svc.evidence(store, db, p['source_id'])))

    @app.get('/api/policies')
    def policies(actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(store.all(db, 'policies'))

    @app.get('/api/orders/{key}')
    def order(key: str, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(svc.order_for(store, db, key, actor))

    @app.get('/api/orders/{key}/reship-availability')
    def reship_availability(key: str, actor=Depends(identity)):
        from .reships import availability
        with store.transaction() as db:
            order=svc.order_for(store,db,key,actor)
            return envelope(availability(store,db,order))

    @app.get('/api/tickets')
    def tickets(actor=Depends(identity)):
        with store.transaction() as db:
            return envelope([t for t in store.all(db, 'tickets') if svc.can_view_ticket(t,actor)])

    @app.post('/api/tickets')
    def add_ticket(payload: TicketInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(svc.create_ticket(store, db, actor, payload.model_dump()))

    @app.get('/api/tickets/{key}')
    def get_ticket(key: str, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(svc.ticket_for(store, db, key, actor))

    @app.patch('/api/tickets/{key}')
    def patch_ticket(key: str, payload: TicketPatch, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(svc.change_ticket(store, db, actor, key, payload.model_dump(exclude_unset=True)))

    @app.post('/api/tickets/{key}/assist')
    def run(key: str, payload: RunInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(assist(store, db, actor, key, payload.model_dump()))

    @app.post('/api/tickets/{key}/translation')
    async def translate_ticket(key: str, payload: TranslationInput, actor=Depends(identity)):
        return envelope(await translator.translate(actor, key, payload))

    @app.post('/api/translation-preview')
    async def translate_preview(payload: TranslationPreviewInput, actor=Depends(identity)):
        request=TranslationInput(context_version=1, target='zh', text=payload.text)
        return envelope(await translator.translate(actor, None, request))

    @app.post('/api/tickets/{key}/assist-live')
    async def live_run(key: str,payload: RunInput,actor=Depends(identity)):
        with store.transaction() as db:
            t=svc.ticket_for(store,db,key,actor)
            svc.version(t,payload.version)
            svc.check_can_generate(store,db,t)
        if not app.state.reply_enabled:
            raise DomainError('MODEL_DISABLED','线上实验入口未启用；离线评测可继续运行',503)
        return envelope(await model_runtime.run(actor,key,payload.model_dump()))

    @app.post('/api/tickets/{key}/candidates/{run_id}/load-for-editing')
    def load_candidate(key: str,run_id: str,payload: CandidateLoadInput,actor=Depends(identity)):
        return envelope(model_runtime.load_for_editing(actor,key,run_id,payload.version))

    @app.get('/api/evaluation-reports')
    def evaluation_reports(actor=Depends(identity)):
        svc.role(actor,{'supervisor'})
        reports=[]
        comparison_path=ROOT/'output/evaluations/comparison.json'
        adjustments={}
        common_scores={}
        if comparison_path.exists():
            adjustments={r['id']:r['pricing_adjustment'] for r in json.loads(comparison_path.read_text(encoding='utf-8'))['reports']}
        common_path=ROOT/'output/evaluations/common-scorer.json'
        if common_path.exists():
            common=json.loads(common_path.read_text(encoding='utf-8'))
            common_scores={r['id']:{**{k:r[k] for k in ('attempted','hard_checks_passed','failed')},
                                    'scorer_version':common['scorer_version']} for r in common['reports']}
        for path in sorted((ROOT/'output/evaluations').glob('live-*.json')):
            report=json.loads(path.read_text(encoding='utf-8'))
            item={k:report.get(k) for k in ('id','observed_at','state','prompt_version','scorer_version','summary','groups','configuration','evaluation_scope','human_gold_review')}
            item['pricing_adjustment']=adjustments.get(report['id'])
            item['common_scoring']=common_scores.get(report['id'])
            reports.append(item)
        return envelope(sorted(reports,key=lambda r:r['observed_at'],reverse=True))

    @app.get('/api/evaluation-reports/{key}')
    def evaluation_detail(key: str,actor=Depends(identity)):
        svc.role(actor,{'supervisor'})
        # Match report identities from the inventory rather than interpolating a path.
        for path in (ROOT/'output/evaluations').glob('live-*.json'):
            report=json.loads(path.read_text(encoding='utf-8'))
            if report.get('id')==key:
                return envelope(report)
        raise DomainError('NOT_FOUND','未找到评测',404)

    @app.post('/api/tickets/{key}/copy-record')
    def copied(key: str, payload: RunInput, actor=Depends(identity)):
        with store.transaction() as db:
            t = svc.ticket_for(store, db, key, actor)
            svc.version(t, payload.version)
            svc.check_can_reply(store,db,t,actor)
            if not t['draft']:
                raise DomainError('VALIDATION_ERROR', '没有可复制草稿')
            t['copied'], t['version'] = True, t['version'] + 1
            store.put(db, 'tickets', key, t)
            store.audit(db, actor, 'draft_copied_not_sent', key)
            return envelope(t)

    @app.post('/api/tickets/{key}/sent-confirmation')
    def sent(key: str, payload: ConfirmInput, actor=Depends(identity)):
        with store.transaction() as db:
            t = svc.ticket_for(store, db, key, actor)
            svc.version(t, payload.version)
            svc.check_can_reply(store,db,t,actor)
            if not t['draft']:
                raise DomainError('VALIDATION_ERROR', '没有待确认草稿')
            if payload.resolve:
                svc.check_can_resolve(store, db, t)
                if svc.active_handoff(store, db, t):
                    raise DomainError('INVALID_STATE','人工交接请由负责人凭结果结案',409)
                t.update(status='resolved', resolution=payload.receipt)
            elif t['status'] in {'new','reopened'}:
                t['status']='in_progress'
            t['sent_confirmations'].append(dict(receipt=payload.receipt, mode='manual_record', draft_snapshot=t['draft'], actor=actor['id'], observed_at=now()))
            t['version'] += 1
            store.put(db, 'tickets', key, t)
            store.audit(db, actor, 'manual_send_confirmed', key)
            if payload.resolve:
                store.audit(db, actor, 'ticket_resolved', key, {'resolution':payload.receipt})
            return envelope(t)

    @app.get('/api/action-proposals')
    def proposals(actor=Depends(identity)):
        with store.transaction() as db:
            visible = {t['id'] for t in store.all(db, 'tickets') if svc.can_view_ticket(t,actor)}
            return envelope([p for p in store.all(db, 'proposals') if p['ticket_id'] in visible])

    @app.post('/api/action-proposals')
    def propose(payload: ProposalInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(svc.create_proposal(store, db, actor, payload.model_dump()))

    @app.patch('/api/action-proposals/{key}')
    def proposal_patch(key: str, payload: ProposalPatch, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(svc.update_proposal(store, db, actor, key, payload.model_dump()))

    @app.post('/api/action-proposals/{key}/approve')
    def proposal_approve(key: str, payload: ApprovalInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(svc.approve(store, db, actor, key, payload.model_dump()))

    @app.post('/api/action-proposals/{key}/execution-records')
    def execution(key: str, payload: ExecutionInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(svc.record_execution(store, db, actor, key, payload.model_dump()))

    @app.get('/api/runs')
    def runs(actor=Depends(identity)):
        with store.transaction() as db:
            visible = {t['id'] for t in store.all(db, 'tickets') if svc.can_view_ticket(t,actor)}
            return envelope([r for r in store.all(db, 'runs') if r['ticket_id'] in visible])

    @app.get('/api/audit')
    def audit(actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        with store.transaction() as db:
            return envelope([r for r in store.all(db, 'audit') if r.get('workspace')!='s3'])

    def validate_row(db, kind, row):
        if kind == 'tickets':
            return TicketInput.model_validate(row).model_dump()
        p = ProductInput.model_validate(row).model_dump()
        # Do not add a new optional field to unchanged legacy import payloads.
        if 'specifications' not in row:
            p.pop('specifications', None)
        if p['id'] != p['sku']:
            raise ValueError('id 必须等于 SKU')
        s = store.get(db, 'sources', p['source_id'])
        if s['source_type'] != 'local':
            raise ValueError('仅允许本地来源')
        if p['review_status'] == 'published':
            # Import does not grant publish privileges or bypass regression.
            old = store.get(db, 'products', p['id'])
            if encode(old) != encode(p):
                raise ValueError('商品变更请通过来源版本草稿、回归与主管发布流程')
        if p['valid_to'] and timestamp(p['valid_to']) <= timestamp(p['valid_from']):
            raise ValueError('有效期必须递增')
        return p

    @app.post('/api/imports/preview')
    def preview(payload: ImportInput, actor=Depends(identity)):
        with store.transaction() as db:
            details, valid, seen = [], [], set()
            for i, raw in enumerate(payload.rows, 1):
                try:
                    row = validate_row(db, payload.kind, raw)
                    key = row['id'] if payload.kind == 'products' else row.get('external_id')
                    if payload.kind == 'tickets' and not key:
                        raise ValueError('导入工单必须提供 external_id 用于去重')
                    if key in seen:
                        details.append(dict(row=i, status='duplicate_in_file', key=key))
                        continue
                    seen.add(key)
                    duplicate = False
                    for old in store.all(db, payload.kind):
                        if (old['id'] if payload.kind == 'products' else old['external_id']) == key:
                            if payload.kind == 'products' and encode(old) != encode(row):
                                raise ValueError('同 SKU 内容冲突，需要版本修订流程')
                            if payload.kind == 'tickets':
                                svc.ticket_for(store, db, old['id'], actor)
                            duplicate = True
                    if payload.kind == 'tickets' and row.get('order_id'):
                        svc.order_for(store, db, row['order_id'], actor)
                    details.append(dict(row=i, status='duplicate' if duplicate else 'valid', key=key))
                    if not duplicate:
                        valid.append(row)
                except Exception as exc:
                    details.append(dict(row=i, status='error', error=str(exc) if not isinstance(exc, DomainError) else exc.message))
            batch = dict(id=uid('IMP'), actor=actor['id'], kind=payload.kind, source_type='local',
                         file_hash=hashlib.sha256(encode(payload.model_dump()).encode()).hexdigest(),
                         details=details, rows=valid, state='preview', created_at=now())
            store.put(db, 'imports', batch['id'], batch)
            return envelope(batch)

    @app.post('/api/imports/{key}/commit')
    def commit(key: str, actor=Depends(identity)):
        with store.transaction() as db:
            b = store.get(db, 'imports', key)
            if b['actor'] != actor['id']:
                raise DomainError('FORBIDDEN', '无权提交该批次', 403)
            if b['state'] == 'committed':
                return envelope(b)
            if any(x['status'] == 'error' for x in b['details']):
                raise DomainError('VALIDATION_ERROR', '含错误行的批次不可提交，请修正后重新预览')
            inserted = 0
            for row in b['rows']:
                row = validate_row(db, b['kind'], row)
                if b['kind'] == 'tickets':
                    before = len(store.all(db, 'tickets'))
                    svc.create_ticket(store, db, actor, row)
                    inserted += len(store.all(db, 'tickets')) - before
                else:
                    existing = {p['id']: p for p in store.all(db, 'products')}
                    if row['id'] in existing:
                        if encode(existing[row['id']]) != encode(row):
                            raise DomainError('VERSION_CONFLICT', '预览后商品已变更', 409)
                    else:
                        store.put(db, 'products', row['id'], row)
                        inserted += 1
            b['state'], b['inserted'] = 'committed', inserted
            store.put(db, 'imports', key, b)
            store.audit(db, actor, 'import_committed', key, dict(inserted=inserted))
            return envelope(b)

    @app.get('/api/export')
    def export(actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        with store.transaction() as db:
            from .store import TABLES
            from .trial import TABLES as S3_TABLES
            return envelope({table: [] if table in S3_TABLES else [{k:v for k,v in row.items() if k!='token_hash'}
                for row in store.all(db, table) if table!='audit' or row.get('workspace')!='s3'] for table in TABLES})

    from .workflow_api import install as install_workflow
    install_workflow(app,store,identity,envelope)
    from .improvement_api import install
    install(app, store, identity, envelope)
    from .handoff_api import install as install_handoffs
    install_handoffs(app,store,identity,envelope,lambda db:merchant.actors(store,db))
    merchant.install(app,store,identity,envelope)
    from .catalog_api import install as install_catalog
    install_catalog(app,store,identity,envelope)
    from .attachment_api import install as install_attachments
    install_attachments(app,store,identity,envelope)
    from .pilot import install as install_pilot
    install_pilot(app,store,identity,envelope)
    from .trial import install as install_trial
    install_trial(app,store,identity)
    frontend = ROOT / 'frontend/dist'
    if serve_frontend and (frontend / 'assets').is_dir() and (frontend / 'index.html').is_file():
        app.mount('/assets', StaticFiles(directory=frontend / 'assets'), name='assets')

        @app.get('/')
        def index():
            with store.transaction() as db:
                configured = store.all(db, 'merchant_settings')
                if not configured or not configured[0].get('initialized'):
                    return RedirectResponse('/merchant-setup', headers={'Cache-Control':'no-store'})
            # HTML must revalidate after deployment; hashed assets remain cacheable.
            return FileResponse(frontend / 'index.html',headers={'Cache-Control':'no-store'})
    return app


app = create_app()
