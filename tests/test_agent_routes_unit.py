"""Focused endpoint self-checks; the companion PR owns independent acceptance cases."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from dormdish_agent import routes


def test_rollout_is_off_and_invalid_configuration_fails_closed(monkeypatch):
    user = str(uuid4())
    for name in ('DORMDISH_AGENT_ENABLED', 'DORMDISH_AGENT_KILL_SWITCH', 'DORMDISH_AGENT_ROLLOUT_PERCENT'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(HTTPException): routes.admit(user)
    monkeypatch.setenv('DORMDISH_AGENT_ENABLED', 'true')
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '100')
    routes.admit(user)
    monkeypatch.setenv('DORMDISH_AGENT_KILL_SWITCH', 'true')
    with pytest.raises(HTTPException): routes.admit(user)
    monkeypatch.setenv('DORMDISH_AGENT_KILL_SWITCH', 'false')
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '101')
    with pytest.raises(HTTPException): routes.admit(user)


def test_kill_switch_keeps_owner_reconciliation_and_deletion(monkeypatch):
    user, conversation = str(uuid4()), str(uuid4())
    calls = []
    class Store:
        def __init__(self, client, owner): assert owner == user
        def get_conversation(self, value):
            calls.append(('get', str(value)))
            return {'id': str(value)}
        def delete_conversation(self, value): calls.append(('delete', str(value)))
    monkeypatch.setattr(routes, 'AgentStore', Store)
    monkeypatch.setenv('DORMDISH_AGENT_KILL_SWITCH', 'true')
    app = FastAPI()
    app.include_router(routes.create_router(lambda: {'id': user}, lambda: {'id': user}, lambda: None, lambda u: None))
    with TestClient(app) as client:
        assert client.post('/agent/conversations', json={}).status_code == 503
        assert client.get('/agent/conversations/' + conversation).status_code == 200
        assert client.delete('/agent/conversations/' + conversation).status_code == 204
    assert calls == [('get', conversation), ('delete', conversation)]


def test_legacy_save_cannot_update_another_owner(monkeypatch):
    import app
    writes = []
    class Query:
        data = []
        def select(self, *args): return self
        def eq(self, *args): return self
        def limit(self, *args): return self
        def execute(self): return self
        def update(self, *args): writes.append(args); return self
        def upsert(self, *args): writes.append(args); return self
    monkeypatch.setattr(app, 'supabase', SimpleNamespace(table=lambda _: Query()))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(app.save_meal_plan(app.SaveMealPlanRequest(plan_id=str(uuid4()),
            dining_hall='busch-dining-hall', date='2026-10-10', meal_plan_data={}),
            None, {'id': str(uuid4())}))
    assert caught.value.status_code == 404
    assert writes == []


def test_oversized_agent_body_rejected_before_storage(monkeypatch):
    app = FastAPI()
    app.include_router(routes.create_router(lambda: {}, lambda: {}, lambda: None, lambda u: None))
    with TestClient(app) as client:
        assert client.post('/agent/conversations', content=b'x' * 32769).status_code == 413


def test_missing_planning_context_persists_clarification_and_duplicate_replays(monkeypatch):
    from dormdish_agent.models import RunState
    user, conversation, request = str(uuid4()), uuid4(), uuid4()
    state = RunState(id=request, conversation_id=conversation, status='running')
    calls = []
    class Store:
        def __init__(self, client, owner): assert owner == user
        def claim_turn(self, cid, body):
            calls.append('claim')
            assert cid == conversation and body.request_id == request
            return state.status == 'running', state
        def load_history(self, cid): calls.append('history'); return []
        def finish_turn(self, run, history, text):
            calls.append('finish')
            assert run.status == 'complete' and history == [] and 'dining hall' in text
            return run
    monkeypatch.setattr(routes, 'AgentStore', Store)
    monkeypatch.setattr(routes, 'admit', lambda _: None)
    app = FastAPI()
    app.include_router(routes.create_router(lambda: {'id': user}, lambda: {'id': user}, lambda: None, lambda u: None))
    with TestClient(app) as client:
        first = client.post(f'/agent/conversations/{conversation}/messages', json={'request_id': str(request), 'text': 'hello'})
        duplicate = client.post(f'/agent/conversations/{conversation}/messages', json={'request_id': str(request), 'text': 'hello'})
    assert first.status_code == 200
    assert first.content == duplicate.content
    assert calls == ['claim', 'history', 'finish', 'claim']


def test_stream_persists_before_terminal_and_does_not_retry_ambiguous_finish(monkeypatch):
    from dormdish_agent.models import RunState, TurnRequest
    saved, states = [], []
    class Client:
        def __init__(self, **kw): pass
        async def close(self): pass
    class Turn:
        def __init__(self, context, request, conversation_id, history, **kw):
            self.state = RunState(id=request.request_id, conversation_id=conversation_id, status='running')
            self.history, self.assistant_text = [], 'Hello'
        async def stream(self):
            yield routes.append_event(self.state, 'text_delta', {'text': 'Hello'})
            yield routes.append_event(self.state, 'complete', {'citations': []})
    class Store:
        def __init__(self, fail): self.fail = fail
        def finish_turn(self, state, *args):
            saved.append(state.status)
            if self.fail: raise routes.StorageError('unavailable')
            return state
    monkeypatch.setattr(routes, 'AsyncOpenAI', Client)
    monkeypatch.setattr(routes, 'AgentTurn', Turn)
    async def run(fail):
        state = RunState(id=uuid4(), conversation_id=uuid4(), status='running')
        frames = []
        async for event in routes.run_stream(Store(fail), state, TurnRequest(request_id=state.id, text='hi'),
                                             SimpleNamespace(proposal=None), [], None):
            frames.append(event)
            if b'"type":"complete"' in event: assert saved == ['complete']
        return frames
    assert len(asyncio.run(run(False))) == 2
    saved.clear()
    assert len(asyncio.run(run(True))) == 1
    assert saved == ['complete']


def test_stream_disconnect_persists_error(monkeypatch):
    from dormdish_agent.models import RunState, TurnRequest
    saved = []
    class Client:
        def __init__(self, **kw): pass
        async def close(self): pass
    class Turn:
        def __init__(self, context, request, cid, history, **kw):
            self.state = RunState(id=request.request_id, conversation_id=cid, status='running')
        async def stream(self):
            yield routes.append_event(self.state, 'text_delta', {'text': 'Partial'})
    class Store:
        def finish_turn(self, state, *args): saved.append(state); return state
    monkeypatch.setattr(routes, 'AsyncOpenAI', Client)
    monkeypatch.setattr(routes, 'AgentTurn', Turn)
    async def run():
        state = RunState(id=uuid4(), conversation_id=uuid4(), status='running')
        stream = routes.run_stream(Store(), state, TurnRequest(request_id=state.id, text='hi'), None, [], None)
        await anext(stream)
        await stream.aclose()
    asyncio.run(run())
    assert len(saved) == 1 and saved[0].status == 'error'


def test_profile_restrictions_do_not_silently_disappear():
    from dormdish_agent.models import Nutrition, TurnRequest
    class DB:
        data = []
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def limit(self, *_): return self
        def execute(self): return self
    request = TurnRequest(request_id=uuid4(), text='Dinner', context={
        'dining_hall': 'busch', 'date': '2026-10-10', 'meal_type': 'dinner'})
    profile = {'id': str(uuid4()), 'university': 'rutgers', 'dietary_restrictions': ['Dairy Free', 'Vegetarian']}
    targets = lambda _: Nutrition(calories=2000, protein_g=100, fat_g=60, carbs_g=220)
    context = routes.build_context(DB(), profile, request, targets)
    assert context.allergens == ('milk',) and context.restrictions == ('vegetarian',)
    profile['dietary_restrictions'] = ['mystery allergy']
    with pytest.raises(ValueError): routes.build_context(DB(), profile, request, targets)


def test_action_keeps_other_meals_and_sums_the_whole_plan():
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    import json
    from dormdish_agent.models import PendingAction, Nutrition
    proposal = PendingAction.model_validate(json.loads((Path(__file__).resolve().parents[1] /
        'contracts/agent-v1.json').read_text())['conversation']['pending_actions'][0]).proposal
    old = {'description': 'Preserved breakfast', 'items': [{'name': 'Egg', 'calories': 100,
        'protein_g': 10, 'fat_g': 5, 'carbs_g': 3}]}
    target = {'plan': {'id': str(uuid4()), 'meal_plan_data': {'meal': 'breakfast', 'generation_id': 'old',
        'meal_plan': {'meals': {'breakfast': old}}}}, 'version': 'opaque'}
    context = SimpleNamespace(proposal=proposal, targets=Nutrition(calories=2000, protein_g=100, fat_g=60, carbs_g=200),
                              snapshot=SimpleNamespace(expires_at=datetime.now(timezone.utc) + timedelta(hours=2)))
    stored = routes.stored_action(context, target)
    assert stored.save_payload['meal_plan']['meals']['breakfast'] == old
    assert stored.save_payload['meal_plan']['totals']['calories'] == 100 + proposal.totals.calories
    assert 'meal' not in stored.save_payload and 'generation_id' not in stored.save_payload
    assert target['plan']['meal_plan_data']['meal'] == 'breakfast'
    assert stored.expected_plan_version == 'opaque'


from tests.test_agent_storage_unit import pg, stores, prepared_action, confirmation


@pytest.fixture
def policy_stores(stores, pg):
    from pathlib import Path
    pg.sql((Path(__file__).resolve().parents[1] / 'migrations/enforce_agent_daily_save_policy.sql').read_text())
    return stores


def test_pg_competing_halls_cannot_create_two_daily_plans(policy_stores, pg):
    from tests.test_agent_persistence import parallel
    owner, _ = policy_stores
    actions = [prepared_action(owner)[1] for _ in range(2)]
    pg.sql(f"UPDATE agent_actions SET proposal = jsonb_set(proposal, '{{dining_hall}}', '\"different-hall\"') WHERE id = '{actions[1].id}'")
    outcomes = parallel(*(lambda action=action: owner.confirm_action(action.id, confirmation()) for action in actions))
    assert outcomes.count('stale_plan') == 1
    assert pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{owner.user_id}'") == '1'


def test_pg_replacement_collision_and_hall_change_rejected(policy_stores, pg):
    owner, _ = policy_stores
    _, action = prepared_action(owner)
    saved = owner.confirm_action(action.id, confirmation()).events[-1].data.saved_plan_id
    target = owner.get_saved_plan(saved)
    _, replacement = prepared_action(owner, target=target)
    pg.sql(f"INSERT INTO saved_meal_plans SELECT '{uuid4()}', user_id, 'another-hall', date, meal_plan_data, created_at, updated_at FROM saved_meal_plans WHERE id = '{saved}'")
    with pytest.raises(routes.StorageError, match='stale_plan'):
        owner.confirm_action(replacement.id, confirmation())
    pg.sql(f"DELETE FROM saved_meal_plans WHERE user_id = '{owner.user_id}' AND id <> '{saved}'")
    pg.sql(f"UPDATE agent_actions SET proposal = jsonb_set(proposal, '{{dining_hall}}', '\"different-hall\"') WHERE id = '{replacement.id}'")
    with pytest.raises(routes.StorageError, match='stale_plan'):
        owner.confirm_action(replacement.id, confirmation())
    assert owner.get_saved_plan(saved) == target
