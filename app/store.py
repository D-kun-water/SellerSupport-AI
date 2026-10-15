import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from .domain import DomainError, now

ROOT = Path(__file__).resolve().parents[1]
TABLES = ('products', 'sources', 'policies', 'orders', 'tickets', 'proposals', 'runs', 'imports', 'audit', 'model_calls', 'knowledge_revisions',
          'feedback', 'issues', 'issue_operations', 'hypotheses', 'tasks', 'observations', 'eval_cases', 'eval_runs', 'handoffs', 'attachments', 'conversation_imports', 'merchant_settings', 'members', 'order_grants', 'catalog_releases', 'pilot_settings', 'pilot_reports', 's3_sources', 's3_cases', 's3_sessions', 's3_plans', 's3_observations')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def uid(prefix):
    return prefix + '-' + uuid4().hex[:12]


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            for table in TABLES:
                db.execute(f'CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)')
            db.execute('INSERT OR IGNORE INTO schema_version VALUES (1)')
            db.execute('INSERT OR IGNORE INTO schema_version VALUES (2)')
            db.execute('INSERT OR IGNORE INTO schema_version VALUES (3)')
            db.execute('INSERT OR IGNORE INTO schema_version VALUES (4)')
            db.execute('INSERT OR IGNORE INTO schema_version VALUES (5)')
            db.execute('INSERT OR IGNORE INTO schema_version VALUES (6)')
            db.execute('INSERT OR IGNORE INTO schema_version VALUES (7)')
            db.execute('INSERT OR IGNORE INTO schema_version VALUES (8)')
            db.execute('INSERT OR IGNORE INTO schema_version VALUES (9)')
            from .trial_records import archive,migrate_cycles
            for cfg in self.all(db,'pilot_settings'):archive(self,db,cfg)
            migrate_cycles(self,db)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, db, table, key):
        assert table in TABLES
        row = db.execute(f'SELECT payload FROM {table} WHERE id=?', (key,)).fetchone()
        if not row:
            raise DomainError('NOT_FOUND', '未找到对象', 404)
        return json.loads(row[0])

    def all(self, db, table):
        assert table in TABLES
        return [json.loads(x[0]) for x in db.execute(f'SELECT payload FROM {table} ORDER BY rowid')]

    def put(self, db, table, key, obj):
        assert table in TABLES
        db.execute(f'INSERT INTO {table} VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (key, encode(obj)))

    def audit(self, db, actor, action, ref, details=None):
        event = dict(id=uid('AUD'), actor=actor['id'], role=actor['role'], action=action,
                     object_ref=ref, details=details or {}, observed_at=now(), source_type='local')
        self.put(db, 'audit', event['id'], event)

    def seed(self):
        # Insert-only: restarting never overwrites changes or approvals.
        bundle = json.loads((ROOT / 'tests/fixtures/catalog.json').read_text(encoding='utf-8'))
        with self.transaction() as db:
            settings = self.all(db, 'merchant_settings')
            if settings and settings[0].get('shop_id') != 'TEST-SHOP':
                return
            for table in ('products', 'sources', 'policies', 'orders'):
                for obj in bundle[table]:
                    db.execute(f'INSERT OR IGNORE INTO {table} VALUES (?,?)', (obj['id'], encode(obj)))

    def backup(self, target):
        with sqlite3.connect(self.path) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
