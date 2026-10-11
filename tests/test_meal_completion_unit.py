"""Small implementation self-checks; companion tests own independent acceptance."""
import json
import re
from uuid import uuid4

import pytest
from fastapi import HTTPException

from services.meal_completion import CompletionRequest, CompletionStore, saved_items
from tests.test_agent_storage_unit import pg


def plan(owner, plan_id=None):
    return {'id': str(plan_id or uuid4()), 'user_id': str(owner), 'date': '2026-10-10',
        'completion_version': str(uuid4()), 'meal_plan_data': {'meal_plan': {'meals': {'dinner': {
            'items': [{'name': 'Rice', 'servings': 2, 'calories': 400, 'protein_g': 8, 'fat_g': 2, 'carbs_g': 88},
                      {'name': 'Beans', 'calories': 100, 'protein_g': 6, 'fat_g': 0, 'carbs_g': 20}]}}}}}


def test_snapshot_amounts_are_not_multiplied_and_requests_reject_duplicates():
    row = plan(uuid4())
    assert saved_items(row)[0]['totals']['calories'] == 400
    request = dict(request_id=str(uuid4()), date=row['date'], plan_version=row['completion_version'],
        expected_revision=0, selected_items=[{'meal_type': 'dinner', 'index': 0}] * 2)
    with pytest.raises(ValueError, match='Duplicate'): CompletionRequest(**request)
    row['meal_plan_data']['meal_plan']['meals']['dinner']['items'][0]['fat_g'] = float('nan')
    with pytest.raises(ValueError): saved_items(row)


@pytest.fixture(scope='module')
def completion_pg(pg):
    pg.sql('CREATE TABLE food_log(id uuid PRIMARY KEY, user_id uuid REFERENCES users(id), date text, calories numeric, protein_g numeric, fat_g numeric, carbs_g numeric); GRANT SELECT ON food_log TO service_role;')
    from pathlib import Path
    pg.sql((Path(__file__).resolve().parents[1] / 'migrations/add_meal_completion.sql').read_text())
    original = pg.sql
    def sql(query):
        try: return original(query)
        except RuntimeError as error:
            match = re.search(r'ERROR:\s+(completion_\w+)', str(error))
            if match: error.message = match[1]
            raise
    pg.sql = sql
    return pg


def test_pg_version_guard_frozen_intake_and_exact_replay(completion_pg):
    pg = completion_pg
    owner = uuid4()
    row = plan(owner)
    pg.sql(f"INSERT INTO users VALUES ('{owner}')")
    payload = json.dumps(row['meal_plan_data'])
    pg.sql(f"INSERT INTO saved_meal_plans(id,user_id,date,meal_plan_data) VALUES ('{row['id']}','{owner}','{row['date']}','{payload}')")
    store = CompletionStore(pg, owner)
    current = store.current(row['id'])
    version = current['plan']['completion_version']
    body = CompletionRequest(request_id=uuid4(), date=row['date'], plan_version=version,
        expected_revision=0, selected_items=[{'meal_type': 'dinner', 'index': 0}])
    state = store.write(row['id'], body)
    assert state['revision'] == 1 and state['recorded_items'][0]['totals']['calories'] == 400
    assert store.write(row['id'], body) == state
    pg.sql(f"UPDATE saved_meal_plans SET meal_plan_data = '{{}}' WHERE id = '{row['id']}'")
    assert store.write(row['id'], body) == state
    with pytest.raises(HTTPException) as conflict:
        store.write(row['id'], body.model_copy(update={'request_id': uuid4(), 'expected_revision': 1}))
    assert conflict.value.status_code in (409, 422)
    evidence = store.rpc('meal_completion_intake', p_date=row['date'])
    assert evidence['completions'][0]['totals']['calories'] == 400
    cleared = body.model_copy(update={'request_id': uuid4(), 'expected_revision': 1, 'mode': 'frozen', 'selected_items': []})
    assert store.write(row['id'], cleared)['revision'] == 2
    assert store.rpc('meal_completion_intake', p_date=row['date'])['completions'] == []
    assert store.receipt(body.request_id)['state'] == state
