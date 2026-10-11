"""Independent preference regressions using actual RPCs in disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memory.preference_manager import PreferenceManager
from dormdish_agent.models import TurnRequest
from tests.test_agent_storage_unit import pg, stores, terminal

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module', autouse=True)
def preference_schema(pg):
    # Deliberately support legacy contradictory rows and a deployed category-scoped unique index.
    pg.sql('''CREATE TABLE user_preferences (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        category text NOT NULL CHECK (category IN ('favorite', 'dislike')), item_name text NOT NULL,
        item_type text NOT NULL CHECK (item_type IN ('meal_plan', 'food_item')),
        meal_type text NOT NULL CHECK (meal_type IN ('breakfast', 'lunch', 'dinner')),
        meal_plan_id text, reason text, created_at timestamptz NOT NULL DEFAULT now());
        CREATE UNIQUE INDEX ON user_preferences(user_id, category, lower(item_name), meal_type);
        GRANT SELECT, INSERT, UPDATE, DELETE ON user_preferences TO service_role;''')
    legacy = str(uuid4())
    pg.sql(f"""INSERT INTO users VALUES ('{legacy}');
        INSERT INTO user_preferences(user_id,category,item_name,item_type,meal_type)
        VALUES ('{legacy}','favorite','Rice','food_item','dinner'),
               ('{legacy}','dislike','Rice','food_item','dinner');""")
    before = rows(pg, legacy)
    pg.sql((ROOT / 'migrations/atomic_preferences.sql').read_text())
    assert rows(pg, legacy) == before  # Additive migration must not silently rewrite legacy choices.


def rows(pg, owner):
    return json.loads(pg.sql(f"SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY id),'[]') FROM user_preferences p WHERE user_id='{owner}'"))


def history(store, text='Synthetic remembered taste'):
    conversation = store.create_conversation()
    _, run = store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text=text))
    replay = [{'role': 'user', 'content': text}]
    store.finish_turn(terminal(run), replay, 'Acknowledged')
    return conversation, replay


def test_legacy_contradictions_collapse_only_on_explicit_item_change(stores, pg):
    owner, _ = stores
    old, latest, untouched = [str(uuid4()) for _ in range(3)]
    pg.sql(f"""INSERT INTO user_preferences(id,user_id,category,item_name,item_type,meal_type,created_at) VALUES
        ('{old}','{owner.user_id}','favorite','RICE','food_item','dinner','2026-01-01'),
        ('{latest}','{owner.user_id}','dislike',' Rice ','food_item','dinner','2026-02-01'),
        ('{untouched}','{owner.user_id}','favorite','Soup','food_item','lunch','2026-01-01');""")
    manager = PreferenceManager(pg)
    result = manager.add_favorite_meal_plan(owner.user_id, 'legacy-plan-id', 'rice', 'dinner')['data'][0]
    assert result['id'] == latest
    assert result['item_type'] == 'meal_plan' and result['meal_plan_id'] == 'legacy-plan-id'
    assert result['category'] == 'favorite' and result['item_name'] == 'rice'
    assert {row['id'] for row in rows(pg, owner.user_id)} == {latest, untouched}
    repeat = manager.add_favorite_meal_plan(owner.user_id, 'legacy-plan-id', 'rice', 'dinner')['data'][0]
    assert repeat == result
    changed = manager.add_dislike(owner.user_id, 'Rice', 'food_item', 'dinner', 'Texture')['data'][0]
    assert changed['id'] == latest and changed['meal_plan_id'] is None and changed['reason'] == 'Texture'


def test_simultaneous_normalized_choices_share_one_id_and_preserve_other_scope(stores, pg):
    owner, other = stores
    manager = PreferenceManager(pg)
    first = manager.add_favorite_food_item(owner.user_id, 'Rice', 'dinner')['data'][0]
    manager.add_dislike(owner.user_id, 'Rice', 'food_item', 'lunch')
    manager.add_dislike(other.user_id, 'Rice', 'food_item', 'dinner')
    preserved = rows(pg, other.user_id)
    barrier = Barrier(6)

    def choose(index):
        barrier.wait(timeout=10)
        if index % 2:
            return manager.add_dislike(owner.user_id, ' rice ', 'food_item', 'dinner')['data'][0]
        return manager.add_favorite_food_item(owner.user_id, 'RICE', 'dinner')['data'][0]

    with ThreadPoolExecutor(6) as pool:
        outcomes = list(pool.map(choose, range(6)))
    assert {outcome['id'] for outcome in outcomes} == {first['id']}
    remaining = rows(pg, owner.user_id)
    assert len(remaining) == 2
    assert sum(row['meal_type'] == 'dinner' for row in remaining) == 1
    assert next(row for row in remaining if row['meal_type'] == 'lunch')['category'] == 'dislike'
    assert rows(pg, other.user_id) == preserved


@pytest.mark.parametrize('category', ['favorite', 'dislike'])
def test_forget_category_across_meals_preserves_opposite_and_other_owner(stores, pg, category):
    owner, other = stores
    manager = PreferenceManager(pg)
    opposite = 'dislike' if category == 'favorite' else 'favorite'
    pg.sql(f"""INSERT INTO user_preferences(user_id,category,item_name,item_type,meal_type) VALUES
        ('{owner.user_id}','{category}','Rice','food_item','lunch'),
        ('{owner.user_id}','{category}',' RICE ','food_item','dinner'),
        ('{owner.user_id}','{opposite}','rice','food_item','breakfast'),
        ('{other.user_id}','{category}','Rice','food_item','dinner');""")
    owned, _ = history(owner)
    foreign, replay = history(other)
    forget = getattr(manager, 'remove_' + category)
    assert forget(owner.user_id, ' rice ') == {'status': 'deleted'}
    assert [(r['category'], r['meal_type']) for r in rows(pg, owner.user_id)] == [(opposite, 'breakfast')]
    assert len(rows(pg, other.user_id)) == 1
    assert owner.load_history(owned.id) == []
    assert other.load_history(foreign.id) == replay
    # Even an absent delete invalidates a newly loaded stale replay, but is otherwise idempotent.
    _, run = owner.claim_turn(owned.id, TurnRequest(request_id=uuid4(), text='Earlier favorite'))
    assert forget(owner.user_id, ' rice ') == {'status': 'not_found'}
    owner.finish_turn(terminal(run), [{'role': 'user', 'content': 'Restore forgotten favorite'}])
    assert owner.load_history(owned.id) == []
    assert len(rows(pg, owner.user_id)) == 1


@pytest.mark.parametrize('operation', ['replace', 'forget'])
def test_history_failure_restores_exact_rows_and_replay(stores, pg, operation):
    owner, _ = stores
    manager = PreferenceManager(pg)
    manager.add_favorite_food_item(owner.user_id, 'Rice', 'dinner')
    conversation, replay = history(owner)
    before = rows(pg, owner.user_id)
    pg.sql('''CREATE FUNCTION deny_epoch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic private SQL detail'; END $$;
        CREATE TRIGGER deny_epoch BEFORE UPDATE ON agent_conversations FOR EACH ROW EXECUTE FUNCTION deny_epoch();''')
    try:
        with pytest.raises(RuntimeError, match='^Preference update unavailable; refresh preferences before retrying$'):
            if operation == 'replace':
                manager.add_dislike(owner.user_id, 'Rice', 'food_item', 'dinner')
            else:
                manager.remove_favorite(owner.user_id, 'Rice')
        assert rows(pg, owner.user_id) == before
        assert owner.load_history(conversation.id) == replay
    finally:
        pg.sql('DROP TRIGGER deny_epoch ON agent_conversations; DROP FUNCTION deny_epoch()')


@pytest.mark.parametrize('operation', ['set', 'forget'])
def test_change_invalidates_all_owned_replays_and_blocks_inflight_resurrection(stores, pg, operation):
    owner, other = stores
    manager = PreferenceManager(pg)
    conversation, _ = history(owner)
    another, _ = history(owner)
    foreign, foreign_replay = history(other)
    _, run = owner.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Stale prior context'))
    if operation == 'set':
        manager.add_favorite_food_item(owner.user_id, 'Rice', 'dinner')
    else:
        manager.remove_dislike(owner.user_id, 'Rice')
    owner.finish_turn(terminal(run), [{'role': 'user', 'content': 'Stale prior preference'}], 'Completed')
    assert owner.get_run(run.id).status == 'complete'
    assert owner.load_history(conversation.id) == owner.load_history(another.id) == []
    assert other.load_history(foreign.id) == foreign_replay
    # New instructions after invalidation are replayable; forgetting must not disable memory forever.
    _, fresh = owner.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='New explicit instruction'))
    new_history = [{'role': 'user', 'content': 'New explicit instruction'}]
    owner.finish_turn(terminal(fresh), new_history)
    assert owner.load_history(conversation.id) == new_history


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
@pytest.mark.parametrize('function,args', [('preference_set', "'favorite','Rice','food_item','dinner'"),
                                           ('preference_forget', "'favorite','Rice'")])
def test_both_rpc_entrypoints_deny_untrusted_roles(stores, pg, role, function, args):
    owner, _ = stores
    with pytest.raises(RuntimeError, match='permission denied for function'):
        pg.sql(f"SET ROLE {role}; SELECT {function}('{owner.user_id}',{args})")
    assert rows(pg, owner.user_id) == []


@pytest.fixture
def api(stores, pg):
    spec = importlib.util.spec_from_file_location('preference_http_test_app', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {}, clear=True), patch('dotenv.load_dotenv'):
        spec.loader.exec_module(module)
    module.supabase = pg
    owner, _ = stores
    module.app.dependency_overrides[module.require_premium] = lambda: {'id': owner.user_id}
    with TestClient(module.app) as client:
        yield client, module, owner


def test_existing_http_contract_uses_authenticated_owner_and_preserves_delete_result(api, stores, pg):
    client, _, owner = api
    _, other = stores
    payload = dict(item_name="Chef's Rice 🌶️", item_type='meal_plan', meal_type='dinner', meal_plan_id='legacy-id', user_id=other.user_id)
    response = client.post('/preferences/favorite', json=payload)
    assert response.status_code == 200
    assert response.json() == dict(status='success', message="Added Chef's Rice 🌶️ to favorites",
                                  item_name=payload['item_name'], item_type='meal_plan', meal_type='dinner')
    assert rows(pg, owner.user_id)[0]['meal_plan_id'] == 'legacy-id'
    assert rows(pg, other.user_id) == []
    response = client.post('/preferences/dislike', json={**payload, 'reason': 'Texture'})
    assert response.status_code == 200 and rows(pg, owner.user_id)[0]['category'] == 'dislike'
    for expected in [True, False]:
        removed = client.delete('/preferences/dislike/' + payload['item_name'])
        assert removed.status_code == 200
        assert removed.json()['status'] == 'success' and removed.json()['was_found'] is expected


@pytest.mark.parametrize('route,field,value', [
    ('favorite', 'item_name', ' \t'), ('favorite', 'meal_type', 'snack'),
    ('favorite', 'item_type', 'profile'), ('favorite', 'meal_plan_id', 'x' * 201),
    ('dislike', 'item_name', 'x' * 201), ('dislike', 'reason', 'x' * 1001),
    ('dislike', 'meal_type', None), ('dislike', 'item_name', 42),
])
def test_http_rejects_invalid_body_before_storage(api, pg, route, field, value):
    client, module, owner = api
    class NoStorage:
        def rpc(self, *args):
            pytest.fail('Invalid HTTP input reached storage')
    module.supabase = NoStorage()
    payload = dict(item_name='Rice', item_type='food_item', meal_type='dinner')
    response = client.post('/preferences/' + route, json={**payload, field: value})
    assert response.status_code == 422
    assert rows(pg, owner.user_id) == []


@pytest.mark.parametrize('method,path', [('post', '/preferences/favorite'), ('post', '/preferences/dislike'),
                                        ('delete', '/preferences/favorite/Rice'), ('delete', '/preferences/dislike/Rice')])
def test_http_storage_errors_are_private_and_never_retry(api, caplog, method, path):
    client, module, _ = api
    calls = []
    class FailedStorage:
        def rpc(self, name, params):
            calls.append(name)
            return self
        def execute(self):
            raise RuntimeError('PRIVATE_PROFILE_AND_SQL_CREDENTIAL')
    module.supabase = FailedStorage()
    kwargs = {'json': dict(item_name='Rice', item_type='food_item', meal_type='dinner')} if method == 'post' else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 500 and len(calls) == 1
    assert 'PRIVATE_PROFILE_AND_SQL_CREDENTIAL' not in response.text + caplog.text
    assert response.json() == {'error': 'HTTP error', 'message': {
        '/preferences/favorite': 'Failed to add favorite',
        '/preferences/dislike': 'Failed to record dislike',
        '/preferences/favorite/Rice': 'Failed to remove favorite',
        '/preferences/dislike/Rice': 'Failed to remove dislike',
    }[path], 'status_code': 500}


def test_http_preferences_require_session_before_storage(api):
    client, module, _ = api
    module.app.dependency_overrides.clear()
    class NoStorage:
        def rpc(self, *args):
            pytest.fail('Unauthenticated request reached storage')
    module.supabase = NoStorage()
    for method, path in [('post', '/preferences/favorite'), ('delete', '/preferences/dislike/Rice')]:
        kwargs = {'json': dict(item_name='Rice', item_type='food_item', meal_type='dinner')} if method == 'post' else {}
        assert getattr(client, method)(path, **kwargs).status_code == 401


@pytest.mark.parametrize('field,value', [
    ('category', 'profile'), ('item_type', None), ('meal_type', 'snack'),
    ('item_name', ''), ('item_name', 'x' * 201), ('reason', 'x' * 1001),
    ('meal_plan_id', 'x' * 201),
])
def test_rpc_rejects_invalid_values_without_mutation_or_history_loss(stores, pg, field, value):
    owner, _ = stores
    conversation, replay = history(owner)
    values = dict(p_user_id=owner.user_id, p_category='favorite', p_item_name='Rice',
                  p_item_type='food_item', p_meal_type='dinner')
    with pytest.raises(RuntimeError, match='preference_invalid'):
        pg.rpc('preference_set', {**values, 'p_' + field: value}).execute()
    assert rows(pg, owner.user_id) == []
    assert owner.load_history(conversation.id) == replay
