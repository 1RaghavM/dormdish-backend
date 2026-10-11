"""Independent completion API/SQL tests: synthetic rows, real transactions, no models."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
    legacy_owner, legacy_plan = str(uuid4()), str(uuid4())
    database.sql(f"INSERT INTO users(id) VALUES ('{legacy_owner}'); INSERT INTO saved_meal_plans(id,user_id,date,meal_plan_data) VALUES ('{legacy_plan}','{legacy_owner}','{DAY}', '{{\"legacy\":true}}');")
    database.sql((ROOT / 'migrations/add_meal_completion.sql').read_text())
    database.legacy_plan = legacy_plan
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
                          f"SELECT meal_completion_intake('{api.owner}','{DAY}')",
                          f"SELECT meal_completion_write('{api.owner}','{plan}',{literal({'plan_id': plan, **request})},'[]')"]:
            with pytest.raises(RuntimeError, match='permission denied'):
                api.pg.sql(f'SET ROLE {role}; {statement}')
    # Existing synthetic saved-plan FK has no cascade; real deletion route removes plans first.
    api.pg.sql(f"DELETE FROM saved_meal_plans WHERE user_id = '{api.owner}'; DELETE FROM users WHERE id = '{api.owner}'")
    for table in ('meal_completions', 'meal_completion_requests'):
        assert api.pg.sql(f"SELECT count(*) FROM {table} WHERE user_id = '{api.owner}'") == '0'


@pytest.mark.parametrize('path,value', [
    ([], []), (['meal_plan'], []), (['meal_plan', 'meals', 'dinner'], []),
    (['meal_plan', 'meals', 'dinner', 'items', '0'], None),
])
def test_malformed_saved_shapes_report_validation_error_not_service_outage(api, path, value):
    plan = seed(api)
    if path:
        expression = f"jsonb_set(meal_plan_data, '{{{','.join(path)}}}', {literal(value)})"
    else:
        expression = literal(value)
    api.pg.sql(f"UPDATE saved_meal_plans SET meal_plan_data = {expression} WHERE id = '{plan}'")
    response = api.client.get(f'/meal-plan/{plan}/completion')
    assert response.status_code == 422, response.text
    assert recorded(api) == []


def test_edit_between_snapshot_read_and_transaction_rejects_stale_indices(api, monkeypatch):
    plan = seed(api)
    request = body(api, plan)
    original = api.database.rpc
    writes = []

    def rpc(name, params):
        if name == 'meal_completion_write':
            writes.append(params)
            api.pg.sql(f"UPDATE saved_meal_plans SET meal_plan_data = jsonb_set(meal_plan_data, "
                       f"'{{meal_plan,meals,dinner,items,0,name}}', '\"Changed after read\"') WHERE id = '{plan}'")
        return original(name, params)

    monkeypatch.setattr(api.database, 'rpc', rpc)
    put(api, plan, request, 409)
    assert len(writes) == 1 and recorded(api) == []
    assert api.client.get(f"/meal-completion-requests/{request['request_id']}").status_code == 404


def test_owner_change_rotates_version_and_never_transfers_old_consumption(api):
    plan = seed(api)
    request = body(api, plan)
    state = put(api, plan, request)
    api.pg.sql(f"UPDATE saved_meal_plans SET user_id = '{api.other}' WHERE id = '{plan}'")
    assert api.client.get(f'/meal-plan/{plan}/completion').status_code == 404
    assert recorded(api) == [state]
    assert intake(api)['remaining_targets']['calories'] == 1700
    api.client.headers['Authorization'] = 'Bearer other'
    assert current(api, plan)['plan']['completion_version'] != request['plan_version']
    assert current(api, plan)['state']['revision'] == 0
    assert recorded(api) == []


def test_over_500_food_logs_fail_closed_instead_of_truncating_intake(api):
    api.pg.sql(f"""INSERT INTO food_log(id,user_id,date,calories,protein_g,fat_g,carbs_g)
        SELECT gen_random_uuid()::text,'{api.owner}','{DAY}',1,0,0,0 FROM generate_series(1,501)""")
    context = FoodContext(api.database, api.owner, 'rutgers',
        PlanningContext(dining_hall='busch', date=date.fromisoformat(DAY), meal_type='dinner'),
        Nutrition(calories=2000, protein_g=100, fat_g=70, carbs_g=200))
    result = asyncio.run(read_intake.on_invoke_tool(RunContextWrapper(context), '{}'))
    assert isinstance(result, str) and 'could not verify' in result


def test_initial_state_is_empty_and_saved_rows_keep_legacy_data(api):
    plan = seed(api)
    before = json.loads(api.pg.sql(f"SELECT meal_plan_data FROM saved_meal_plans WHERE id = '{plan}'"))
    snapshot = current(api, plan)
    assert snapshot['state']['revision'] == 0
    assert snapshot['state']['items'] == snapshot['state']['selected_items'] == []
    assert snapshot['state']['totals'] == ZERO
    put(api, plan, body(api, plan))
    assert json.loads(api.pg.sql(f"SELECT meal_plan_data FROM saved_meal_plans WHERE id = '{plan}'")) == before


def test_multiple_versions_count_actual_consumption_and_undo_only_selected_version(api):
    plan = seed(api)
    old_request = body(api, plan)
    old = put(api, plan, old_request)
    api.pg.sql(f"UPDATE saved_meal_plans SET meal_plan_data = jsonb_set(meal_plan_data, "
               f"'{{meal_plan,meals,dinner,items,0,calories}}', '450') WHERE id = '{plan}'")
    new_request = body(api, plan)
    new = put(api, plan, new_request)
    assert len(recorded(api)) == 2
    assert {state['plan_version'] for state in recorded(api)} == {old['plan_version'], new['plan_version']}
    assert intake(api)['remaining_targets']['calories'] == 1250
    put(api, plan, {**old_request, 'request_id': str(uuid4()), 'expected_revision': 1,
                    'mode': 'frozen', 'selected_items': []})
    assert intake(api)['remaining_targets']['calories'] == 1550
    assert current(api, plan)['state'] == new


def test_receipts_remain_exact_after_old_age_and_frozen_undo_can_be_rechecked(api):
    plan = seed(api)
    request = body(api, plan)
    original = put(api, plan, request)
    api.pg.sql(f"UPDATE meal_completion_requests SET created_at = now() - interval '2 years' WHERE user_id = '{api.owner}'; "
               f"DELETE FROM saved_meal_plans WHERE id = '{plan}'")
    assert put(api, plan, request) == original
    undo = {**request, 'request_id': str(uuid4()), 'expected_revision': 1, 'mode': 'frozen', 'selected_items': []}
    put(api, plan, undo)
    recheck = put(api, plan, {**request, 'request_id': str(uuid4()), 'expected_revision': 2, 'mode': 'frozen'})
    assert recheck['items'] == original['items'] and recheck['totals'] == RICE
    assert recheck['revision'] == 3


def test_selection_order_is_canonical_for_exact_duplicate_replay(api):
    plan = seed(api)
    request = body(api, plan, selected=(1, 0))
    original = put(api, plan, request)
    assert original['selected_items'] == [{'meal_type': 'dinner', 'index': 0}, {'meal_type': 'dinner', 'index': 1}]
    assert put(api, plan, {**request, 'selected_items': list(reversed(request['selected_items']))}) == original


def test_rpc_rejects_tampered_server_catalog_without_partial_write(api):
    plan = seed(api)
    request = body(api, plan)
    catalog = current(api, plan)['available_items']
    catalog[0]['totals']['calories'] = 1
    with pytest.raises(RuntimeError, match='completion_invalid'):
        api.pg.rpc('meal_completion_write', {'p_user_id': api.owner, 'p_plan_id': plan,
                   'p_request': {'plan_id': plan, **request}, 'p_items': catalog}).execute()
    assert recorded(api) == []
    assert api.client.get(f"/meal-completion-requests/{request['request_id']}").status_code == 404


def test_additive_migration_preserves_preexisting_saved_rows(completion_pg):
    row = json.loads(completion_pg.sql(f"SELECT to_jsonb(p) FROM saved_meal_plans p WHERE id = '{completion_pg.legacy_plan}'"))
    assert row['meal_plan_data'] == {'legacy': True}
    assert row['date'] == DAY
    from uuid import UUID
    assert UUID(row['completion_version'])


@pytest.mark.parametrize('conflicting', [False, True])
def test_duplicate_recovers_when_original_commits_and_plan_disappears_after_receipt_read(api, monkeypatch, conflicting):
    from services.meal_completion import CompletionRequest, CompletionStore
    plan = seed(api)
    request = body(api, plan)
    original_rpc = api.database.rpc
    committed = []
    store = CompletionStore(RouteDatabase(api.pg), api.owner)

    def rpc(name, params):
        if name == 'meal_completion_current' and not committed:
            committing = {**request, 'selected_items': [{'meal_type': 'dinner', 'index': 1}]} if conflicting else request
            committed.append(store.write(plan, CompletionRequest(**committing)))
            api.pg.sql(f"DELETE FROM saved_meal_plans WHERE id = '{plan}'")
        return original_rpc(name, params)

    monkeypatch.setattr(api.database, 'rpc', rpc)
    response = api.client.put(f'/meal-plan/{plan}/completion', json=request)
    assert response.status_code == (409 if conflicting else 200), response.text
    if not conflicting:
        assert response.json() == committed[0]
    assert recorded(api) == committed
    assert api.client.get(f"/meal-completion-requests/{request['request_id']}").json() == committed[0]


def test_rolling_write_quota_accepts_1000th_replays_and_rejects_without_mutation(api):
    plan = seed(api)
    original = body(api, plan)
    first = put(api, plan, original)
    # Bulk synthetic receipts represent earlier writes; one aged receipt is outside the rolling window.
    api.pg.sql(f"""INSERT INTO meal_completion_requests(user_id, request_id, request, state)
        SELECT '{api.owner}', gen_random_uuid(), request, state FROM meal_completion_requests,
            generate_series(1, 998) WHERE request_id = '{original['request_id']}';
        INSERT INTO meal_completion_requests(user_id, request_id, request, state, created_at)
        SELECT '{api.owner}', gen_random_uuid(), request, state, now() - interval '25 hours'
            FROM meal_completion_requests WHERE request_id = '{original['request_id']}';
        INSERT INTO meal_completion_requests(user_id, request_id, request, state)
        SELECT '{api.other}', gen_random_uuid(), request, state FROM meal_completion_requests,
            generate_series(1, 500) WHERE request_id = '{original['request_id']}';""")
    last = {**original, 'request_id': str(uuid4()), 'expected_revision': 1, 'selected_items': []}
    final = put(api, plan, last)
    assert final['revision'] == 2 and final['totals'] == ZERO
    before = recorded(api)
    receipts = api.pg.sql(f"SELECT count(*) FROM meal_completion_requests WHERE user_id = '{api.owner}'")
    assert receipts == '1001'  # 1,000 recent, one expired.
    rejected = {**original, 'request_id': str(uuid4()), 'expected_revision': 2}
    put(api, plan, rejected, 429)
    assert recorded(api) == before
    assert api.pg.sql(f"SELECT count(*) FROM meal_completion_requests WHERE user_id = '{api.owner}'") == receipts
    assert api.client.get(f"/meal-completion-requests/{rejected['request_id']}").status_code == 404
    assert put(api, plan, original) == first and put(api, plan, last) == final
    # The rolling write budget belongs to the owner, not the selected meal date.
    tomorrow = seed(api, day='2026-10-11')
    tomorrow_request = body(api, tomorrow, date='2026-10-11')
    put(api, tomorrow, tomorrow_request, 429)
    assert recorded(api, '2026-10-11') == []
    assert api.client.get(f"/meal-completion-requests/{tomorrow_request['request_id']}").status_code == 404
    api.client.headers['Authorization'] = 'Bearer other'
    other_plan = seed(api, owner=api.other)
    assert put(api, other_plan, body(api, other_plan))['revision'] == 1
    # Aging one counted receipt opens exactly one slot without deleting its replay history.
    api.client.headers['Authorization'] = 'Bearer owner'
    api.pg.sql(f"UPDATE meal_completion_requests SET created_at = now() - interval '25 hours' "
               f"WHERE user_id = '{api.owner}' AND request_id = '{original['request_id']}'")
    assert put(api, plan, rejected)['revision'] == 3
    assert put(api, plan, original) == first
    put(api, plan, {**original, 'request_id': str(uuid4()), 'expected_revision': 3}, 429)


def test_version_quota_accepts_100th_allows_updates_and_is_owner_date_scoped(api):
    # Deleted historical plans legitimately retain completion rows without a saved-plan FK.
    api.pg.sql(f"""INSERT INTO meal_completions(user_id, plan_id, plan_version, date,
            revision, recorded_items, selected_items)
        SELECT '{api.owner}', gen_random_uuid(), gen_random_uuid(), '{DAY}', 1, '[]', '[]'
            FROM generate_series(1, 99);
        INSERT INTO meal_completions(user_id, plan_id, plan_version, date,
            revision, recorded_items, selected_items)
        SELECT '{api.other}', gen_random_uuid(), gen_random_uuid(), '{DAY}', 1, '[]', '[]'
            FROM generate_series(1, 50);""")
    plan = seed(api)
    request = body(api, plan)
    first = put(api, plan, request)
    assert len(recorded(api)) == 100
    update = {**request, 'request_id': str(uuid4()), 'expected_revision': 1, 'selected_items': []}
    final = put(api, plan, update)
    assert final['revision'] == 2 and final['totals'] == ZERO
    before = recorded(api)
    overflow = seed(api)
    rejected = body(api, overflow)
    put(api, overflow, rejected, 429)
    assert recorded(api) == before
    assert current(api, overflow)['state']['revision'] == 0
    assert api.pg.sql(f"SELECT count(*) FROM meal_completion_requests WHERE user_id = '{api.owner}'") == '2'
    assert api.client.get(f"/meal-completion-requests/{rejected['request_id']}").status_code == 404
    assert put(api, plan, request) == first and put(api, plan, update) == final
    tomorrow = seed(api, day='2026-10-11')
    assert put(api, tomorrow, body(api, tomorrow, date='2026-10-11'))['revision'] == 1
    assert len(recorded(api)) == 100 and len(recorded(api, '2026-10-11')) == 1
    api.client.headers['Authorization'] = 'Bearer other'
    other_plan = seed(api, owner=api.other)
    assert put(api, other_plan, body(api, other_plan))['revision'] == 1
    assert len(recorded(api)) == 51
