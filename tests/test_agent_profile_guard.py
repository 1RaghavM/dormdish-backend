"""Independent current-profile Apply guard checks against real local PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dormdish_agent import routes

from tests.test_agent_routes import api, route_pg, pg, stores, turn_body, events, install_model
from tests.test_agent_storage_unit import confirmation


@pytest.fixture(scope='module', autouse=True)
def profile_columns(route_pg):
    route_pg.sql('''ALTER TABLE users
        ADD COLUMN age integer, ADD COLUMN height_feet integer, ADD COLUMN height_inches integer,
        ADD COLUMN weight_lbs numeric, ADD COLUMN gender text, ADD COLUMN activity_level text,
        ADD COLUMN goal text, ADD COLUMN goal_weight_lbs numeric, ADD COLUMN weight_change_speed text,
        ADD COLUMN custom_calories integer, ADD COLUMN custom_protein_g integer,
        ADD COLUMN custom_fat_g integer, ADD COLUMN custom_carbs_g integer,
        ADD COLUMN username text, ADD COLUMN last_login timestamptz;''')


def preview(api, monkeypatch):
    install_model(monkeypatch)
    conversation = api.owner.create_conversation()
    response = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=turn_body())
    assert response.status_code == 200, response.text
    action = next(event['data']['action'] for event in events(response) if event['type'] == 'action_required')
    return action


def side_effects(api):
    owner = api.owner.user_id
    return json.loads(api.pg.sql(f"""SELECT jsonb_build_object(
        'plans', (SELECT coalesce(jsonb_agg(to_jsonb(s)), '[]') FROM saved_meal_plans s WHERE user_id = '{owner}'),
        'quota', (SELECT coalesce(jsonb_agg(to_jsonb(q)), '[]') FROM agent_quotas q WHERE user_id = '{owner}'),
        'runs', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY id), '[]') FROM agent_runs r WHERE user_id = '{owner}'),
        'actions', (SELECT coalesce(jsonb_agg(to_jsonb(a)), '[]') FROM agent_actions a WHERE user_id = '{owner}'))"""))


@pytest.mark.parametrize('change', [
    "university = 'hopkins'", "dietary_restrictions = '[\"peanut\"]'", "dietary_preference = 'vegan'",
    'age = 21', 'height_feet = 6', 'height_inches = 2', 'weight_lbs = 170', "gender = 'male'",
    "activity_level = 'moderately_active'", "goal = 'lose_weight'", 'goal_weight_lbs = 150',
    "weight_change_speed = 'slow'", 'custom_calories = 1800', 'custom_protein_g = 110',
    'custom_fat_g = 60', 'custom_carbs_g = 180',
])
def test_changed_protected_profile_refuses_apply_without_any_partial_write(api, monkeypatch, change):
    action = preview(api, monkeypatch)
    before = side_effects(api)
    api.pg.sql(f"UPDATE users SET {change} WHERE id = '{api.owner.user_id}'")
    body = confirmation().model_dump(mode='json')
    result = api.client.post(f"/agent/actions/{action['id']}/confirm", json=body)
    assert result.status_code == 409, result.text
    assert side_effects(api) == before
    assert api.client.get(f"/agent/runs/{body['request_id']}").status_code == 404


@pytest.mark.parametrize('change', ['', "username = 'new name'", 'last_login = now()'])
def test_unchanged_planning_profile_and_unrelated_account_updates_allow_apply(api, monkeypatch, change):
    action = preview(api, monkeypatch)
    expected = api.owner.get_profile()
    if change:
        api.pg.sql(f"UPDATE users SET {change} WHERE id = '{api.owner.user_id}'")
    assert api.owner.get_profile() == expected
    result = api.client.post(f"/agent/actions/{action['id']}/confirm", json=confirmation().model_dump(mode='json'))
    assert result.status_code == 200, result.text
    saved = result.json()['events'][-1]['data']['saved_plan_id']
    assert api.owner.get_saved_plan(saved)['plan']['user_id'] == api.owner.user_id


def test_applied_action_replays_original_after_profile_changes(api, monkeypatch):
    action = preview(api, monkeypatch)
    body = confirmation().model_dump(mode='json')
    original = api.client.post(f"/agent/actions/{action['id']}/confirm", json=body)
    assert original.status_code == 200
    api.pg.sql(f"UPDATE users SET university = 'hopkins', dietary_restrictions = '[\"peanut\"]' WHERE id = '{api.owner.user_id}'")
    before = side_effects(api)
    replay = api.client.post(f"/agent/actions/{action['id']}/confirm", json=body)
    assert replay.json() == original.json()
    assert side_effects(api) == before
    another = api.client.post(f"/agent/actions/{action['id']}/confirm", json=confirmation().model_dump(mode='json'))
    assert another.status_code == 200
    assert another.json()['events'][-1]['data']['saved_plan_id'] == original.json()['events'][-1]['data']['saved_plan_id']
    assert side_effects(api)['plans'] == before['plans']


@pytest.mark.parametrize('endpoint', ['messages', 'confirm'])
def test_client_cannot_supply_expected_profile_guard(api, monkeypatch, endpoint):
    if endpoint == 'messages':
        conversation = api.owner.create_conversation()
        path, body = f'/agent/conversations/{conversation.id}/messages', turn_body()
    else:
        action = preview(api, monkeypatch)
        path, body = f"/agent/actions/{action['id']}/confirm", confirmation().model_dump(mode='json')
    body['expected_profile_version'] = '0' * 64
    before = side_effects(api)
    result = api.client.post(path, json=body)
    assert result.status_code == 422
    assert side_effects(api) == before


def test_legacy_action_without_profile_guard_fails_closed(api, monkeypatch):
    action = preview(api, monkeypatch)
    api.pg.sql(f"UPDATE agent_actions SET expected_profile_version = NULL WHERE id = '{action['id']}'")
    before = side_effects(api)
    result = api.client.post(f"/agent/actions/{action['id']}/confirm", json=confirmation().model_dump(mode='json'))
    assert result.status_code == 409
    assert side_effects(api) == before


def test_profile_change_before_finish_cannot_persist_an_action(api, monkeypatch):
    install_model(monkeypatch)
    original_rpc = api.database.rpc
    finish_calls = []

    def rpc(name, params):
        if name == 'agent_finish_turn' and params.get('p_action'):
            finish_calls.append(params)
            api.pg.sql(f"UPDATE users SET custom_calories = 1200 WHERE id = '{api.owner.user_id}'")
        return original_rpc(name, params)

    monkeypatch.setattr(api.database, 'rpc', rpc)
    conversation = api.owner.create_conversation()
    body = turn_body()
    response = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=body)
    assert len(finish_calls) == 1
    assert not any(event['type'] in ('action_required', 'complete') for event in events(response))
    state = side_effects(api)
    assert state['plans'] == state['actions'] == []
    assert state['quota'][0]['requests'] == 1  # Only the already-admitted model turn.
    recovered = api.client.get(f"/agent/runs/{body['request_id']}")
    assert recovered.status_code == 200 and recovered.json()['status'] == 'error'
    assert recovered.json()['events'][-1]['type'] == 'error'
    assert '1200' not in recovered.text and 'custom_calories' not in recovered.text
    assert api.owner.get_conversation(conversation.id).pending_actions == []


def test_confirm_waits_for_concurrent_profile_update_then_refuses_stale_action(api, monkeypatch):
    action = preview(api, monkeypatch)
    before = side_effects(api)
    marker = 'profile_update_' + uuid4().hex
    with ThreadPoolExecutor(2) as pool:
        update = pool.submit(api.pg.sql, f"""SET application_name = '{marker}'; BEGIN;
            UPDATE users SET dietary_restrictions = '["peanut"]' WHERE id = '{api.owner.user_id}';
            SELECT pg_sleep(2); COMMIT;""")
        deadline = time.monotonic() + 5
        while api.pg.sql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{marker}' AND wait_event = 'PgSleep'") != '1':
            assert time.monotonic() < deadline, 'Profile transaction did not acquire its row lock'
            time.sleep(.02)
        apply = pool.submit(api.client.post, f"/agent/actions/{action['id']}/confirm",
                            json=confirmation().model_dump(mode='json'))
        time.sleep(.15)
        assert not apply.done(), 'Apply bypassed the locked current user row'
        update.result(timeout=5)
        result = apply.result(timeout=5)
    assert result.status_code == 409, result.text
    assert side_effects(api) == before


def test_database_profile_is_owner_scoped_private_and_not_auth_cache(api, monkeypatch):
    api.profiles['owner']['university'] = 'hopkins'
    api.profiles['owner']['dietary_restrictions'] = ['not a supported restriction']
    api.pg.sql(f"""UPDATE users SET username = 'private login', age = 21, height_feet = 5,
        height_inches = 10, weight_lbs = 170, gender = 'male', activity_level = 'moderately_active',
        goal = 'maintain', custom_calories = 1800 WHERE id = '{api.owner.user_id}'""")
    snapshot = api.owner.get_profile()
    assert snapshot['profile']['university'] == 'rutgers'
    assert snapshot['profile']['custom_calories'] == 1800
    assert 'username' not in snapshot['profile'] and 'last_login' not in snapshot['profile']
    assert len(snapshot['version']) == 64 and all(c in '0123456789abcdef' for c in snapshot['version'])
    assert api.other.get_profile()['profile']['custom_calories'] is None
    import app as application
    app = FastAPI()
    app.include_router(routes.create_router(application.get_current_user, application.require_premium,
        lambda: api.database, application.agent_daily_targets))
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer owner'
        client.headers['X-App-Version'] = '2.2.4'
        action = preview(SimpleNamespace(**(vars(api) | {'client': client})), monkeypatch)
    assert 'expected_profile_version' not in action
    stored = api.pg.sql(f"SELECT expected_profile_version FROM agent_actions WHERE id = '{action['id']}'")
    assert stored == snapshot['version']
    persisted = json.loads(api.pg.sql(f"SELECT to_jsonb(a) FROM agent_actions a WHERE id = '{action['id']}'"))
    assert persisted['save_payload']['nutrition_targets']['calories'] == 1800
    result = api.client.get(f"/agent/conversations/{persisted['conversation_id']}")
    assert stored not in result.text
    for role in ['anon', 'authenticated']:
        with pytest.raises(RuntimeError, match='permission denied'):
            api.pg.sql(f"SET ROLE {role}; SELECT agent_get_profile('{api.owner.user_id}')")


@pytest.fixture
def dietary_menu(api):
    original = api.pg.sql('SELECT menu_data FROM menus LIMIT 1')
    yield
    api.pg.sql("UPDATE menus SET menu_data = '" + original.replace("'", "''") + "'")


def set_stored_diet(api, diet, tags):
    api.profiles['owner']['dietary_preference'] = 'omnivore'  # Deliberately stale auth cache.
    api.pg.sql(f"UPDATE users SET dietary_preference = '{diet}' WHERE id = '{api.owner.user_id}'")
    if tags == 'absent':
        api.pg.sql("UPDATE menus SET menu_data = menu_data #- '{items,0,dietary_tags}'")
    else:
        encoded = json.dumps(tags)
        api.pg.sql(f"UPDATE menus SET menu_data = jsonb_set(menu_data, '{{items,0,dietary_tags}}', '{encoded}')")


@pytest.mark.parametrize('diet,tag', [('vegan', 'vegan'), ('vegetarian', 'vegetarian'),
    ('pescetarian', 'pescatarian'), ('keto', 'keto'), ('gluten_free', None), ('dairy_free', None)])
def test_current_profile_diet_enum_can_preview_and_apply(api, monkeypatch, dietary_menu, diet, tag):
    set_stored_diet(api, diet, [tag] if tag else [])
    action = preview(api, monkeypatch)
    assert action['proposal']['totals']['carbs_g'] == 66  # Exact menu math, including for Keto.
    assert side_effects(api)['plans'] == []
    applied = api.client.post(f"/agent/actions/{action['id']}/confirm", json=confirmation().model_dump(mode='json'))
    assert applied.status_code == 200, applied.text
    saved = applied.json()['events'][-1]['data']['saved_plan_id']
    payload = api.owner.get_saved_plan(saved)['plan']['meal_plan_data']
    assert payload['nutrition_targets']['carbs_g'] == 200  # Keep current target; no guessed diet target.
    assert payload['meal_plan']['totals']['carbs_g'] == 66
    assert api.owner.get_profile()['profile']['dietary_preference'] == diet


@pytest.mark.parametrize('diet', ['pescetarian', 'keto'])
@pytest.mark.parametrize('tags', ['absent', None, [], ['unverified-diet-label']])
def test_profile_diet_unknown_or_missing_menu_evidence_never_creates_action(
    api, monkeypatch, dietary_menu, diet, tags
):
    set_stored_diet(api, diet, tags)
    requests = install_model(monkeypatch)
    conversation = api.owner.create_conversation()
    response = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=turn_body())
    assert response.status_code == 200, response.text  # Accepted profile, rejected food evidence.
    assert len(requests) == 3
    result = next(item['output'] for item in requests[-1]['input']
                  if item.get('type') == 'function_call_output' and item['call_id'] == 'call_2')
    assert result == 'This tool could not verify the requested data. Do not invent an answer; explain the limitation.'
    assert not any(event['type'] in ('meal_preview', 'action_required') for event in events(response))
    assert api.owner.get_conversation(conversation.id).pending_actions == []
    state = side_effects(api)
    assert state['plans'] == state['actions'] == []
