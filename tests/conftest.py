"""Order-scope tests use an ordinary agent, never a product test role."""
import os
import pytest

# Business fixtures are opt-in and confined to regression runs.
os.environ['SELLERSUPPORT_SEED_MODE'] = 'test'
os.environ['SELLERSUPPORT_AGENT_TOKEN'] = 'test-agent-token'
os.environ['SELLERSUPPORT_SUPERVISOR_TOKEN'] = 'test-supervisor-token'
from app import merchant


@pytest.fixture(autouse=True)
def limited_order_agent(request, monkeypatch):
    if request.module.__name__.split('.')[-1] not in {
        'test_tickets', 'test_improvement', 'test_validation_setup', 'test_handoffs',
        'test_reships_attachments', 'test_translation',
    }:
        return
    original = merchant.seed_members

    def seed(store, identities, empty=False):
        original(store, identities, empty)
        with store.transaction() as db:
            if any(m['id'] == 'test-limited' for m in store.all(db, 'members')):
                return
            store.put(db, 'members', 'test-limited', dict(id='test-limited',
                label='Order scope test agent', role='agent', enabled=True,
                token_hash=merchant.digest('test-limited-local')))
            store.put(db, 'order_grants', 'test-limited:TEST-O1', dict(
                id='test-limited:TEST-O1', member_id='test-limited',
                order_id='TEST-O1', version=1, enabled=True))

    monkeypatch.setattr(merchant, 'seed_members', seed)
