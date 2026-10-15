"""Validation setup: local access controls and evidence-only offline observations."""
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from . import services as svc
from .domain import DomainError, now, timestamp
from .store import uid, encode
from .merchant import digest


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Plan(Input):
    version: int = Field(ge=0)
    owner_id: str
    source_type: Literal['local', 'merchant_authorized']
    authorization_reference: str = Field(min_length=3, max_length=200)
    authorization_confirmed: bool
    scope: Literal['local_offline', 'local_assisted'] = 'local_offline'
    participant_ids: list[str] = Field(default_factory=list, max_length=20)
    starts_at: str
    ends_at: str
    retention_days: int = Field(strict=True, ge=1, le=365)
    minimum_sample: int = Field(strict=True, ge=1, le=10000)
    minimum_shadow_days: int = Field(default=0,strict=True,ge=0,le=90)
    @model_validator(mode='after')
    def dates(self):
        if timestamp(self.starts_at) >= timestamp(self.ends_at):
            raise ValueError('观察窗口结束须晚于开始')
        if self.source_type == 'merchant_authorized' and not self.authorization_confirmed:
            raise ValueError('授权离线观测需要确认授权记录')
        return self


class Switch(Input):
    version: int = Field(ge=0)
    stopped: bool
    reason: str = Field(min_length=3, max_length=200)


class MemberChange(Input):
    version: int = Field(ge=0)
    enabled: bool | None = None
    token: str | None = Field(default=None, min_length=24, max_length=200)
    @model_validator(mode='after')
    def change(self):
        if self.enabled is None and self.token is None:
            raise ValueError('需要新凭据或启用状态')
        return self


class EvidenceBundle(Input):
    manifest: dict
    files: dict[str, str] = Field(min_length=1, max_length=30)


class DeleteReport(Input):
    version: int = Field(ge=1)
    reason: Literal['retention_expired', 'authorization_revoked', 'provider_requested']


def control(store, db):
    return next(iter(store.all(db, 'pilot_settings')), dict(id='pilot', version=0, stopped=False, plan=None))


def require_model_running(store):
    with store.transaction() as db:
        if control(store, db)['stopped']:
            raise DomainError('PILOT_STOPPED', '主管已停止模型候选；请继续人工处理或规则查证', 503)


def summary(store, db):
    cfg = control(store, db)
    reports = store.all(db, 'pilot_reports')
    return dict(control=cfg, reports=reports,
                runtime_mode='local_only', real_pilot_stage='not_started',
                note='授权与人工标签为提供者声明；本地回放不证明真实影子/辅助运行完成。')


def install(app, store, identity, envelope):
    @app.get('/api/merchant/members')
    def members(actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        with store.transaction() as db:
            return envelope([dict(id=m['id'], label=m['label'], role=m['role'],
                enabled=m.get('enabled', True), version=m.get('version', 0)) for m in store.all(db, 'members') if not m.get('retired_legacy_member')])

    @app.get('/api/pilot')
    def get_pilot(actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        with store.transaction() as db:
            return envelope(summary(store, db))

    @app.put('/api/pilot/plan')
    def plan(payload: Plan, actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        with store.transaction() as db:
            cfg = control(store, db)
            svc.version(cfg, payload.version)
            owner = store.get(db, 'members', payload.owner_id)
            if not owner.get('enabled', True):
                raise DomainError('FORBIDDEN', '试点负责人已停用', 409)
            for member_id in payload.participant_ids:
                member = store.get(db, 'members', member_id)
                if not member.get('enabled', True):
                    raise DomainError('FORBIDDEN', '试点参与者已停用', 409)
            # New plans keep historical reports but summary counts only this plan version.
            from .trial_records import archive
            archive(store,db,cfg)
            cfg.update(plan=payload.model_dump(exclude={'version'}), version=cfg['version']+1, revoked=False)
            cfg['plan_id'] = uid('PLAN')
            store.put(db, 'pilot_settings', 'pilot', cfg)
            archive(store,db,cfg)
            store.audit(db, actor, 'pilot_plan_saved', cfg['plan_id'], {'source_type': payload.source_type})
            return envelope(cfg)

    @app.post('/api/pilot/stop')
    def stop(payload: Switch, actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        with store.transaction() as db:
            cfg = control(store, db); svc.version(cfg, payload.version)
            cfg.update(stopped=payload.stopped, version=cfg['version']+1)
            store.put(db, 'pilot_settings', 'pilot', cfg)
            store.audit(db, actor, 'pilot_model_switch', 'pilot', {'stopped': payload.stopped})
            return envelope(cfg)

    @app.patch('/api/merchant/members/{key}')
    def member(key: str, payload: MemberChange, actor=Depends(identity)):
        if key != actor['id']:
            svc.role(actor, {'supervisor'})
        elif actor['role'] != 'supervisor' and payload.enabled is not None:
            raise DomainError('FORBIDDEN', '客服只能轮换自己的凭据', 403)
        with store.transaction() as db:
            row = store.get(db, 'members', key)
            svc.version(dict(version=row.get('version', 0)), payload.version)
            members = store.all(db, 'members')
            if payload.enabled is False and row['role'] == 'supervisor' and not any(
                    m['id'] != key and m['role'] == 'supervisor' and m.get('enabled', True) for m in members):
                raise DomainError('INVALID_STATE', '不能停用最后一名有效主管', 409)
            if payload.token:
                if payload.token.startswith('local-') or any(
                        m['id'] != key and m['token_hash'] == digest(payload.token) for m in members):
                    raise DomainError('VALIDATION_ERROR', '新凭据不能使用初始本地凭据或与已有人员重复')
                row['token_hash'] = digest(payload.token)
            if payload.enabled is not None:
                row['enabled'] = payload.enabled
            row['version'] = row.get('version', 0)+1
            store.put(db, 'members', key, row)
            store.audit(db, actor, 'member_access_changed', key,
                        {'rotated': payload.token is not None, 'enabled': row.get('enabled', True)})
            return envelope({k: v for k, v in row.items() if k != 'token_hash'})

    @app.post('/api/pilot/reports')
    def report(payload: EvidenceBundle, actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        # Flat filenames only. No request can read arbitrary server paths.
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+\.(csv|md|txt)', name) for name in payload.files):
            raise DomainError('VALIDATION_ERROR', '资料名仅允许英文数字横线下划线及csv/md/txt扩展名')
        if sum(len(s.encode('utf-8')) for s in payload.files.values()) > 2_000_000:
            raise DomainError('VALIDATION_ERROR', '资料包文本总量上限2MB')
        from scripts.verify_merchant_pilot import verify
        # Raw text exists only in a temporary directory; persistent records contain aggregates and hashes.
        try:
            with TemporaryDirectory(prefix='sellersupport-offline-', dir=store.path.parent) as folder:
                root = Path(folder)
                for name, content in payload.files.items():
                    (root/name).write_text(content, encoding='utf-8')
                path = root/'manifest.json'; path.write_text(encode(payload.manifest), encoding='utf-8')
                result = verify(path)
        except (ValueError, KeyError, TypeError, OSError, DomainError):
            raise DomainError('VALIDATION_ERROR', '资料包校验失败：核对授权、文件清单、观测字段、来源与耗时')
        with store.transaction() as db:
            cfg = control(store, db); p = cfg.get('plan')
            if not p:
                raise DomainError('INVALID_STATE', '先保存试点计划', 409)
            if result['source_type'] != p['source_type']:
                raise DomainError('VALIDATION_ERROR', '报告与计划资料类型不匹配')
            if p['source_type'] == 'merchant_authorized' and result['authorization_reference'] != p['authorization_reference']:
                raise DomainError('VALIDATION_ERROR', '报告与计划授权记录不匹配')
            with_rows = payload.files.get(payload.manifest.get('observations_csv'), '')
            import csv, io
            observations = list(csv.DictReader(io.StringIO(with_rows)))
            if any(not timestamp(p['starts_at']) <= timestamp(r['reviewed_at']) < timestamp(p['ends_at']) for r in observations):
                raise DomainError('VALIDATION_ERROR', '审核时间不在当前观察窗口内')
            fingerprint = digest(encode(dict(manifest=payload.manifest, files=payload.files)))
            for old in store.all(db, 'pilot_reports'):
                if old.get('fingerprint') == fingerprint and old.get('plan_id') == cfg['plan_id']:
                    return envelope(old)
            aggregates = {k: v for k, v in result.items() if k not in {'rows', 'authorization_reference', 'files'}}
            row = dict(aggregates, id=uid('PILOT'), version=1, plan_id=cfg['plan_id'], fingerprint=fingerprint,
                       evidence_hashes=[f['sha256'] for f in result['files']], state='recorded',
                       sample_status='sufficient_for_planned_review' if result['sample_size'] >= p['minimum_sample'] else 'insufficient',
                       retention_days=p['retention_days'], created_at=now())
            row['decision'] = ('no_real_merchant_conclusion' if p['source_type']=='local' else
                'collect_more_data' if row['sample_status']=='insufficient' else
                'fix_quality' if not result['quality_not_worse_observed'] else
                'fix_workload' if result['net_saved_seconds'] <= 0 else 'review_observed_value')
            store.put(db, 'pilot_reports', row['id'], row)
            store.audit(db, actor, 'pilot_report_recorded', row['id'], {'sample_size': row['sample_size']})
            return envelope(row)

    @app.post('/api/pilot/reports/{key}/delete')
    def delete(key: str, payload: DeleteReport, actor=Depends(identity)):
        svc.role(actor, {'supervisor'})
        with store.transaction() as db:
            row = store.get(db, 'pilot_reports', key); svc.version(row, payload.version)
            if row['state'] == 'deleted':
                return envelope(row)
            if payload.reason == 'retention_expired':
                from datetime import timedelta
                if timestamp(now()) < timestamp(row['created_at']) + timedelta(days=row['retention_days']):
                    raise DomainError('INVALID_STATE', '保留期限尚未到期', 409)
            # Audit tombstone only; no fingerprints, observation hashes or metrics survive here.
            row = dict(id=key, version=row['version']+1, state='deleted', deleted_at=now())
            store.put(db, 'pilot_reports', key, row)
            store.audit(db, actor, 'pilot_report_deleted', key, {'reason': payload.reason})
            return envelope(row)
