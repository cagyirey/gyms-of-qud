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


def _serve(backend):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(RpcService(backend), token=TOKEN),
                                           log_level='critical', lifespan='off', ws='none'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() >= deadline:
            sock.close()
            pytest.fail('Loopback reference server failed to start')
        time.sleep(.01)
    return server, thread, sock, port


class _DropFirstMutation(http.client.HTTPConnection):
    """Deliver the request, then hide the first reply for one operation."""

    target = 'step'
    seen = []

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        payload = json.loads(body)
        self._op = payload['operation']['op']
        self._request_id = payload['request_id']
        if self._op == type(self).target:
            type(self).seen.append(self._request_id)
        return super().request(method, url, body, headers, encode_chunked=encode_chunked)

    def getresponse(self):
        response = super().getresponse()
        if self._op == type(self).target and len(type(self).seen) == 1:
            response.read()
            raise TimeoutError('dropped reply after the server committed')
        return response


def test_dropped_step_reply_is_replayed_with_the_same_request_id(monkeypatch):
    # A second step() must not mint a new id: that wedges on stale_decision.
    monkeypatch.setattr(http.client, 'HTTPConnection', _DropFirstMutation)
    _DropFirstMutation.target = 'step'
    _DropFirstMutation.seen = []
    world = MockBackend()
    server, thread, sock, port = _serve(world)
    try:
        with QudEnv(HttpBackend(f'http://127.0.0.1:{port}/rpc', token=TOKEN)) as env:
            env.reset(seed=1)
            with pytest.raises(TransportUncertain) as lost:
                env.step('wait')
            assert lost.value.request_id == _DropFirstMutation.seen[0]
            assert world.observe().turn == 1
            from qudgym.errors import QudGymError
            with pytest.raises(QudGymError) as stuck:
                env.step('wait')
            assert stuck.value.code == 'reconcile_required'
            assert world.observe().turn == 1
            assert len(_DropFirstMutation.seen) == 1
            recovered = env.replay()
            assert recovered.observation.turn == 1
            assert recovered.metrics.decisions_elapsed == 1
            assert _DropFirstMutation.seen == [lost.value.request_id, lost.value.request_id]
            advanced = env.step('wait')
            assert advanced.observation.turn == 2
            assert _DropFirstMutation.seen[-1] != lost.value.request_id
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_reconcile_adopts_the_server_cursor_and_does_not_invent_a_reward():
    from qudgym.errors import QudGymError, TransportUncertain as Uncertain

    class CommitThenDrop(MockBackend):
        def step(self, action_id, *, decision_id):
            result = super().step(action_id, decision_id=decision_id)
            if not getattr(self, 'dropped', False):
                self.dropped = True
                raise Uncertain('reply dropped')
            return result

    world = CommitThenDrop()
    env = QudEnv(world)
    env.reset(seed=1)
    with pytest.raises(Uncertain):
        env.step('wait')
    assert world.observe().turn == 1
    with pytest.raises(QudGymError) as stuck:
        env.reset(seed=1)
    assert stuck.value.code == 'reconcile_required'
    assert world.observe().turn == 1
    observed = env.reconcile()
    assert observed.decision_id == world.observe().decision_id
    assert env.reward_unknown is True
    assert env.current is None
    nxt = env.step('wait')
    assert nxt.observation.turn == 2
    assert env.reward_unknown is False


def test_dropped_release_is_not_retried_as_a_new_request(monkeypatch):
    from qudgym.errors import QudGymError

    monkeypatch.setattr(http.client, 'HTTPConnection', _DropFirstMutation)
    _DropFirstMutation.target = 'release'
    _DropFirstMutation.seen = []
    world = MockBackend(allow_oracle=True)
    server, thread, sock, port = _serve(world)
    try:
        backend = HttpBackend(f'http://127.0.0.1:{port}/rpc', token=TOKEN)
        backend.reset(seed=1)
        handle = backend.snapshot().handle
        with pytest.raises(TransportUncertain) as lost:
            backend.release(handle)
        assert handle not in world._snapshots
        with pytest.raises(QudGymError) as stuck:
            backend.release(handle)
        assert stuck.value.code == 'reconcile_required'
        assert _DropFirstMutation.seen == [lost.value.request_id]
        from qudgym.models import Released
        assert backend.replay() == Released(released=True)
        assert _DropFirstMutation.seen == [lost.value.request_id, lost.value.request_id]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
