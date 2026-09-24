from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from qudgym import MockBackend
from qudgym.models import RpcRequest, Reset, Step, Hello, Observe
from qudgym.rpc import RpcService
from qudgym.server import create_app

TOKEN = 'a' * 48


def req(id, op):
    return RpcRequest(request_id=id, operation=op)


def test_duplicate_step_is_applied_once_even_concurrently():
    backend = MockBackend()
    service = RpcService(backend)
    service.handle(req('reset', Reset()))
    request = req('step', Step(action_id='wait', decision_id=backend.observe().decision_id))
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(service.handle, [request] * 40))
    assert len({r.model_dump_json() for r in replies}) == 1
    assert backend.observe().turn == 1


def test_request_id_reuse_with_different_operation_is_rejected():
    service = RpcService(MockBackend())
    service.handle(req('x', Hello()))
    response = service.handle(req('x', Reset()))
    assert response.error.code == 'request_id_conflict'


def test_cached_response_is_not_mutable_by_caller():
    service = RpcService(MockBackend())
    r = req('x', Hello())
    response = service.handle(r)
    response.result['is_mock'] = False
    assert service.handle(r).result['is_mock'] is True


def test_stale_guard_survives_cache_eviction():
    backend = MockBackend()
    service = RpcService(backend, cache_size=1)
    service.handle(req('reset', Reset()))
    step = req('s', Step(action_id='wait', decision_id=backend.observe().decision_id))
    service.handle(step)
    service.handle(req('hello', Hello()))
    assert service.handle(step).error.code == 'stale_decision'
    assert backend.observe().turn == 1


def test_backend_bug_faults_worker_not_game_reward():
    class Broken(MockBackend):
        def step(self, *args, **kwargs):
            raise RuntimeError('secret internal traceback')
    backend = Broken()
    service = RpcService(backend)
    service.handle(req('r', Reset()))
    reply = service.handle(req('s', Step(action_id='wait', decision_id=backend.observe().decision_id)))
    assert reply.error.code == 'internal_error'
    assert 'secret' not in reply.model_dump_json()
    assert service.handle(req('o', Observe())).error.code == 'worker_faulted'
    assert service.handle(req('r2', Reset())).error is None


@pytest.fixture
def client():
    with TestClient(create_app(RpcService(MockBackend()), token=TOKEN)) as client:
        yield client


def test_authentication_is_required(client):
    payload = req('x', Hello()).model_dump(mode='json')
    assert client.post('/rpc', json=payload).status_code == 401
    assert client.post('/rpc', json=payload, headers={'Authorization': 'Bearer wrong'}).status_code == 401
    response = client.post('/rpc', json=payload, headers={'Authorization': f'Bearer {TOKEN}'})
    assert response.status_code == 200
    assert response.json()['result']['is_mock'] is True


def test_browser_origin_rejected_even_with_token(client):
    response = client.post('/rpc', json=req('x', Hello()).model_dump(mode='json'),
                           headers={'Authorization': f'Bearer {TOKEN}', 'Origin': 'https://example.com'})
    assert response.status_code == 403


def test_invalid_protocol_and_size_limits(client):
    headers = {'Authorization': f'Bearer {TOKEN}'}
    assert client.post('/rpc', content=b'{oops', headers=headers).status_code == 422
    assert client.post('/rpc', content=b'x' * 1_048_577, headers=headers).status_code == 413
    assert client.post('/rpc', json={'protocol_version': '99'}, headers=headers).status_code == 422


def test_server_denies_oracle_operations_in_player_mode(client):
    headers = {'Authorization': f'Bearer {TOKEN}'}
    client.post('/rpc', json=req('r', Reset()).model_dump(mode='json'), headers=headers)
    response = client.post('/rpc', json={'request_id':'snap', 'operation':{'op':'snapshot'}}, headers=headers)
    assert response.json()['error']['code'] == 'unsupported'


@pytest.mark.parametrize('seed', [True, 1.5, '7'])
def test_wire_seed_is_strict(seed):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RpcRequest.model_validate({'request_id': 'bad-seed', 'operation': {'op': 'reset', 'seed': seed}})
