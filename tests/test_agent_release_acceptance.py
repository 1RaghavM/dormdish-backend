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
