import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
import pytest

SPEC = importlib.util.spec_from_file_location('oidc_deploy', Path(__file__).resolve().parents[1]/'_lib/oidc_deploy.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    archive = tmp_path/'image.tar'
    archive.write_bytes(b'abcdefghij')
    env = {'PLUGLAYER_API_URL': 'https://api.example.com', 'PLUGLAYER_APP_ID': 'app',
        'PLUGLAYER_CONNECTION_ID': 'app', 'PLUGLAYER_ARCHIVE_PATH': str(archive),
        'PLUGLAYER_IMAGE_TAG': 'commit', 'PLUGLAYER_OIDC_AUDIENCE': 'https://api.example.com/github-actions',
        'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://pipelines.actions.githubusercontent.com/idtoken?job=1',
        'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'runner-secret', 'GITHUB_OUTPUT': str(tmp_path/'output')}
    monkeypatch.setattr(helper.time, 'sleep', lambda _: None)
    return helper.Deployment(env)


def test_oidc_exchange_renewal_and_retry(deployment, monkeypatch, capsys):
    calls, masks = [], []
    clock = [0]
    monkeypatch.setattr(helper.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(helper, 'mask', masks.append)
    failures = [True]
    def transport(url, method='GET', body=None, headers=None, timeout=90):
        calls.append((url, method, body, headers))
        if 'pipelines.actions' in url:
            assert 'audience=https%3A%2F%2Fapi.example.com%2Fgithub-actions' in url
            return {'value': 'github-identity'}
        if url.endswith('/token'):
            assert json.loads(body)['identity_token'] == 'github-identity'
            return {'app_id': 'app', 'access_token': 'plci_secret', 'expires_in': 900}
        if failures:
            failures.pop()
            raise HTTPError(url, 503, 'unavailable', {}, None)
        assert headers['Authorization'] == 'Bearer plci_secret'
        return {'data': {'status': 'completed'}}
    monkeypatch.setattr(deployment, 'transport', transport)
    assert deployment.request('/tasks/task', retry=True)['status'] == 'completed'
    clock[0] = 850
    deployment.request('/tasks/task')
    assert sum(url.endswith('/token') for url, *_ in calls) == 2
    assert masks == ['github-identity', 'plci_secret']*2
    assert 'plci_secret' not in capsys.readouterr().out


def test_chunk_acknowledgement_and_lost_completion_response(deployment, monkeypatch):
    calls = []
    def request(path, method='GET', payload=None, raw=None, headers=None, **kwargs):
        calls.append((path, method))
        if path == '/uploads':
            return {'upload_id': 'upload', 'chunk_size': 4}
        if '/chunks/' in path:
            assert kwargs['retry'] is True
            return {'received_bytes': int(headers['X-Upload-Offset'])+len(raw)}
        if path.endswith('/complete'):
            raise URLError('response lost after server accepted completion')
        if path == '/uploads/upload':
            return {'status': 'completed', 'task_id': 'task'}
        return {'status': 'completed'}
    monkeypatch.setattr(deployment, 'request', request)
    deployment.deploy()
    assert calls.count(('/uploads/upload/complete', 'POST')) == 1
    assert len([p for p, _ in calls if '/chunks/' in p]) == 3
    assert Path(deployment.env['GITHUB_OUTPUT']).read_text() == 'task_id=task\nfinal_status=completed\n'


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_failed_rollout_fails_action(deployment, monkeypatch, status):
    def request(path, *args, **kwargs):
        if path == '/uploads': return {'upload_id': 'upload', 'chunk_size': 100}
        if '/chunks/' in path: return {'received_bytes': 10}
        if path.endswith('/complete'): return {'task_id': 'task'}
        return {'status': status}
    monkeypatch.setattr(deployment, 'request', request)
    with pytest.raises(RuntimeError, match='Deployment failed'):
        deployment.deploy()
    assert 'final_status='+status in Path(deployment.env['GITHUB_OUTPUT']).read_text()


def test_unauthorized_identity_and_redirect_rejected(deployment):
    deployment.env['ACTIONS_ID_TOKEN_REQUEST_URL'] = 'https://attacker.invalid/token'
    with pytest.raises((ValueError, RuntimeError)):
        deployment.authenticate()
    assert helper.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://attacker.invalid') is None


def test_non_idempotent_request_never_retried(deployment, monkeypatch):
    deployment.token, deployment.token_until = 'secret', float('inf')
    calls = []
    def transport(*args, **kwargs):
        calls.append(1)
        raise URLError('lost response')
    monkeypatch.setattr(deployment, 'transport', transport)
    with pytest.raises(URLError):
        deployment.request('/uploads', 'POST', {'size_bytes': 10})
    assert len(calls) == 1
