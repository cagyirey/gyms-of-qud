import json
import socket
import threading
import time
import http.client
import pytest
import uvicorn
from qudgym import MockBackend, QudEnv
from qudgym.client import HttpBackend
from qudgym.errors import TransportUncertain
from qudgym.rpc import RpcService
from qudgym.server import create_app

TOKEN = 'b' * 48


@pytest.mark.parametrize('url', ['http://example.com/rpc', 'https://127.0.0.1/rpc',
                                 'http://127.0.0.1/not-rpc', 'http://127.0.0.1/rpc?x=y',
                                 'http://user:pass@127.0.0.1/rpc', 'http://127.0.0.1/rpc#x'])
def test_remote_or_ambiguous_endpoints_rejected(url):
    with pytest.raises(ValueError):
        HttpBackend(url, token=TOKEN)


def test_short_token_rejected():
    with pytest.raises(ValueError):
        HttpBackend('http://127.0.0.1:8765/rpc', token='short')


def test_transport_failure_is_never_retried(monkeypatch):
    calls = []
    class Broken:
        def __init__(self, *args, **kwargs):
            calls.append(1)
        def request(self, *args, **kwargs):
            raise TimeoutError()
        def close(self):
            pass
    monkeypatch.setattr(http.client, 'HTTPConnection', Broken)
    backend = HttpBackend('http://127.0.0.1:8765/rpc', token=TOKEN)
    with pytest.raises(TransportUncertain):
        backend.reset()
    assert len(calls) == 1


def test_reply_request_id_must_match(monkeypatch):
    class Reply:
        status = 200
        def read(self, size):
            return json.dumps({'request_id':'wrong', 'result':{'ok':True}}).encode()
    class Connection:
        def __init__(self, *a, **kw): pass
        def request(self, *a, **kw): pass
        def getresponse(self): return Reply()
        def close(self): pass
    monkeypatch.setattr(http.client, 'HTTPConnection', Connection)
    with pytest.raises(TransportUncertain):
        HttpBackend('http://127.0.0.1/rpc', token=TOKEN).capabilities()


def test_real_loopback_http_episode_and_snapshot():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(RpcService(MockBackend(allow_oracle=True)), token=TOKEN),
                                           log_level='critical', lifespan='off', ws='none'))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if time.monotonic() >= deadline:
                pytest.fail('Loopback reference server failed to start')
            time.sleep(.01)
        with QudEnv(HttpBackend(f'http://127.0.0.1:{port}/rpc', token=TOKEN)) as env:
            assert env.backend.capabilities().is_mock
            env.reset(seed=7)
            root = env.backend.snapshot()
            original_hash = env.backend.state_hash()
            env.step('wait')
            env.restore(root.handle)
            assert env.backend.state_hash() == original_hash
            env.backend.release(root.handle)
            for action in ('move:E','move:E','answer:open','move:E','move:E'):
                result = env.step(action)
            assert result.metrics.outcome == 'success'
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_invalid_result_body_is_uncertain_not_a_false_transition(monkeypatch):
    backend = HttpBackend('http://127.0.0.1/rpc', token=TOKEN)
    monkeypatch.setattr(backend, '_call', lambda operation: {'unrecognized': 'result'})
    with pytest.raises(TransportUncertain):
        backend.reset()
