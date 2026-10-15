"""A source checkout can create its runtime without bundled databases."""
import importlib.util
from pathlib import Path

from app.store import Store


def test_generated_bootstrap_is_private_persistent_and_database_specific(tmp_path, monkeypatch):
    import json
    import app.main as main
    from fastapi.testclient import TestClient
    from app.model_config import ModelConfig, bootstrap_tokens, credential_file
    monkeypatch.setattr(main, 'local_values', lambda: {})
    database = tmp_path/'random-auth.sqlite3'
    application = main.create_app(database, serve_frontend=False, model_config=ModelConfig(), seed_mode='empty')
    tokens = json.loads(credential_file(database).read_text(encoding='utf-8'))
    assert len(set(tokens.values())) == 2 and all(len(value) >= 40 for value in tokens.values())
    assert bootstrap_tokens(database, {}) == tokens
    assert bootstrap_tokens(tmp_path/'other.sqlite3', {}) != tokens
    with TestClient(application) as client:
        assert client.get('/api/merchant/setup', headers={'Authorization':'Bearer '+tokens['supervisor']}).status_code == 200
        assert client.get('/api/merchant/setup', headers={'Authorization':'Bearer invalid-supervisor-token'}).status_code == 403


def test_normal_startup_keeps_business_tables_empty(tmp_path, monkeypatch):
    import app.main as main
    from app.model_config import ModelConfig
    monkeypatch.setattr(main, 'local_values', lambda: {})
    app = main.create_app(tmp_path/'fresh.sqlite3', serve_frontend=False, model_config=ModelConfig())
    with app.state.store.transaction() as db:
        for table in ('products', 'policies', 'orders', 'tickets', 'order_grants'):
            assert app.state.store.all(db, table) == []
        assert app.state.store.all(db, 'merchant_settings')[0]['initialized'] is False


def launcher():
    spec=importlib.util.spec_from_file_location('source_startup',Path(__file__).parents[1]/'scripts/run.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_example_and_empty_databases_are_initialized_separately(tmp_path,monkeypatch):
    module=launcher()
    monkeypatch.setattr(module,'ROOT',tmp_path)
    module.initialize_database('example.sqlite3','test')
    module.initialize_database('empty.sqlite3','empty')
    example=Store(tmp_path/'example.sqlite3')
    empty=Store(tmp_path/'empty.sqlite3')
    with example.transaction() as db:
        assert example.all(db,'products') and example.all(db,'orders')
        assert {m['role'] for m in example.all(db,'members')}=={'agent','supervisor'}
    with empty.transaction() as db:
        assert empty.all(db,'products')==[] and empty.all(db,'orders')==[]
    from tests.catalog_builder import install_catalog
    assert install_catalog(example)['products']==30


def test_startup_does_not_reseed_existing_business_database(tmp_path,monkeypatch):
    module=launcher()
    monkeypatch.setattr(module,'ROOT',tmp_path)
    path=tmp_path/'existing.sqlite3'
    store=Store(path)
    with store.transaction() as db:
        store.put(db,'merchant_settings','shop',dict(id='shop',shop_id='CUSTOM-SHOP'))
    module.initialize_database(path.name,'test')
    with store.transaction() as db:
        assert store.all(db,'products')==[]
        assert store.all(db,'merchant_settings')[0]['shop_id']=='CUSTOM-SHOP'

def test_launcher_rebuilds_changed_sources_but_reuses_current_build(tmp_path,monkeypatch):
    import os
    module=launcher()
    monkeypatch.setattr(module,'ROOT',tmp_path)
    source=tmp_path/'frontend/src/main.tsx';source.parent.mkdir(parents=True)
    source.write_text('export {}',encoding='utf-8')
    assert module.frontend_needs_build()
    entry=tmp_path/'frontend/dist/index.html';entry.parent.mkdir(parents=True)
    entry.write_text('<div id="root"></div>',encoding='utf-8')
    os.utime(source,ns=(1_000_000_000,1_000_000_000));os.utime(entry,ns=(2_000_000_000,2_000_000_000))
    assert not module.frontend_needs_build()
    os.utime(source,ns=(3_000_000_000,3_000_000_000))
    assert module.frontend_needs_build()


def test_launcher_rebuilds_changed_frontend_configuration(tmp_path,monkeypatch):
    import os
    module=launcher();monkeypatch.setattr(module,'ROOT',tmp_path)
    entry=tmp_path/'frontend/dist/index.html';entry.parent.mkdir(parents=True)
    entry.write_text('built',encoding='utf-8')
    config=tmp_path/'frontend/tsconfig.json';config.write_text('{}',encoding='utf-8')
    os.utime(entry,ns=(1_000_000_000,1_000_000_000));os.utime(config,ns=(2_000_000_000,2_000_000_000))
    assert module.frontend_needs_build()
