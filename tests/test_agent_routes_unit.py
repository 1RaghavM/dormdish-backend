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
    target = {'plan': {'id': str(uuid4()), 'dining_hall': proposal.dining_hall, 'meal_plan_data': {'meal': 'breakfast', 'generation_id': 'old',
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


@pytest.mark.parametrize('saved_hall', ['busch', 'Busch Dining Hall', 'busch-dining-hall'])
def test_registered_legacy_hall_alias_uses_original_target(saved_hall):
    from dormdish_agent.models import Nutrition, PlanningContext
    class DB:
        data = [{'id': str(uuid4()), 'dining_hall': saved_hall}]
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def limit(self, *_): return self
        def execute(self): return self
    db = DB()
    context = routes.FoodContext(db, str(uuid4()), 'rutgers',
        PlanningContext(dining_hall='busch', date='2026-10-10', meal_type='dinner'),
        Nutrition(calories=2000, protein_g=100, fat_g=60, carbs_g=220))
    snapshot = {'plan': {'id': db.data[0]['id'], 'dining_hall': saved_hall, 'date': '2026-10-10'}, 'version': 'unchanged'}
    owner = SimpleNamespace(client=db, user_id=context.user_id, get_saved_plan=lambda _: snapshot)
    assert routes.read_target(owner, context) is snapshot
    assert snapshot['plan']['dining_hall'] == saved_hall
    for different_hall in ('livingston', 'hopkins-cafe', 'BUSCH', 'invented-busch'):
        snapshot['plan']['dining_hall'] = different_hall
        with pytest.raises(HTTPException) as caught:
            routes.read_target(owner, context)
        assert caught.value.status_code == 409


# A separate database keeps the historical migration selfchecks unchanged.
from tests.test_agent_storage_unit import pg as profile_base_pg


@pytest.fixture(scope='module')
def profile_pg(profile_base_pg):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    profile_base_pg.sql('''ALTER TABLE users ADD university text, ADD dietary_restrictions jsonb,
        ADD custom_calories integer, ADD email text, ADD password_hash text;''')
    for name in ('enforce_agent_daily_save_policy.sql', 'enforce_agent_profile_safety.sql'):
        profile_base_pg.sql((root / 'migrations' / name).read_text())
    return profile_base_pg


@pytest.fixture
def profile_store(profile_pg):
    owner = str(uuid4())
    profile_pg.sql(f"INSERT INTO users(id, university, dietary_restrictions, email, password_hash) VALUES ('{owner}', 'rutgers', '[]', 'private@example.test', 'secret')")
    return routes.AgentStore(profile_pg, owner)


def profile_action(store, *, version=None, before_finish=None):
    from pathlib import Path
    from datetime import datetime, timedelta, timezone
    import json
    from dormdish_agent.models import PendingAction, RunState, TurnRequest
    version = version if version is not None else store.get_profile()['version']
    conversation = store.create_conversation()
    _, run = store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Synthetic dinner'))
    data = json.loads((Path(__file__).resolve().parents[1] / 'contracts/agent-v1.json').read_text())['conversation']['pending_actions'][0]
    data.update(id=str(uuid4()), expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    action = PendingAction.model_validate(data)
    routes.append_event(run, 'action_required', {'action': action.model_dump(mode='json')})
    routes.append_event(run, 'complete', {'citations': []})
    if before_finish:
        before_finish()
    store.finish_turn(run, [], 'Preview', routes.StoredAction(action, {'trusted': True}, expected_profile_version=version))
    return action


def test_pg_profile_snapshot_excludes_secrets_and_unrelated_changes(profile_store, profile_pg):
    snapshot = profile_store.get_profile()
    assert snapshot['profile']['university'] == 'rutgers'
    assert not {'email', 'password_hash', 'id'} & snapshot['profile'].keys()
    assert len(snapshot['version']) == 64
    action = profile_action(profile_store)
    profile_pg.sql(f"UPDATE users SET email = 'changed@example.test', password_hash = 'changed' WHERE id = '{profile_store.user_id}'")
    assert profile_store.get_profile() == snapshot
    assert profile_store.confirm_action(action.id, confirmation()).status == 'complete'


@pytest.mark.parametrize('during_run', [False, True])
def test_pg_changed_profile_never_applies_old_proposal(profile_store, profile_pg, during_run):
    def change():
        profile_pg.sql(f"UPDATE users SET dietary_restrictions = '[\"Dairy Free\"]' WHERE id = '{profile_store.user_id}'")
    if during_run:
        with pytest.raises(routes.StorageError, match='stale_profile'):
            profile_action(profile_store, before_finish=change)
        assert profile_pg.sql(f"SELECT count(*) FROM agent_actions WHERE user_id = '{profile_store.user_id}'") == '0'
        assert profile_pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{profile_store.user_id}'") == '0'
        return
    action = profile_action(profile_store)
    change()
    quota = profile_pg.sql(f"SELECT requests FROM agent_quotas WHERE user_id = '{profile_store.user_id}'")
    request = confirmation()
    with pytest.raises(routes.StorageError, match='stale_profile'):
        profile_store.confirm_action(action.id, request)
    assert profile_pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{profile_store.user_id}'") == '0'
    assert profile_pg.sql(f"SELECT count(*) FROM agent_runs WHERE id = '{request.request_id}'") == '0'
    assert profile_pg.sql(f"SELECT requests FROM agent_quotas WHERE user_id = '{profile_store.user_id}'") == quota


def test_pg_profile_change_after_apply_keeps_idempotent_result(profile_store, profile_pg):
    action = profile_action(profile_store)
    request = confirmation()
    applied = profile_store.confirm_action(action.id, request)
    profile_pg.sql(f"UPDATE users SET custom_calories = 1900 WHERE id = '{profile_store.user_id}'")
    assert profile_store.confirm_action(action.id, request) == applied
    assert profile_store.confirm_action(action.id, confirmation()).events[0].data.saved_plan_id == applied.events[0].data.saved_plan_id


def test_pg_legacy_pending_and_missing_new_profile_version_fail_closed(profile_store, profile_pg):
    action = profile_action(profile_store)
    profile_pg.sql(f"UPDATE agent_actions SET expected_profile_version = NULL WHERE id = '{action.id}'")
    with pytest.raises(routes.StorageError, match='stale_profile'):
        profile_store.confirm_action(action.id, confirmation())
    with pytest.raises(routes.StorageError, match='stale_profile'):
        prepared_action(profile_store)
    assert profile_pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{profile_store.user_id}'") == '0'


def test_known_profile_rejection_persists_only_recoverable_error(monkeypatch):
    from dormdish_agent.models import RunState, TurnRequest
    writes = []
    class Client:
        def __init__(self, **kw): pass
        async def close(self): pass
    class Turn:
        def __init__(self, context, request, cid, history, **kw):
            self.state = RunState(id=request.request_id, conversation_id=cid, status='running')
            self.history, self.assistant_text = [{'role': 'assistant', 'content': 'Old context'}], 'Old context'
        async def stream(self):
            yield routes.append_event(self.state, 'text_delta', {'text': 'Preview'})
            yield routes.append_event(self.state, 'complete', {'citations': []})
    class Store:
        def finish_turn(self, state, history, text, action=None):
            writes.append((state.model_copy(deep=True), history, text, action))
            if len(writes) == 1: raise routes.StorageError('stale_profile')
            return state
    monkeypatch.setattr(routes, 'AsyncOpenAI', Client)
    monkeypatch.setattr(routes, 'AgentTurn', Turn)
    async def run():
        state = RunState(id=uuid4(), conversation_id=uuid4(), status='running')
        return [frame async for frame in routes.run_stream(Store(), state,
            TurnRequest(request_id=state.id, text='hi'), SimpleNamespace(proposal=None), [], None)]
    frames = asyncio.run(run())
    assert len(writes) == 2 and writes[0][0].status == 'complete'
    assert writes[1][0].status == 'error' and writes[1][1:] == ([], '', None)
    assert writes[1][0].events[-1].data.code == 'stale_profile'
    assert b'"type":"error"' in frames[-1] and not any(b'"type":"complete"' in frame for frame in frames)
