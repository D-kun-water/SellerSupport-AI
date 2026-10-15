"""Isolated local validation workspace. No provider or platform writes are implemented here."""
from datetime import timedelta
from typing import Literal
from fastapi import Depends, Header
from pydantic import Field, model_validator
from .pilot import Input, control
from .domain import DomainError, now, timestamp
from .store import uid, encode
from .merchant import digest
from . import services as svc
from .trial_records import archive,metrics,permitted,plan_for_management,summary

TABLES = ('s3_sources', 's3_cases', 's3_sessions', 's3_plans', 's3_observations')


class Source(Input):
    source_type: Literal['local', 'merchant_authorized']
    authorization_reference: str = Field(min_length=3, max_length=200)
    kind: Literal['compatibility', 'returns']
    sku: str = Field(pattern=r'^[A-Z0-9_-]{3,100}$')
    region: Literal['US', 'EU']
    device_id: str | None = Field(default=None, max_length=100)
    use_case: Literal['charging', 'data', 'video'] | None = None
    result: Literal['compatible', 'conditional', 'incompatible'] | None = None
    conditions: list[str] = Field(min_length=1, max_length=10)
    window_days: int | None = Field(default=None, strict=True, ge=1, le=365)
    source_text: str = Field(min_length=20, max_length=20000)
    locator: str = Field(min_length=3, max_length=200)
    valid_from: str
    valid_to: str
    @model_validator(mode='after')
    def validate_source(self):
        if timestamp(self.valid_to) <= timestamp(self.valid_from):raise ValueError('来源有效区间错误')
        required=[self.sku,*self.conditions]
        if self.kind=='compatibility':
            if not self.device_id or not self.use_case or not self.result or self.window_days is not None:raise ValueError('适配规则字段不完整')
            required.extend([self.device_id,self.use_case,self.result])
        else:
            if self.window_days is None or any((self.device_id,self.use_case,self.result)):raise ValueError('退货政策字段不匹配')
            required.append(str(self.window_days))
        if any(not s.strip() or s not in self.source_text for s in required):raise ValueError('事实与条件必须在来源原文中明确出现')
        return self


class Review(Input):
    version: int = Field(ge=1)
    decision: Literal['pass', 'fail', 'uncertain']
    reason: str = Field(min_length=3, max_length=1000)
    edited_reply: str | None = Field(default=None, min_length=1, max_length=20000)


class Case(Input):
    source_type: Literal['local', 'merchant_authorized']
    external_id: str = Field(min_length=3, max_length=100)
    message: str = Field(min_length=1, max_length=12000)
    kind: Literal['compatibility', 'returns']
    sku: str = Field(min_length=3, max_length=100)
    region: Literal['US', 'EU']
    device_id: str | None = Field(default=None, max_length=100)
    use_case: Literal['charging', 'data', 'video'] | None = None
    delivered_at: str | None = None
    as_of: str
    @model_validator(mode='after')
    def times(self):
        timestamp(self.as_of)
        if self.delivered_at:timestamp(self.delivered_at)
        return self


class Version(Input):
    version: int = Field(ge=1)


class Stage(Input):
    version: int = Field(ge=0)
    stage: Literal['replay', 'shadow', 'assisted', 'paused', 'completed']
    reason: str = Field(min_length=3, max_length=1000)
    decision: Literal['continue','adjust','stop'] | None = None


class Withdraw(Version):
    reason: str = Field(min_length=3,max_length=1000)


class Revoke(Input):
    version: int = Field(ge=0)


class Close(Version):
    session_version: int = Field(ge=0)
    decision: Literal['adjust','stop']
    reason: str = Field(min_length=3,max_length=1000)


class Purge(Input):
    confirm_plan_id: str
    reason: Literal['retention_expired','authorization_revoked','provider_requested']
    dry_run: bool = True
    plan_version: int | None = Field(default=None,ge=1)
    preview_token: str | None = None


class Observation(Version):
    manual_seconds: float = Field(ge=0,le=86400,allow_inf_nan=False)
    wait_seconds: float = Field(ge=0,le=86400,allow_inf_nan=False)
    review_seconds: float = Field(ge=0,le=86400,allow_inf_nan=False)
    rework_seconds: float = Field(ge=0,le=86400,allow_inf_nan=False)
    maintenance_seconds: float = Field(ge=0,le=86400,allow_inf_nan=False)
    baseline_error: bool
    assisted_error: bool
    adopted: bool
    original_channel_receipt: str | None = Field(default=None,min_length=3,max_length=200)
    @model_validator(mode='after')
    def receipt(self):
        if self.adopted != bool(self.original_channel_receipt):raise ValueError('采纳必须填写原渠道凭据，未采纳不能含凭据')
        return self


def gate(store, db, actor, authorization):
    cfg=control(store, db);p=cfg.get('plan')
    if not p:raise DomainError('INVALID_STATE','先保存S3观察计划',409)
    if cfg.get('revoked'):raise DomainError('AUTHORIZATION_REVOKED','当前计划授权已撤销',403)
    if p['source_type']=='merchant_authorized':
        if actor['id'].startswith('local-') or len(authorization.removeprefix('Bearer '))<24:
            raise DomainError('FORBIDDEN','授权资料须使用至少24字符凭据的自定义人员身份',403)
        participants=set(p.get('participant_ids',[]))|{p['owner_id']}
        if actor['id'] not in participants:raise DomainError('FORBIDDEN','当前人员未纳入本计划',403)
        if cfg.get('revoked') or not p['authorization_confirmed'] or not timestamp(p['starts_at']) <= timestamp(now()) < timestamp(p['ends_at']):
            raise DomainError('AUTHORIZATION_EXPIRED','当前不在确认的授权观察窗口内',403)
    return cfg,p


def session(store,db,cfg):
    return next((s for s in store.all(db,'s3_sessions') if s['id']==cfg['plan_id']),
                dict(id=cfg['plan_id'],version=0,cycle=0,stage='replay',history=[],source_type=cfg['plan']['source_type']))


def owned(row,cfg,actor):
    if row.get('state')=='deleted':raise DomainError('NOT_FOUND','对象已清理',404)
    if row['plan_id']!=cfg['plan_id'] or row['source_type']!=cfg['plan']['source_type']:
        raise DomainError('FORBIDDEN','对象不属于当前授权计划',403)
    if row.get('owner') and actor['role']!='supervisor' and row['owner']!=actor['id']:
        raise DomainError('FORBIDDEN','无权处理其他人员的案例',403)


def audit(store,db,actor,action,key,cfg):
    event=dict(id=uid('AUD'),actor=actor['id'],role=actor['role'],action=action,object_ref=key,
        details={'plan_id':cfg['plan_id']},observed_at=now(),source_type=cfg['plan']['source_type'],workspace='s3')
    store.put(db,'audit',event['id'],event)


def response(data,source):return dict(data=data,errors=[],source_type=source,observed_at=now())


def require_open(store,db,cfg):
    if session(store,db,cfg)['stage']=='completed':raise DomainError('INVALID_STATE','计划已收口，不能新增或改写审核与观测',409)


def evidence_current(store,db,cases):
    for c in cases:
        snapshot=c.get('evidence_snapshot')
        if not snapshot:return False
        try:src=store.get(db,'s3_sources',snapshot['id'])
        except DomainError:return False
        if src.get('state')!='published' or src.get('checksum')!=snapshot['checksum'] or not timestamp(src['valid_from'])<=timestamp(now())<timestamp(src['valid_to']):return False
    return bool(cases)


def terminate(store,db,cfg,s,decision,reason,actor):
    if s['stage']=='completed':raise DomainError('INVALID_STATE','计划已经收口，不能覆盖原决策',409)
    measured_stage=s['stage'] if s['stage']!='paused' else next((h['from_stage'] for h in reversed(s['history']) if h['to_stage']=='paused'),'replay')
    m=metrics(store,db,cfg['plan_id'],measured_stage,s.get('cycle',0))
    cases=[c for c in store.all(db,'s3_cases') if c['plan_id']==cfg['plan_id'] and c.get('state')!='deleted' and c.get('cycle',0)==s.get('cycle',0) and c['stage']==measured_stage]
    s['closing_review']=dict(decision=decision,reason=reason,actor=actor['id'],at=now(),cycle=s.get('cycle',0),closed_from=s['stage'],measured_stage=measured_stage,
        metrics=m,evidence_complete=m['case_count']>=cfg['plan']['minimum_sample'] and m['reviewed_cases']==m['case_count'] and not m['missing_measurements'],
        evidence_current=evidence_current(store,db,cases),real_effect='unverified')
    s['history'].append(dict(from_stage=s['stage'],to_stage='completed',cycle=s.get('cycle',0),actor=actor['id'],reason=reason,decision=decision,at=now()))
    s.update(stage='completed',version=s['version']+1);store.put(db,'s3_sessions',s['id'],s);audit(store,db,actor,'s3_trial_terminated',s['id'],cfg)
    return s


def install(app,store,identity):
    @app.get('/api/s3/workspace')
    def workspace(actor=Depends(identity), authorization:str=Header(default='')):
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization)
            sources=[s for s in store.all(db,'s3_sources') if s['plan_id']==cfg['plan_id'] and s['state']!='deleted']
            cases=[c for c in store.all(db,'s3_cases') if c['plan_id']==cfg['plan_id'] and c.get('state')!='deleted' and (actor['role']=='supervisor' or c['owner']==actor['id'])]
            ids={c['id'] for c in cases}
            observations=[r for r in store.all(db,'s3_observations') if r['plan_id']==cfg['plan_id'] and r.get('state')!='deleted' and r['case_id'] in ids]
            return response(dict(session=session(store,db,cfg),sources=sources,cases=cases,observations=observations,
                metrics=metrics(store,db,cfg['plan_id']),stage_metrics=metrics(store,db,cfg['plan_id'],session(store,db,cfg)['stage'],session(store,db,cfg).get('cycle',0)),
                plan_source_type=p['source_type'],plan_id=cfg['plan_id'],authorization_reference=p['authorization_reference'],
                starts_at=p['starts_at'],ends_at=p['ends_at'],external_actions=0,model_calls=0,
                note='本地规则辅助工作区；运行模式只表示工作流配置，不能证明真实试点或经营效果。'),p['source_type'])

    @app.post('/api/s3/sources')
    def add_source(payload:Source,actor=Depends(identity),authorization:str=Header(default='')):
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization)
            require_open(store,db,cfg)
            if payload.source_type!=p['source_type'] or payload.authorization_reference!=p['authorization_reference']:
                raise DomainError('VALIDATION_ERROR','来源类型或授权编号与计划不符')
            row=dict(payload.model_dump(),id=uid('S3SRC'),version=1,plan_id=cfg['plan_id'],state='draft',
                     created_by=actor['id'],created_at=now(),checksum=digest(encode(payload.model_dump())))
            store.put(db,'s3_sources',row['id'],row);audit(store,db,actor,'s3_source_created',row['id'],cfg)
            return response(row,p['source_type'])

    @app.post('/api/s3/cases/{key}/observation')
    def observation(key:str,payload:Observation,actor=Depends(identity),authorization:str=Header(default='')):
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization);row=store.get(db,'s3_cases',key);owned(row,cfg,actor)
            if not row.get('review'):raise DomainError('INVALID_STATE','先完成案例人工审核',409)
            values=payload.model_dump(exclude={'version'});fingerprint=digest(encode(values))
            existing=next((x for x in store.all(db,'s3_observations') if x['case_id']==key and x.get('state')!='deleted'),None)
            if existing:
                if existing['fingerprint']!=fingerprint:raise DomainError('VERSION_CONFLICT','观测已冻结，不能覆盖原测量',409)
                return response(existing,p['source_type'])
            require_open(store,db,cfg)
            svc.version(row,payload.version)
            if (row['review']['decision']=='pass' and payload.assisted_error) or (row['review']['decision']=='fail' and not payload.assisted_error):
                raise DomainError('VALIDATION_ERROR','错误标记与人工审核结论冲突')
            if payload.adopted:
                if row['stage']!='assisted' or session(store,db,cfg)['stage']!='assisted' or row.get('cycle',0)!=session(store,db,cfg).get('cycle',0) or row['review']['decision']!='pass' or not row['copied']:
                    raise DomainError('INVALID_STATE','仅当前辅助阶段审核并复制的案例可记录采纳',409)
                src=store.get(db,'s3_sources',row['evidence_snapshot']['id'])
                if src['state']!='published' or src['checksum']!=row['evidence_snapshot']['checksum'] or not timestamp(src['valid_from'])<=timestamp(now())<timestamp(src['valid_to']):
                    raise DomainError('STALE_EVIDENCE','来源失效，不能登记新采纳',409)
            record=dict(values,id=uid('S3OBS'),version=1,case_id=key,plan_id=cfg['plan_id'],source_type=p['source_type'],
                stage=row['stage'],cycle=row.get('cycle',0),fingerprint=fingerprint,recorded_by=actor['id'],recorded_at=now(),state='recorded',
                measurement_mode='human_declared' if p['source_type']=='merchant_authorized' else 'test_review')
            store.put(db,'s3_observations',record['id'],record);audit(store,db,actor,'s3_observation_recorded',key,cfg)
            return response(record,p['source_type'])

    @app.post('/api/s3/sources/{key}/withdraw')
    def withdraw(key:str,payload:Withdraw,actor=Depends(identity),authorization:str=Header(default='')):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization);row=store.get(db,'s3_sources',key);owned(row,cfg,actor);svc.version(row,payload.version)
            if row['state']!='published':raise DomainError('INVALID_STATE','仅发布来源可停用',409)
            row.update(state='withdrawn',version=row['version']+1);store.put(db,'s3_sources',key,row)
            audit(store,db,actor,'s3_source_withdrawn',key,cfg);return response(row,p['source_type'])

    @app.post('/api/s3/sources/{key}/review')
    def review_source(key:str,payload:Review,actor=Depends(identity),authorization:str=Header(default='')):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization);row=store.get(db,'s3_sources',key);owned(row,cfg,actor);svc.version(row,payload.version)
            require_open(store,db,cfg)
            if row['state']!='draft' or payload.edited_reply is not None:raise DomainError('INVALID_STATE','来源仅允许草稿审核且不能写回复',409)
            if p['source_type']=='merchant_authorized' and row['created_by']==actor['id']:
                raise DomainError('FORBIDDEN','授权来源须由另一名主管核对，不能自审',403)
            row.update(state='published' if payload.decision=='pass' else 'rejected',version=row['version']+1,
                review_mode='human' if p['source_type']=='merchant_authorized' else 'test_review',reviewed_by=actor['id'],reviewed_at=now(),review_reason=payload.reason)
            store.put(db,'s3_sources',key,row);audit(store,db,actor,'s3_source_reviewed',key,cfg)
            return response(row,p['source_type'])

    @app.post('/api/s3/cases')
    def add_case(payload:Case,actor=Depends(identity),authorization:str=Header(default='')):
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization);s=session(store,db,cfg)
            if s['stage'] in {'paused','completed'}:raise DomainError('INVALID_STATE','试点已暂停或完成',409)
            if payload.source_type!=p['source_type']:raise DomainError('VALIDATION_ERROR','案例来源类型与计划不匹配')
            if not timestamp(p['starts_at'])<=timestamp(payload.as_of)<timestamp(p['ends_at']):raise DomainError('VALIDATION_ERROR','案例参考时间不在观察窗口内')
            fingerprint=digest(encode(payload.model_dump()))
            for old in store.all(db,'s3_cases'):
                if old['plan_id']==cfg['plan_id'] and old.get('external_id')==payload.external_id:
                    owned(old,cfg,actor)
                    if old.get('fingerprint')!=fingerprint:raise DomainError('VERSION_CONFLICT','案例标识相同但事实不同',409)
                    return response(old,p['source_type'])
            candidates=[x for x in store.all(db,'s3_sources') if x['plan_id']==cfg['plan_id'] and x['source_type']==p['source_type'] and x['state']=='published'
                and x['kind']==payload.kind and x['sku']==payload.sku and x['region']==payload.region
                and timestamp(x['valid_from'])<=timestamp(payload.as_of)<timestamp(x['valid_to'])]
            if payload.kind=='compatibility':candidates=[x for x in candidates if x['device_id']==payload.device_id and x['use_case']==payload.use_case]
            reply='';status='needs_human';snapshot=None
            if len(candidates)==1:
                snapshot=candidates[0].copy()
                if payload.kind=='compatibility':
                    reply=f"For {payload.sku} and {payload.device_id}, the recorded {payload.use_case} result is {snapshot['result']}. "+' '.join(snapshot['conditions']);status='draft'
                elif payload.delivered_at:
                    elapsed=timestamp(payload.as_of)-timestamp(payload.delivered_at)
                    if elapsed>=timedelta(0):
                        result='inside' if elapsed<=timedelta(days=snapshot['window_days']) else 'outside'
                        reply=f"The recorded delivery time is {result} the {snapshot['window_days']}-day return window. "+' '.join(snapshot['conditions'])+' This is a time-window check, not an approval.';status='draft'
            row=dict(payload.model_dump(),id=uid('S3CASE'),plan_id=cfg['plan_id'],version=1,owner=actor['id'],stage=s['stage'],cycle=s.get('cycle',0),status=status,
                fingerprint=fingerprint,reply=reply,evidence_snapshot=snapshot,review=None,copied=False,created_at=now())
            store.put(db,'s3_cases',row['id'],row);audit(store,db,actor,'s3_case_created',row['id'],cfg)
            return response(row,p['source_type'])

    @app.post('/api/s3/cases/{key}/review')
    def review_case(key:str,payload:Review,actor=Depends(identity),authorization:str=Header(default='')):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization);row=store.get(db,'s3_cases',key);owned(row,cfg,actor);svc.version(row,payload.version)
            require_open(store,db,cfg)
            if row.get('review'):raise DomainError('INVALID_STATE','案例审核已冻结，修正需新案例',409)
            if p['source_type']=='merchant_authorized' and row['owner']==actor['id']:raise DomainError('FORBIDDEN','授权案例需另一名主管审核',403)
            if payload.decision=='pass' and (row['status']!='draft' or not row['evidence_snapshot'] or not (payload.edited_reply or row['reply'])):raise DomainError('NO_EVIDENCE','来源或关键事实不足，不能审核通过')
            row.update(reply=payload.edited_reply or row['reply'],version=row['version']+1,
                review=dict(decision=payload.decision,reason=payload.reason,reviewed_by=actor['id'],reviewed_at=now(),
                            mode='human' if p['source_type']=='merchant_authorized' else 'test_review'))
            store.put(db,'s3_cases',key,row);audit(store,db,actor,'s3_case_reviewed',key,cfg);return response(row,p['source_type'])

    @app.post('/api/s3/stage')
    def stage(payload:Stage,actor=Depends(identity),authorization:str=Header(default='')):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization);s=session(store,db,cfg);svc.version(s,payload.version)
            if payload.stage=='completed' and payload.decision in {'adjust','stop'}:
                return response(terminate(store,db,cfg,s,payload.decision,payload.reason,actor),p['source_type'])
            allowed={'replay':{'shadow','paused'},'shadow':{'assisted','paused'},'assisted':{'completed','paused'},'paused':{'replay'},'completed':set()}
            if payload.stage not in allowed[s['stage']]:raise DomainError('INVALID_STATE','阶段转换不允许',409)
            if payload.stage in {'shadow','assisted','completed'}:
                cases=[c for c in store.all(db,'s3_cases') if c['plan_id']==cfg['plan_id'] and c.get('stage')==s['stage'] and c.get('state')!='deleted' and c.get('cycle',0)==s.get('cycle',0)]
                allow_bad=payload.stage=='completed' and payload.decision in {'adjust','stop'}
                if len(cases)<p['minimum_sample'] or any(not c.get('review') or (not allow_bad and c['review']['decision']!='pass') for c in cases):
                    raise DomainError('QUALITY_GATE_FAILED','当前阶段样本或人工审核未通过',409)
                measured=metrics(store,db,cfg['plan_id'],s['stage'],s.get('cycle',0))
                if measured['missing_measurements'] or measured['measured_cases']<p['minimum_sample']:
                    raise DomainError('OBSERVATION_GATE_FAILED','当前阶段缺少逐案例配对耗时观测',409)
            if payload.stage=='assisted' and p.get('minimum_shadow_days',0):
                entered=next((h['at'] for h in reversed(s['history']) if h['to_stage']=='shadow'),None)
                if not entered or timestamp(now())<timestamp(entered)+timedelta(days=p['minimum_shadow_days']):
                    raise DomainError('OBSERVATION_GATE_FAILED','影子观察尚未达到计划时长',409)
            if payload.stage=='assisted' and p['source_type']=='merchant_authorized' and p['scope']!='local_assisted':
                raise DomainError('FORBIDDEN','授权范围未包含本地人工辅助',403)
            if payload.stage=='completed':
                if not payload.decision:raise DomainError('VALIDATION_ERROR','完成评审须记录继续、调整或停止')
                if not evidence_current(store,db,cases):raise DomainError('STALE_EVIDENCE','当前来源已失效，不能据此评审继续；可调整或停止',409)
                measured=metrics(store,db,cfg['plan_id'],s['stage'],s.get('cycle',0))
                if payload.decision=='continue' and (measured['net_saved_seconds']<=0 or measured['assisted_errors']>measured['baseline_errors'] or measured['semantic_uncertain']):
                    raise DomainError('VALUE_GATE_FAILED','观测净价值或质量不支持继续，应调整或停止',409)
                if p['source_type']=='merchant_authorized' and payload.decision=='continue' and (not measured['adopted_cases'] or p.get('minimum_shadow_days',0)<1):
                    raise DomainError('VALUE_GATE_FAILED','继续真实辅助试点需采纳记录及至少一天的已配置影子观察',409)
                s['closing_review']=dict(decision=payload.decision,reason=payload.reason,actor=actor['id'],at=now(),metrics=measured,cycle=s.get('cycle',0),closed_from=s['stage'],measured_stage=s['stage'],evidence_complete=True,evidence_current=True,
                    real_effect='observed_sample_only' if p['source_type']=='merchant_authorized' else 'unverified')
            if s['stage']=='paused' and payload.stage=='replay':s['cycle']=s.get('cycle',0)+1
            s['history'].append(dict(from_stage=s['stage'],to_stage=payload.stage,cycle=s.get('cycle',0),actor=actor['id'],reason=payload.reason,decision=payload.decision,at=now()))
            s.update(stage=payload.stage,version=s['version']+1);store.put(db,'s3_sessions',s['id'],s);audit(store,db,actor,'s3_stage_changed',s['id'],cfg)
            return response(s,p['source_type'])

    @app.post('/api/s3/cases/{key}/copy')
    def copy(key:str,payload:Version,actor=Depends(identity),authorization:str=Header(default='')):
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization);row=store.get(db,'s3_cases',key);owned(row,cfg,actor);svc.version(row,payload.version)
            if session(store,db,cfg)['stage']!='assisted' or row['stage']!='assisted' or row.get('cycle',0)!=session(store,db,cfg).get('cycle',0) or not row.get('review') or row['review']['decision']!='pass':
                raise DomainError('INVALID_STATE','仅辅助阶段经审核通过的当前阶段案例可复制',409)
            snapshot=row['evidence_snapshot'];source=store.get(db,'s3_sources',snapshot['id'])
            if source['state']!='published' or source['checksum']!=snapshot['checksum'] or not timestamp(source['valid_from'])<=timestamp(now())<timestamp(source['valid_to']):
                raise DomainError('STALE_EVIDENCE','来源已停用或过期，禁止采纳旧回复',409)
            row.update(copied=True,version=row['version']+1);store.put(db,'s3_cases',key,row);audit(store,db,actor,'s3_copy_requested_not_sent',key,cfg)
            return response(dict(reply=row['reply'],case=row,note='仅返回已审核文本，不表示客户收到'),p['source_type'])

    @app.get('/api/s3/export')
    def export(actor=Depends(identity),authorization:str=Header(default='')):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            cfg,p=gate(store,db,actor,authorization)
            return response({t:[r for r in store.all(db,t) if r.get('plan_id',r['id'])==cfg['plan_id']] for t in TABLES},p['source_type'])

    def management(db,actor,authorization):
        svc.role(actor,{'supervisor'});cfg=control(store,db);p=cfg.get('plan')
        if not p:raise DomainError('INVALID_STATE','先保存计划',409)
        if p['source_type']=='merchant_authorized' and (actor['id'].startswith('local-') or len(authorization.removeprefix('Bearer '))<24 or actor['id'] not in set(p.get('participant_ids',[]))|{p['owner_id']}):
            raise DomainError('FORBIDDEN','仅授权计划中的自定义主管可管理',403)
        return cfg,p

    @app.get('/api/s3/plans')
    def plans(actor=Depends(identity),authorization:str=Header(default='')):
        svc.role(actor,{'supervisor'})
        with store.transaction() as db:
            current=control(store,db);archive(store,db,current)
            rows=[]
            for r in store.all(db,'s3_plans'):
                if not permitted(r['plan'],actor,authorization):continue
                s=session(store,db,dict(plan_id=r['id'],plan=r['plan']))
                item=dict(r,current=r['id']==current.get('plan_id'),stage=s['stage'],session_version=s['version'],cycle=s.get('cycle',0),counts={t:sum(x.get('plan_id')==r['id'] and x.get('state')!='deleted' for x in store.all(db,t)) for t in ('s3_sources','s3_cases','s3_observations')})
                rows.append(item)
            return response(rows,'mixed' if len({r['source_type'] for r in rows})>1 else rows[0]['source_type'] if rows else 'unknown')

    @app.post('/api/s3/plans/{key}/close')
    def close_plan(key:str,payload:Close,actor=Depends(identity),authorization:str=Header(default='')):
        with store.transaction() as db:
            archived=plan_for_management(store,db,key,actor,authorization);svc.version(archived,payload.version)
            cfg=dict(plan_id=key,plan=archived['plan']);s=session(store,db,cfg);svc.version(s,payload.session_version)
            terminate(store,db,cfg,s,payload.decision,payload.reason,actor)
            return response(summary(store,db,archived),archived['source_type'])

    @app.get('/api/s3/plans/{key}/summary')
    def plan_summary(key:str,actor=Depends(identity),authorization:str=Header(default='')):
        with store.transaction() as db:
            archived=plan_for_management(store,db,key,actor,authorization)
            return response(summary(store,db,archived),archived['source_type'])

    @app.post('/api/s3/plans/{key}/revoke')
    def revoke_plan(key:str,payload:Version,actor=Depends(identity),authorization:str=Header(default='')):
        with store.transaction() as db:
            archived=plan_for_management(store,db,key,actor,authorization);svc.version(archived,payload.version)
            archived.update(revoked=True,version=archived['version']+1);store.put(db,'s3_plans',key,archived)
            cfg=control(store,db)
            if cfg.get('plan_id')==key:
                cfg.update(revoked=True,version=cfg['version']+1);store.put(db,'pilot_settings','pilot',cfg)
            audit(store,db,actor,'s3_authorization_revoked',key,dict(plan_id=key,plan=archived['plan']))
            return response(dict(plan_id=key,revoked=True),archived['source_type'])

    @app.post('/api/s3/revoke')
    def revoke(payload:Revoke,actor=Depends(identity),authorization:str=Header(default='')):
        with store.transaction() as db:
            cfg,p=management(db,actor,authorization);svc.version(cfg,payload.version)
            cfg.update(revoked=True,version=cfg['version']+1);store.put(db,'pilot_settings','pilot',cfg)
            archived=archive(store,db,cfg);archived.update(revoked=True,version=archived['version']+1);store.put(db,'s3_plans',archived['id'],archived)
            audit(store,db,actor,'s3_authorization_revoked',cfg['plan_id'],cfg)
            return response(dict(plan_id=cfg['plan_id'],revoked=True),p['source_type'])

    @app.post('/api/s3/purge')
    def purge(payload:Purge,actor=Depends(identity),authorization:str=Header(default='')):
        with store.transaction() as db:
            archived=plan_for_management(store,db,payload.confirm_plan_id,actor,authorization)
            if payload.plan_version is not None:svc.version(archived,payload.plan_version)
            cfg=dict(plan_id=archived['id'],plan=archived['plan'],revoked=archived['revoked']);p=cfg['plan']
            if payload.reason=='authorization_revoked' and not cfg.get('revoked'):raise DomainError('INVALID_STATE','授权尚未撤销',409)
            selected=[]
            for table in ('s3_cases','s3_sources','s3_observations'):
                for row in store.all(db,table):
                    if row['plan_id']!=cfg['plan_id'] or row.get('state')=='deleted':continue
                    if payload.reason=='retention_expired' and timestamp(now())<timestamp(row.get('created_at',row.get('recorded_at')))+timedelta(days=p['retention_days']):continue
                    selected.append((table,row))
            # Expired source originals also exist in case snapshots; purge their dependent copies.
            source_ids={r['id'] for t,r in selected if t=='s3_sources'}
            selected_ids={r['id'] for _,r in selected}
            for row in store.all(db,'s3_cases'):
                if row['plan_id']==cfg['plan_id'] and row.get('state')!='deleted' and row['id'] not in selected_ids and (row.get('evidence_snapshot') or {}).get('id') in source_ids:
                    selected.append(('s3_cases',row));selected_ids.add(row['id'])
            case_ids={r['id'] for t,r in selected if t=='s3_cases'}
            for row in store.all(db,'s3_observations'):
                if row['plan_id']==cfg['plan_id'] and row.get('state')!='deleted' and row['id'] not in selected_ids and row['case_id'] in case_ids:
                    selected.append(('s3_observations',row));selected_ids.add(row['id'])
            preview_token=digest(encode(dict(plan_version=archived['version'],reason=payload.reason,plan_id=cfg['plan_id'],
                session_version=session(store,db,cfg)['version'],rows=sorted((t,r['id'],r['version']) for t,r in selected))))
            if not payload.dry_run:
                if payload.preview_token!=preview_token:raise DomainError('VERSION_CONFLICT','请先预览；清理对象或状态已变化时需重新预览',409)
                for table,row in selected:
                    store.put(db,table,row['id'],dict(id=row['id'],version=row['version']+1,plan_id=cfg['plan_id'],source_type=p['source_type'],state='deleted'))
                # Remove free-text reasons in the stage history too; preserve minimal tombstone.
                if payload.reason!='retention_expired' or selected:
                    s=session(store,db,cfg);store.put(db,'s3_sessions',s['id'],dict(id=s['id'],version=s['version']+1,stage='completed',history=[],source_type=p['source_type']))
                audit(store,db,actor,'s3_data_purged',cfg['plan_id'],cfg)
            return response(dict(dry_run=payload.dry_run,selected=len(selected),plan_version=archived['version'],plan_id=cfg['plan_id'],
                preview_token=preview_token,counts={t:sum(t==table for table,_ in selected) for t in ('s3_sources','s3_cases','s3_observations')},
                note='仅清理指定计划的当前库记录；原始资料包和旧备份需分别清理'),p['source_type'])
