"""Independent completion API/SQL tests: synthetic rows, real transactions, no models."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agents import RunContextWrapper
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dormdish_agent.models import Nutrition, PlanningContext
from dormdish_agent.tools import FoodContext, read_intake
from tests.test_agent_storage_unit import LocalPG, pg
from tests.test_agent_routes import RouteDatabase

ROOT = Path(__file__).resolve().parents[1]
DAY = '2026-10-10'
ZERO = dict(calories=0, protein_g=0, fat_g=0, carbs_g=0)
RICE = dict(calories=300, protein_g=6, fat_g=1.5, carbs_g=66)
BEANS = dict(calories=120, protein_g=8, fat_g=1, carbs_g=20)


def literal(value):
    return "'" + json.dumps(value).replace("'", "''") + "'"


class CompletionPG(LocalPG):
    def sql(self, sql):
        try:
            return super().sql(sql)
        except RuntimeError as error:
            match = re.search(r'ERROR:\s+(completion_\w+)', str(error))
            if match:
                error.message = match[1]
            raise


@pytest.fixture(scope='module')
def completion_pg(pg):
    database = CompletionPG(pg.database)
    database.sql('''CREATE TABLE food_log(id text PRIMARY KEY, user_id uuid REFERENCES users(id) ON DELETE CASCADE,
        date date, calories numeric, protein_g numeric, fat_g numeric, carbs_g numeric);
        GRANT SELECT ON food_log TO service_role;''')
    database.sql((ROOT / 'migrations/add_meal_completion.sql').read_text())
    return database


@pytest.fixture
def api(completion_pg, monkeypatch):
    import app as application
    from services.meal_completion import create_router
    owners = [str(uuid4()), str(uuid4())]
    for owner in owners:
        completion_pg.sql(f"INSERT INTO users(id) VALUES ('{owner}')")
    profiles = {token: {'id': owner, 'university': 'rutgers'} for token, owner in zip(['owner', 'other'], owners)}
    monkeypatch.setattr(application, 'verify_session_token', profiles.get)
    monkeypatch.setenv('DORMDISH_COMPLETION_ENABLED', 'true')
    database = RouteDatabase(completion_pg)
    app = FastAPI()
    app.include_router(create_router(application.get_current_user, lambda: database))
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer owner'
        yield SimpleNamespace(pg=completion_pg, client=client, owner=owners[0], other=owners[1], database=database)


def seed(api, *, owner=None, day=DAY, payload=None):
    plan_id = str(uuid4())
    payload = payload or {'meal_plan': {'meals': {'dinner': {'items': [
        {'name': 'Rice 🌶', 'servings': 1.5, 'serving_size': 'cup', **RICE},
        {'name': 'Beans', 'servings': 1, 'serving_size': 'bowl', **BEANS}], 'totals': {'calories': 9999}}},
        'totals': {'calories': 8888}}}
    api.pg.sql(f"INSERT INTO saved_meal_plans(id,user_id,date,dining_hall,meal_plan_data) VALUES "
               f"('{plan_id}','{owner or api.owner}','{day}','Busch',{literal(payload)})")
    return plan_id


def current(api, plan_id):
    response = api.client.get(f'/meal-plan/{plan_id}/completion')
    assert response.status_code == 200, response.text
    return response.json()


def body(api, plan_id, *, selected=(0,), **changes):
    return {'request_id': str(uuid4()), 'date': DAY,
            'plan_version': current(api, plan_id)['plan']['completion_version'],
            'expected_revision': 0, 'mode': 'current',
            'selected_items': [{'meal_type': 'dinner', 'index': i} for i in selected], **changes}


def put(api, plan_id, request, status=200):
    response = api.client.put(f'/meal-plan/{plan_id}/completion', json=request)
    assert response.status_code == status, response.text
    return response.json()


def recorded(api, day=DAY):
    response = api.client.get('/meal-completions', params={'date': day})
    assert response.status_code == 200, response.text
    return response.json()


def test_partial_selection_uses_saved_item_amounts_not_servings_or_claimed_totals(api):
    plan = seed(api)
    snapshot = current(api, plan)
    assert len(snapshot['available_items']) == 2
    request = body(api, plan)
    state = put(api, plan, request)
    assert state['recorded_items'][0]['totals'] == RICE
    assert state['totals'] == RICE
    assert state['items'] == state['recorded_items']
    assert state['selected_items'] == request['selected_items']
    assert state['revision'] == 1
    assert (state['plan_id'], state['plan_version'], state['date']) == (plan, request['plan_version'], DAY)
    assert state['recorded_items'][0]['name'] == 'Rice 🌶'
    assert state['recorded_items'][0]['totals'] == RICE
    assert state['recorded_items'][0]['source_id']
    assert api.client.get(f"/meal-completion-requests/{request['request_id']}").json() == state


def test_whole_meal_then_uncheck_then_empty_keeps_exact_math_and_receipts(api):
    plan = seed(api)
    request = body(api, plan, selected=(0, 1))
    first = put(api, plan, request)
    assert [item['totals'] for item in first['recorded_items']] == [RICE, BEANS]
    assert first['totals'] == {key: RICE[key] + BEANS[key] for key in ZERO}
    assert intake(api)['remaining_targets']['calories'] == 1580
    second_request = {**request, 'request_id': str(uuid4()), 'expected_revision': 1,
                      'selected_items': [{'meal_type': 'dinner', 'index': 1}]}
    second = put(api, plan, second_request)
    assert second['selected_items'] == second_request['selected_items'] and second['revision'] == 2
    assert second['totals'] == BEANS and len(second['items']) == 1
    assert intake(api)['remaining_targets']['calories'] == 1880
    third = put(api, plan, {**second_request, 'request_id': str(uuid4()), 'expected_revision': 2, 'selected_items': []})
    assert third['selected_items'] == [] and third['revision'] == 3
    assert third['recorded_items'] == first['recorded_items']
    assert third['totals'] == ZERO and third['items'] == []
    assert intake(api)['remaining_targets']['calories'] == 2000
    assert put(api, plan, request) == first
    assert put(api, plan, second_request) == second
    assert recorded(api)[0]['selected_items'] == []


def test_duplicate_concurrent_request_returns_exactly_one_revision(api):
    plan = seed(api)
    request = body(api, plan)
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: api.client.put(f'/meal-plan/{plan}/completion', json=request), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()['revision'] == 1
    assert len(recorded(api)) == 1


def test_concurrent_distinct_toggles_and_delayed_old_request_cannot_overwrite(api):
    plan = seed(api)
    first = body(api, plan)
    second = {**first, 'request_id': str(uuid4()), 'selected_items': [{'meal_type': 'dinner', 'index': 1}]}
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda req: api.client.put(f'/meal-plan/{plan}/completion', json=req), [first, second]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    successful = next(r.json() for r in responses if r.status_code == 200)
    assert recorded(api)[0] == successful
    delayed = {**first, 'request_id': str(uuid4())}
    put(api, plan, delayed, 409)
    assert recorded(api)[0] == successful


@pytest.mark.parametrize('change', [
    {'selected_items': []}, {'expected_revision': 1}, {'date': '2026-10-11'}, {'mode': 'frozen'},
])
def test_request_id_cannot_be_reused_for_different_payload(api, change):
    plan = seed(api)
    request = body(api, plan)
    original = put(api, plan, request)
    put(api, plan, {**request, **change}, 409)
    assert api.client.get(f"/meal-completion-requests/{request['request_id']}").json() == original
    assert recorded(api) == [original]


@pytest.mark.parametrize('field,value', [
    ('user_id', 'forged'), ('totals', RICE), ('items', []), ('calories', 1),
    ('expected_revision', True), ('expected_revision', -1), ('expected_revision', 2147483647),
    ('plan_version', 'not-a-uuid'), ('date', '2026-02-30'),
    ('selected_items', [{'meal_type': 'dinner', 'index': -1}]),
    ('selected_items', [{'meal_type': 'dinner', 'index': True}]),
    ('selected_items', [{'meal_type': 'dinner', 'index': 30}]),
    ('selected_items', [{'meal_type': 'snack', 'index': 0}]),
    ('selected_items', [{'meal_type': 'dinner', 'index': 0, 'calories': 1}]),
    ('selected_items', [{'meal_type': 'dinner', 'index': 0}] * 2),
])
def test_invalid_client_input_never_writes_or_creates_receipt(api, field, value):
    plan = seed(api)
    request = {**body(api, plan), field: value}
    put(api, plan, request, 422)
    assert recorded(api) == []
    assert api.client.get(f"/meal-completion-requests/{request['request_id']}").status_code == 404


@pytest.mark.parametrize('selection', [[{'meal_type': 'dinner', 'index': 2}], [{'meal_type': 'lunch', 'index': 0}]])
def test_well_formed_but_absent_food_is_not_invented(api, selection):
    plan = seed(api)
    put(api, plan, body(api, plan, selected_items=selection), 422)
    assert recorded(api) == []


def test_cross_owner_reads_writes_receipts_and_lists_do_not_leak(api):
    plan = seed(api)
    request = body(api, plan)
    original = put(api, plan, request)
    api.client.headers['Authorization'] = 'Bearer other'
    assert api.client.get(f'/meal-plan/{plan}/completion').status_code == 404
    assert api.client.put(f'/meal-plan/{plan}/completion', json={**request, 'request_id': str(uuid4())}).status_code == 404
    assert api.client.get(f"/meal-completion-requests/{request['request_id']}").status_code == 404
    assert recorded(api) == []
    api.client.headers['Authorization'] = 'Bearer owner'
    assert recorded(api) == [original]


@pytest.mark.parametrize('token', [None, 'expired'])
def test_authentication_precedes_database_access(api, token):
    api.client.headers.pop('Authorization')
    if token:
        api.client.headers['Authorization'] = 'Bearer ' + token
    assert api.client.get('/meal-completions', params={'date': DAY}).status_code == 401
    assert api.database.calls == []


def test_mismatched_date_and_missing_version_cannot_apply_stale_ui_selection(api):
    plan = seed(api)
    request = body(api, plan, date='2026-10-11')
    put(api, plan, request, 409)
    del request['plan_version']
    put(api, plan, request, 422)
    assert recorded(api) == []


@pytest.mark.parametrize('mutation', ['edit', 'delete'])
def test_plan_changes_preserve_actual_intake_and_only_explicit_frozen_undo(api, mutation):
    plan = seed(api)
    request = body(api, plan)
    original = put(api, plan, request)
    if mutation == 'edit':
        api.pg.sql(f"UPDATE saved_meal_plans SET meal_plan_data = jsonb_set(meal_plan_data, '{{meal_plan,meals,dinner,items,0,calories}}', '999') WHERE id = '{plan}'")
        assert current(api, plan)['plan']['completion_version'] != request['plan_version']
    else:
        api.pg.sql(f"DELETE FROM saved_meal_plans WHERE id = '{plan}'")
    assert recorded(api) == [original]
    assert intake(api)['remaining_targets']['calories'] == 1700
    assert put(api, plan, request) == original
    stale = {**request, 'request_id': str(uuid4()), 'expected_revision': 1}
    put(api, plan, stale, 409 if mutation == 'edit' else 404)
    undone = put(api, plan, {**stale, 'request_id': str(uuid4()), 'mode': 'frozen', 'selected_items': []})
    assert undone['selected_items'] == [] and undone['revision'] == 2
    assert undone['recorded_items'] == original['recorded_items']
    assert undone['totals'] == ZERO and undone['items'] == []
    assert intake(api)['remaining_targets']['calories'] == 2000
    assert api.client.get(f"/meal-completion-requests/{request['request_id']}").json() == original


def test_frozen_mode_cannot_add_food_never_recorded_as_eaten(api):
    plan = seed(api)
    request = body(api, plan)
    put(api, plan, request)
    attempted = {**request, 'request_id': str(uuid4()), 'expected_revision': 1, 'mode': 'frozen',
                 'selected_items': [{'meal_type': 'dinner', 'index': 1}]}
    put(api, plan, attempted, 422)
    assert recorded(api)[0]['recorded_items'][0]['totals'] == RICE


def test_rollout_disable_stops_new_writes_but_allows_recovery_and_read(api, monkeypatch):
    plan = seed(api)
    request = body(api, plan)
    state = put(api, plan, request)
    monkeypatch.setenv('DORMDISH_COMPLETION_ENABLED', 'false')
    put(api, plan, {**request, 'request_id': str(uuid4())}, 503)
    assert current(api, plan)['plan']['id'] == plan
    assert api.client.get(f"/meal-completion-requests/{request['request_id']}").json() == state
    assert recorded(api) == [state]


def intake(api):
    context = FoodContext(api.database, api.owner, 'rutgers',
        PlanningContext(dining_hall='busch', date=date.fromisoformat(DAY), meal_type='dinner'),
        Nutrition(calories=2000, protein_g=100, fat_g=70, carbs_g=200))
    result = asyncio.run(read_intake.on_invoke_tool(RunContextWrapper(context), '{}'))
    assert isinstance(result, dict), result
    return result


def add_log(api, *, owner=None, day=DAY, totals=None, source=None):
    values = totals or RICE
    source_sql = 'NULL' if source is None else "'" + source.replace("'", "''") + "'"
    api.pg.sql(f"INSERT INTO food_log(id,user_id,date,source_id,calories,protein_g,fat_g,carbs_g) VALUES "
        f"('{uuid4()}','{owner or api.owner}','{day}',{source_sql}," + ','.join(str(values[key]) for key in ZERO) + ')')


def test_actual_intake_keeps_manual_logs_and_deduplicates_only_trusted_link(api):
    plan = seed(api)
    state = put(api, plan, body(api, plan))
    source = state['recorded_items'][0]['source_id']
    add_log(api)  # Same nutrition is still a separate manually logged meal.
    add_log(api, source=source)  # Trusted server link refers to the very same eaten food.
    add_log(api, owner=api.other, totals={key: 999 for key in ZERO})
    add_log(api, day='2026-10-11', totals={key: 999 for key in ZERO})
    result = intake(api)
    assert result['completion_records_available'] is True
    assert result['remaining_targets'] == {'calories': 1400, 'protein_g': 88, 'fat_g': 67, 'carbs_g': 68}
    assert all(params['p_user_id'] == api.owner for _, params in api.database.calls)


def test_conflicting_linked_intake_fails_closed_in_actual_tool(api):
    plan = seed(api)
    state = put(api, plan, body(api, plan))
    add_log(api, totals=BEANS, source=state['recorded_items'][0]['source_id'])
    context = FoodContext(api.database, api.owner, 'rutgers',
        PlanningContext(dining_hall='busch', date=date.fromisoformat(DAY), meal_type='dinner'),
        Nutrition(calories=2000, protein_g=100, fat_g=70, carbs_g=200))
    result = asyncio.run(read_intake.on_invoke_tool(RunContextWrapper(context), '{}'))
    assert isinstance(result, str) and 'could not verify' in result
    assert api.owner not in result and state['recorded_items'][0]['source_id'] not in result


@pytest.mark.parametrize('field,value', [('calories', -1), ('protein_g', None), ('fat_g', 'NaN'),
                                        ('carbs_g', True), ('calories', 100001)])
def test_unverifiable_saved_nutrition_cannot_be_recorded(api, field, value):
    plan = seed(api)
    request = body(api, plan)
    api.pg.sql(f"UPDATE saved_meal_plans SET meal_plan_data = jsonb_set(meal_plan_data, "
               f"'{{meal_plan,meals,dinner,items,0,{field}}}', {literal(value)}) WHERE id = '{plan}'")
    response = api.client.get(f'/meal-plan/{plan}/completion')
    assert response.status_code == 422
    assert api.client.put(f'/meal-plan/{plan}/completion', json=request).status_code in (409, 422)
    assert recorded(api) == []


def test_cosmetic_saved_row_updates_preserve_version_but_meaningful_edits_rotate(api):
    plan = seed(api)
    version = current(api, plan)['plan']['completion_version']
    api.pg.sql(f"UPDATE saved_meal_plans SET updated_at = now(), dining_hall = 'Busch Dining Hall' WHERE id = '{plan}'")
    assert current(api, plan)['plan']['completion_version'] == version
    for change in ["date = '2026-10-11'", "meal_plan_data = jsonb_set(meal_plan_data, '{meal_plan,meals,dinner,items,0,name}', '\"Replacement\"')"]:
        api.pg.sql(f"UPDATE saved_meal_plans SET {change} WHERE id = '{plan}'")
        new_version = current(api, plan)['plan']['completion_version']
        assert new_version != version
        version = new_version


def test_receipt_insert_failure_rolls_back_completion_and_can_be_retried(api):
    plan = seed(api)
    request = body(api, plan)
    api.pg.sql(f"""CREATE FUNCTION reject_completion_test() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.user_id = '{api.owner}' THEN RAISE EXCEPTION 'private database detail'; END IF; RETURN NEW; END $$;
        CREATE TRIGGER reject_completion_test BEFORE INSERT ON meal_completion_requests
        FOR EACH ROW EXECUTE FUNCTION reject_completion_test();""")
    try:
        response = api.client.put(f'/meal-plan/{plan}/completion', json=request)
        assert response.status_code == 503 and 'private database detail' not in response.text
        assert recorded(api) == []
        assert api.client.get(f"/meal-completion-requests/{request['request_id']}").status_code == 404
    finally:
        api.pg.sql('DROP TRIGGER reject_completion_test ON meal_completion_requests; DROP FUNCTION reject_completion_test();')
    assert put(api, plan, request)['revision'] == 1


def test_ambiguous_transport_response_recovers_exact_committed_outcome_without_retry(api, monkeypatch):
    plan = seed(api)
    request = body(api, plan)
    original = api.database.rpc
    writes = []

    def rpc(name, params):
        response = original(name, params)
        if name != 'meal_completion_write':
            return response
        def execute():
            writes.append(params)
            response.execute()
            raise TimeoutError('private network details')
        return SimpleNamespace(execute=execute)

    monkeypatch.setattr(api.database, 'rpc', rpc)
    response = api.client.put(f'/meal-plan/{plan}/completion', json=request)
    assert response.status_code == 503 and 'private network details' not in response.text
    assert len(writes) == 1
    receipt = api.client.get(f"/meal-completion-requests/{request['request_id']}")
    assert receipt.status_code == 200 and receipt.json()['revision'] == 1
    assert put(api, plan, request) == receipt.json()
    assert len(writes) == 1


def test_untrusted_database_roles_and_account_deletion(api):
    plan = seed(api)
    request = body(api, plan)
    put(api, plan, request)
    for role in ('anon', 'authenticated'):
        for statement in ['SELECT * FROM meal_completions', 'SELECT * FROM meal_completion_requests',
                          f"SELECT meal_completion_list('{api.owner}','{DAY}')",
                          f"SELECT meal_completion_request('{api.owner}','{request['request_id']}')",
                          f"SELECT meal_completion_intake('{api.owner}','{DAY}')"]:
            with pytest.raises(RuntimeError, match='permission denied'):
                api.pg.sql(f'SET ROLE {role}; {statement}')
    # Existing synthetic saved-plan FK has no cascade; real deletion route removes plans first.
    api.pg.sql(f"DELETE FROM saved_meal_plans WHERE user_id = '{api.owner}'; DELETE FROM users WHERE id = '{api.owner}'")
    for table in ('meal_completions', 'meal_completion_requests'):
        assert api.pg.sql(f"SELECT count(*) FROM {table} WHERE user_id = '{api.owner}'") == '0'
