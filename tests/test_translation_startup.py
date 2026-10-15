import importlib.util
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.model_config import ModelConfig


def test_translation_readiness_contract_and_uncached_entry(tmp_path):
    client = TestClient(create_app(tmp_path/'startup.sqlite3', model_config=ModelConfig(api_key='test-key')))
    health = client.get('/api/health').json()['data']
    assert health['translation_api_version'] == 1
    assert health['workflow_api_version'] == 3
    assert health['knowledge_catalog_api_version'] == 2
    paths = client.get('/openapi.json').json()['paths']
    assert 'post' in paths['/api/tickets/{key}/translation']
    assert 'post' in paths['/api/translation-preview']
    # The launcher may serve a fresh hashed bundle after a backend upgrade.
    assert client.get('/').headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('version,workflow,catalog,expected', [(None,2,2,False),(0,2,2,False),(1,None,2,False),(1,1,2,False),(1,3,None,False),(1,3,1,False),(1,3,2,True)])
def test_launcher_rejects_backend_without_current_translation_contract(monkeypatch, version, workflow, catalog, expected):
    spec = importlib.util.spec_from_file_location('translation_startup_launcher',Path(__file__).parents[1]/'scripts/run.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    class Response(io.BytesIO):
        status = 200
    payload = {'data':{'status':'ok','model_experiment_enabled':False}}
    if version is not None:
        payload['data']['translation_api_version'] = version
    if workflow is not None:
        payload['data']['workflow_api_version'] = workflow
    if catalog is not None:
        payload['data']['knowledge_catalog_api_version'] = catalog
    def urlopen(url, timeout):
        return Response(json.dumps(payload).encode() if url.endswith('/api/health') else b'index')
    monkeypatch.setattr(launcher,'urlopen',urlopen)
    assert launcher.ready(8020) is expected
