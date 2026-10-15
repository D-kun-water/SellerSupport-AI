"""Feedback, improvement tasks and quality-review endpoints."""
from typing import Literal
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .domain import timestamp, now
from . import services as svc, improvement as imp, evaluation as ev


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Version(Input):
    version: int = Field(ge=1)


class Reason(Version):
    reason: str = Field(min_length=3, max_length=3000)


class FeedbackInput(Input):
    ticket_id: str
    message_id: str
    quote: str = Field(min_length=1, max_length=12000)
    label: Literal['compatibility', 'instructions', 'quality', 'logistics', 'unknown']


class IssueInput(Input):
    title: str = Field(min_length=3, max_length=300)
    feedback_ids: list[str] = Field(min_length=1, max_length=500)


class Decision(Input):
    feedback_id: str
    decision: Literal['accepted', 'rejected']


class MemberReview(Reason):
    decisions: list[Decision] = Field(min_length=1, max_length=500)


class IssueState(Reason):
    state: Literal['closed', 'investigating', 'dismissed']


class GroupInput(Reason):
    action: Literal['merge', 'split']
    other_id: str | None = None
    other_version: int | None = Field(default=None, ge=1)
    feedback_ids: list[str] = Field(default_factory=list, max_length=500)
    title: str = Field(default='拆分专题', min_length=3, max_length=300)


class HypothesisInput(Version):
    type: Literal['compatibility', 'usage', 'instructions', 'quality', 'logistics', 'unknown']
    description: str = Field(min_length=5, max_length=3000)
    support_refs: list[str] = Field(default_factory=list, max_length=500)
    counter_refs: list[str] = Field(default_factory=list, max_length=500)
    missing_checks: str = Field(min_length=3, max_length=3000)


class HypothesisReview(Reason):
    decision: Literal['confirm', 'reject']
    before_source_id: str | None = None
    proposed_text: str | None = Field(default=None, max_length=20000)
    verification_record: str | None = Field(default=None, max_length=5000)


class TaskInput(Input):
    issue_id: str
    cause_id: str | None = None
    title: str = Field(min_length=3, max_length=300)
    type: Literal['faq', 'product_page', 'manual', 'process', 'quality_investigation']
    owner: str = Field(min_length=1, max_length=200)
    expected_change: str = Field(min_length=3, max_length=3000)
    acceptance_criteria: str = Field(min_length=3, max_length=3000)
    due_at: str | None = None

    @field_validator('due_at')
    @classmethod
    def date(cls, v):
        if v is not None: timestamp(v)
        return v


class TaskState(Reason):
    state: Literal['accepted', 'in_progress', 'blocked', 'cancelled', 'reopened', 'done']


class TaskAssignment(Reason):
    owner: str = Field(min_length=1,max_length=200)


class ContentInput(Version):
    text: str = Field(min_length=3, max_length=10000)


class DeliverInput(Version):
    reference: str = Field(min_length=3, max_length=3000)
    result: str = Field(min_length=3, max_length=3000)


class EffectiveInput(Version):
    effective_at: str
    revision_id: str | None = None
    evidence: str = Field(default='', max_length=3000)

    @field_validator('effective_at')
    @classmethod
    def date(cls, v):
        timestamp(v)
        return v


class ObservationInput(Version):
    start_at: str
    end_at: str
    as_of: str = Field(default_factory=now)
    conclusion: Literal['improvement_signal', 'no_change', 'worse', 'insufficient_data']
    coverage_complete: bool = False
    coverage_notes: str = Field(min_length=3, max_length=3000)
    confounders: str = Field(min_length=3, max_length=3000)
    reason: str = Field(min_length=3, max_length=3000)

    @field_validator('start_at', 'end_at', 'as_of')
    @classmethod
    def date(cls, v):
        timestamp(v)
        return v


class Expected(Input):
    status: Literal['draft', 'need_clarification', 'escalated']
    required_fragments: list[str] = Field(default_factory=list, max_length=30)
    forbidden_fragments: list[str] = Field(default_factory=list, max_length=30)
    error_code: Literal['FORBIDDEN','NO_EVIDENCE','VALIDATION_ERROR'] | None = None

    @field_validator('required_fragments', 'forbidden_fragments')
    @classmethod
    def fragments(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError('断言片段不能为空')
        return values


class BadCaseInput(Input):
    ticket_id: str
    run_id: str
    error_type: Literal['unsupported_claim', 'wrong_status', 'missing_condition', 'wrong_evidence', 'other']
    correction: str = Field(min_length=3, max_length=20000)
    expected: Expected


class CaseReview(Reason):
    decision: Literal['approve', 'reject']
    expected: Expected


class CaseRevision(Reason):
    expected: Expected


def install(app, store, identity, envelope):
    def supervisor(actor):
        svc.role(actor, {'supervisor'})

    @app.post('/api/feedback')
    def nominate(p: FeedbackInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.nominate(store, db, actor, p.model_dump()))

    @app.get('/api/feedback')
    def feedback(actor=Depends(identity)):
        with store.transaction() as db:
            visible = {t['id'] for t in store.all(db, 'tickets') if actor['role'] == 'supervisor' or t['owner'] == actor['id']}
            return envelope([f for f in store.all(db, 'feedback') if f['ticket_id'] in visible])

    @app.get('/api/issue-suggestions')
    def suggestions(actor=Depends(identity)):
        supervisor(actor)
        with store.transaction() as db:
            return envelope(imp.suggestion_groups(store, db))

    @app.get('/api/issues')
    def issues(actor=Depends(identity)):
        supervisor(actor)
        with store.transaction() as db:
            return envelope([imp.issue_view(store, db, r) for r in store.all(db, 'issues')])

    @app.post('/api/issues')
    def create_issue(p: IssueInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.create_issue(store, db, actor, p.model_dump()))

    @app.post('/api/issues/{key}/review')
    def review_members(key: str, p: MemberReview, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.review_members(store, db, actor, key, p.model_dump()))

    @app.post('/api/issues/{key}/grouping')
    def grouping(key: str, p: GroupInput, actor=Depends(identity)):
        if p.action == 'merge':
            imp.require(p.other_id is not None and p.other_version is not None, '合并需提供另一专题及版本')
        with store.transaction() as db:
            return envelope(imp.grouping_operation(store, db, actor, key, p.model_dump()))

    @app.post('/api/issues/{key}/state')
    def issue_state(key: str, p: IssueState, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.change_issue(store, db, actor, key, p.model_dump()))

    @app.get('/api/issue-operations')
    def operations(actor=Depends(identity)):
        supervisor(actor)
        with store.transaction() as db:
            return envelope(store.all(db, 'issue_operations'))

    @app.post('/api/issue-operations/{key}/undo')
    def undo(key: str, p: Reason, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.undo_grouping(store, db, actor, key, p.model_dump()))

    @app.get('/api/hypotheses')
    def hypotheses(actor=Depends(identity)):
        supervisor(actor)
        with store.transaction() as db:
            return envelope(store.all(db, 'hypotheses'))

    @app.post('/api/issues/{key}/hypotheses')
    def add_hypothesis(key: str, p: HypothesisInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.add_hypothesis(store, db, actor, key, p.model_dump()))

    @app.post('/api/hypotheses/{key}/review')
    def review_hypothesis(key: str, p: HypothesisReview, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.confirm_hypothesis(store, db, actor, key, p.model_dump()))

    @app.get('/api/tasks')
    def tasks(actor=Depends(identity)):
        with store.transaction() as db:
            return envelope([t for t in store.all(db, 'tasks') if actor['role']=='supervisor' or t.get('owner_id')==actor['id']])

    @app.get('/api/task-members')
    def task_members(actor=Depends(identity)):
        supervisor(actor)
        from .merchant import actors
        with store.transaction() as db:
            return envelope([{'id':m['id'],'label':m['label'],'role':m['role']} for m in actors(store,db).values()])

    @app.post('/api/tasks/{key}/assign')
    def assign_task(key: str, p: TaskAssignment, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.assign_task(store,db,actor,key,p.model_dump()))

    @app.post('/api/tasks')
    def create_task(p: TaskInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.create_task(store, db, actor, p.model_dump()))

    @app.post('/api/tasks/{key}/state')
    def change_task(key: str, p: TaskState, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.change_task(store, db, actor, key, p.model_dump()))

    @app.post('/api/tasks/{key}/content-draft')
    def content_draft(key: str, p: ContentInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.content_draft(store, db, actor, key, p.model_dump()))

    @app.post('/api/tasks/{key}/deliver')
    def deliver(key: str, p: DeliverInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.deliver(store, db, actor, key, p.model_dump()))

    @app.post('/api/tasks/{key}/activate')
    def activate(key: str, p: EffectiveInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.activate(store, db, actor, key, p.model_dump()))

    @app.get('/api/observations')
    def observations(actor=Depends(identity)):
        supervisor(actor)
        with store.transaction() as db:
            return envelope(store.all(db, 'observations'))

    @app.post('/api/tasks/{key}/observations')
    def observe(key: str, p: ObservationInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(imp.observe(store, db, actor, key, p.model_dump()))

    @app.get('/api/eval-cases')
    def eval_cases(actor=Depends(identity)):
        supervisor(actor)
        with store.transaction() as db:
            return envelope(store.all(db, 'eval_cases'))

    @app.post('/api/eval-cases')
    def bad_case(p: BadCaseInput, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(ev.bad_case(store, db, actor, p.model_dump()))

    @app.post('/api/eval-cases/import-improvement-candidates')
    @app.post('/api/eval-cases/import-s2-candidates', include_in_schema=False)
    def seed_cases(actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(ev.seed_dataset(store, db, actor))

    @app.post('/api/eval-cases/import-complex-candidates')
    def seed_complex_cases(actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(ev.seed_complex_dataset(store,db,actor))

    @app.post('/api/eval-cases/{key}/review')
    def review_case(key: str, p: CaseReview, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(ev.review_case(store, db, actor, key, p.model_dump()))

    @app.post('/api/eval-cases/{key}/revise')
    def revise_case(key: str, p: CaseRevision, actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(ev.revise_case(store,db,actor,key,p.model_dump()))

    @app.get('/api/eval-runs')
    def eval_runs(actor=Depends(identity)):
        supervisor(actor)
        with store.transaction() as db:
            return envelope(store.all(db, 'eval_runs'))

    @app.post('/api/eval-runs')
    def run_dataset(actor=Depends(identity)):
        with store.transaction() as db:
            return envelope(ev.run_dataset(store, db, actor))
