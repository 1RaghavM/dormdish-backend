"""Cross-feature release gates, using the existing real PostgreSQL/SDK fixtures."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dormdish_agent import legacy
from tests.test_agent_routes import api, route_pg, pg, stores, install_model, turn_body, events  # noqa: F401
from tests.test_controlled_save import plans
from tests.test_legacy_adapters_unit import turns, HINT  # noqa: F401


@pytest.fixture(scope='module')
def save_schema(route_pg):
    route_pg.sql((Path(__file__).resolve().parents[1] / 'migrations/add_agent_controlled_save.sql').read_text())


@pytest.mark.usefixtures('save_schema')
@pytest.mark.parametrize('flag,value', [
    ('DORMDISH_AGENT_KILL_SWITCH', 'true'),
    ('DORMDISH_AGENT_ENABLED', 'false'),
    ('DORMDISH_AGENT_ROLLOUT_PERCENT', '0'),
])
def test_rollback_preserves_saved_receipt_and_owner_only_recovery(api, monkeypatch, flag, value):
    monkeypatch.setenv('DORMDISH_CONTROLLED_SAVE_ENABLED', 'true')
    requests = install_model(monkeypatch)
    conversation = api.owner.create_conversation().id
    body = turn_body(text='Generate and save dinner')
    response = api.client.post(f'/agent/conversations/{conversation}/messages', json=body)
    assert response.status_code == 200
    receipt = events(response)
    before = plans(api)
    assert len(before) == 1 and receipt[-1]['data']['saved_plan_id'] == before[0]['id']
    monkeypatch.setenv(flag, value)

    # An interrupted client can recover a committed save even after emergency rollback.
    recovered = api.client.get('/agent/runs/' + body['request_id'])
    assert recovered.status_code == 200 and recovered.json()['events'] == receipt
    restored = api.client.get(f'/agent/conversations/{conversation}')
    assert restored.status_code == 200 and restored.json()['pending_actions'] == []
    assert api.client.post('/agent/conversations', json={}).status_code == 503
    assert api.client.post(f'/agent/conversations/{conversation}/messages', json=turn_body()).status_code == 503
    assert api.client.post(f'/agent/actions/{uuid4()}/confirm', json={
        'request_id': str(uuid4()), 'proposal_version': 1}).status_code == 503
    assert len(requests) == 3 and plans(api) == before

    api.client.headers['Authorization'] = 'Bearer other'
    assert api.client.get('/agent/runs/' + body['request_id']).status_code == 404
    assert api.client.get(f'/agent/conversations/{conversation}').status_code == 404
    assert api.client.delete(f'/agent/conversations/{conversation}').status_code == 404
    api.client.headers['Authorization'] = 'Bearer owner'
    assert api.client.delete(f'/agent/conversations/{conversation}').status_code == 204
    assert api.client.get('/agent/runs/' + body['request_id']).status_code == 404
    assert plans(api) == before  # Conversation retention never deletes the saved meal.


def test_full_day_context_loading_consumes_shared_deadline(turns, monkeypatch):
    stub = turns(model_calls=4)
    now = [100.0]
    original = legacy.build_context

    def load(*args):
        now[0] += 20
        return original(*args)

    monkeypatch.setattr(legacy, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(legacy, 'build_context', load)

    async def run():
        seen = []
        with pytest.raises(ValueError, match='Generation budget exhausted'):
            async for event in legacy.events(None, {}, None, 'busch', '2026-10-12',
                                              {meal: HINT for meal in ('breakfast', 'lunch', 'dinner')}):
                seen.append(event)
        return seen

    chunks = asyncio.run(run())
    assert [entry[0] for entry in stub.seen] == ['breakfast', 'lunch']
    assert [entry[3] for entry in stub.seen] == [25, 5]
    assert all(chunk['type'] == 'thinking' for chunk in chunks)


def test_smoke_defaults_offline_with_real_tools_even_when_credentials_exist(monkeypatch):
    from scripts import agent_release_smoke as smoke

    def network_forbidden(*args, **kwargs):
        raise AssertionError('Offline smoke must not construct a network transport')

    monkeypatch.setenv('OPENAI_API_KEY', 'private-test-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://must-not-be-used.invalid')
    monkeypatch.setattr(smoke.httpx, 'AsyncHTTPTransport', network_forbidden)
    report = asyncio.run(smoke.run_smoke())
    assert report['outcome'] == 'passed' and report['error_code'] is None
    assert report['mode'] == 'offline' and report['usage_is_synthetic'] is True
    assert (report['requests'], report['responses_with_usage'], report['tool_calls'], report['previews']) == (5, 5, 4, 1)
    assert (report['input_tokens'], report['output_tokens'], report['total_tokens']) == (100, 50, 150)
    assert 'private-test-key' not in str(report)


@pytest.mark.parametrize('deadline', [0, -1, 46, float('nan'), float('inf')])
def test_smoke_rejects_invalid_deadline_before_model(deadline):
    from scripts.agent_release_smoke import run_smoke
    report = asyncio.run(run_smoke(deadline_seconds=deadline))
    assert report['outcome'] == 'error' and report['error_code'] == 'invalid_deadline'
    assert report['requests'] == 0


def test_live_smoke_requires_key_without_starting_transport(monkeypatch):
    from scripts import agent_release_smoke as smoke
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    report = asyncio.run(smoke.run_smoke(live=True))
    assert report['outcome'] == 'error' and report['error_code'] == 'missing_api_key'
    assert report['requests'] == 0


def test_live_smoke_cancels_stalled_transport_within_deadline(monkeypatch):
    import httpx
    from scripts import agent_release_smoke as smoke
    cancelled = []

    async def stall(request):
        assert str(request.url) == 'https://api.openai.com/v1/responses'
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)

    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-key-never-sent')
    monkeypatch.setattr(smoke.httpx, 'AsyncHTTPTransport', lambda **_: httpx.MockTransport(stall))
    report = asyncio.run(smoke.run_smoke(live=True, deadline_seconds=0.05))
    assert report['outcome'] == 'error' and report['error_code'] == 'deadline'
    assert report['requests'] == 1 and report['previews'] == 0
    assert cancelled == [True] and report['duration_seconds'] < 2


@pytest.mark.parametrize('broken', ['missing_usage', 'wrong_portion', 'missing_profile', 'malformed_stream'])
def test_smoke_does_not_report_success_for_incomplete_or_corrupt_scenario(monkeypatch, broken):
    import json
    import httpx
    from scripts import agent_release_smoke as smoke
    original = smoke.offline_response

    def response(context, number):
        normal = original(context, number)
        frame = json.loads(normal.read().decode().split('data: ', 1)[1])
        payload = frame['response']
        if broken == 'missing_usage':
            payload.pop('usage')
        elif broken == 'wrong_portion' and number == 4:
            arguments = json.loads(payload['output'][0]['arguments'])
            arguments['selections'][0]['servings'] = 2
            payload['output'][0]['arguments'] = json.dumps(arguments)
        elif broken == 'missing_profile' and number == 1:
            payload['output'][0]['name'] = 'read_intake'
        data = 'private-malformed-stream-secret' if broken == 'malformed_stream' else json.dumps(frame)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              stream=httpx.ByteStream(('data: ' + data + '\n\n').encode()))

    monkeypatch.setattr(smoke, 'offline_response', response)
    report = asyncio.run(smoke.run_smoke())
    assert report['outcome'] == 'error' and report['error_code']
    if broken != 'malformed_stream':
        assert report['error_code'] == 'scenario_not_satisfied'
    assert 'private-malformed-stream-secret' not in json.dumps(report)


def test_smoke_usage_survives_byte_boundaries_and_multiple_response_frames():
    import json
    import httpx
    from scripts.agent_release_smoke import MeasuredStream
    frame = {'type': 'response.completed', 'response': {'usage': {
        'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10}, 'ignored_text': '🌶'}}
    wire = (': keepalive\n' + 'data: ' + json.dumps(frame, ensure_ascii=False) + '\n\n') * 2 + 'data: [DONE]\n\n'
    encoded = wire.encode()

    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            for byte in encoded:
                yield bytes([byte])

    metrics = dict(input_tokens=0, output_tokens=0, total_tokens=0, responses_with_usage=0)

    async def read():
        return b''.join([chunk async for chunk in MeasuredStream(Bytes(), metrics)])

    assert asyncio.run(read()) == encoded
    assert metrics == dict(input_tokens=14, output_tokens=6, total_tokens=20, responses_with_usage=2)
