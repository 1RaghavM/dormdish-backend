"""Independent admission/rollback and transactional daily-plan policy checks."""
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest

from dormdish_agent import routes
from tests.test_agent_persistence import parallel
from tests.test_agent_routes import api, route_pg, pg, stores, turn_body, events, install_model
from tests.test_agent_storage_unit import prepared_action, confirmation


@pytest.mark.parametrize('key,value', [
    ('DORMDISH_AGENT_ENABLED', 'TRUE'), ('DORMDISH_AGENT_ENABLED', '1'),
    ('DORMDISH_AGENT_KILL_SWITCH', ''), ('DORMDISH_AGENT_KILL_SWITCH', 'False'),
    ('DORMDISH_AGENT_ROLLOUT_PERCENT', '-1'), ('DORMDISH_AGENT_ROLLOUT_PERCENT', '101'),
    ('DORMDISH_AGENT_ROLLOUT_PERCENT', '5.0'), ('DORMDISH_AGENT_ROLLOUT_PERCENT', 'internal'),
])
def test_bad_admission_configuration_never_calls_storage(api, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    result = api.client.post('/agent/conversations', json={})
    assert result.status_code == 503
    assert api.database.calls == []


def test_rollout_cohorts_are_stable_and_only_expand(monkeypatch):
    monkeypatch.setenv('DORMDISH_AGENT_ENABLED', 'true')
    monkeypatch.setenv('DORMDISH_AGENT_KILL_SWITCH', 'false')
    owners = [str(UUID(int=index)) for index in range(256)]
    admitted = []
    for percent in [0, 5, 25, 100]:
        monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', str(percent))
        rounds = []
        for _ in range(2):
            accepted = set()
            for owner in owners:
                try:
                    routes.admit(owner)
                    accepted.add(owner)
                except HTTPException as error:
                    assert error.status_code == 503
            rounds.append(accepted)
        assert rounds[0] == rounds[1]
        admitted.append(rounds[0])
    assert admitted[0] == set() and admitted[-1] == set(owners)
    assert admitted[0] < admitted[1] < admitted[2] < admitted[3]


@pytest.mark.parametrize('setting', ['disabled', 'kill', 'zero_cohort'])
def test_rollback_blocks_new_writes_but_preserves_owner_recovery_and_delete(api, monkeypatch, setting):
    conversation, action = prepared_action(api.owner)
    run_id = api.pg.sql(f"SELECT id FROM agent_runs WHERE conversation_id = '{conversation.id}'")
    key, value = {'disabled': ('DORMDISH_AGENT_ENABLED', 'false'),
        'kill': ('DORMDISH_AGENT_KILL_SWITCH', 'true'),
        'zero_cohort': ('DORMDISH_AGENT_ROLLOUT_PERCENT', '0')}[setting]
    monkeypatch.setenv(key, value)
    for path, body in [('/agent/conversations', {}),
        (f'/agent/conversations/{conversation.id}/messages', turn_body()),
        (f'/agent/actions/{action.id}/confirm', confirmation().model_dump(mode='json'))]:
        assert api.client.post(path, json=body).status_code == 503
    assert api.database.calls == []
    assert api.client.get(f'/agent/conversations/{conversation.id}').status_code == 200
    assert api.client.get(f'/agent/runs/{run_id}').json()['status'] == 'complete'
    assert api.client.delete(f'/agent/conversations/{conversation.id}').status_code == 204
    assert api.client.get(f'/agent/runs/{run_id}').status_code == 404
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '0'


def seed_plan(api, *, payload=None, hall='busch-dining-hall', date='2026-10-10'):
    plan_id = uuid4()
    payload = payload or {'success': True, 'generation_id': 'old-feedback', 'ask_feedback': True,
        'meal': 'breakfast', 'meal_plan': {'meals': {'breakfast': {'description': 'Keep breakfast',
            'items': [{'name': 'Egg', 'calories': 100, 'protein_g': 10, 'fat_g': 5, 'carbs_g': 3}]}},
            'totals': {'calories': 9999}}}
    encoded = json.dumps(payload).replace("'", "''")
    api.pg.sql(f"INSERT INTO saved_meal_plans VALUES ('{plan_id}', '{api.owner.user_id}', '{hall}', '{date}', '{encoded}', now(), now())")
    return plan_id, payload


def test_apply_replaces_only_selected_meal_recalculates_totals_and_expires_with_menu(api, monkeypatch):
    plan_id, original = seed_plan(api)
    requests = install_model(monkeypatch)
    conversation = api.owner.create_conversation()
    result = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=turn_body())
    assert result.status_code == 200
    action = events(result)[-2]['data']['action']
    from datetime import datetime, timedelta
    menu_fetch = datetime.fromisoformat(api.pg.sql('SELECT fetched_at FROM menus LIMIT 1'))
    assert datetime.fromisoformat(action['expires_at']) <= menu_fetch + timedelta(hours=24)
    applied = api.client.post(f"/agent/actions/{action['id']}/confirm", json=confirmation().model_dump(mode='json'))
    assert applied.status_code == 200
    assert applied.json()['events'][0]['data']['saved_plan_id'] == str(plan_id)
    plan = api.owner.get_saved_plan(plan_id)['plan']
    payload = plan['meal_plan_data']
    assert payload['meal_plan']['meals']['breakfast'] == original['meal_plan']['meals']['breakfast']
    assert set(payload['meal_plan']['meals']) == {'breakfast', 'dinner'}
    assert payload['meal_plan']['totals'] == dict(calories=400, protein_g=16, fat_g=6.5, carbs_g=69)
    assert not {'meal', 'generation_id', 'ask_feedback'} & payload.keys()
    assert (plan['date'], plan['dining_hall']) == ('2026-10-10', 'busch-dining-hall')
    assert len(requests) == 3


@pytest.mark.parametrize('change', ['expired', 'version', 'legacy_edit'])
def test_stale_confirmation_is_http_conflict_and_does_not_modify_saved_plan(api, monkeypatch, change):
    plan_id, _ = seed_plan(api)
    install_model(monkeypatch)
    conversation = api.owner.create_conversation()
    result = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=turn_body())
    action = events(result)[-2]['data']['action']
    body = confirmation().model_dump(mode='json')
    if change == 'expired':
        api.pg.sql(f"UPDATE agent_actions SET expires_at = now() - interval '1 minute' WHERE id = '{action['id']}'")
    elif change == 'version':
        body['proposal_version'] = 2
    else:
        api.pg.sql(f"UPDATE saved_meal_plans SET meal_plan_data = '{{\"legacy_edit\":true}}' WHERE id = '{plan_id}'")
    before = api.owner.get_saved_plan(plan_id)
    applied = api.client.post(f"/agent/actions/{action['id']}/confirm", json=body)
    assert applied.status_code == 409
    assert api.owner.get_saved_plan(plan_id) == before
    assert api.pg.sql(f"SELECT count(*) FROM agent_runs WHERE id = '{body['request_id']}'") == '0'


def test_malformed_preserved_nutrition_prevents_action_creation(api, monkeypatch):
    plan_id, original = seed_plan(api, payload={'meal_plan': {'meals': {'breakfast': {'items': [
        {'name': 'Missing macros', 'calories': 100}]}}}})
    install_model(monkeypatch)
    conversation = api.owner.create_conversation()
    body = turn_body()
    result = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=body)
    assert not any(event['type'] in ('action_required', 'complete') for event in events(result))
    assert api.owner.get_run(UUID(body['request_id'])).status == 'error'
    assert api.owner.get_conversation(conversation.id).pending_actions == []
    assert api.owner.get_saved_plan(plan_id)['plan']['meal_plan_data'] == original


def test_concurrent_confirmations_of_same_action_return_one_canonical_plan(api):
    _, action = prepared_action(api.owner)
    bodies = [confirmation() for _ in range(4)]
    outcomes = parallel(*(lambda body=body: api.owner.confirm_action(action.id, body) for body in bodies))
    saved = {result.events[0].data.saved_plan_id for result in outcomes}
    assert len(saved) == 1
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '1'
    assert all(api.owner.get_run(body.request_id).status == 'complete' for body in bodies)


def test_policy_allows_different_owners_and_dates_without_global_daily_collision(api):
    owners = [api.owner, api.other, api.owner]
    actions = [prepared_action(owner)[1] for owner in owners]
    api.pg.sql(f"UPDATE agent_actions SET proposal = jsonb_set(proposal, '{{date}}', '\"2026-10-11\"') WHERE id = '{actions[2].id}'")
    results = parallel(*(lambda owner=owner, action=action: owner.confirm_action(action.id, confirmation())
                         for owner, action in zip(owners, actions)))
    assert all(not isinstance(result, str) for result in results)
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '2'
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.other.user_id}'") == '1'


def test_three_competing_halls_and_replacement_date_changes_are_rejected_atomically(api):
    actions = [prepared_action(api.owner)[1] for _ in range(3)]
    for index, action in enumerate(actions):
        api.pg.sql(f"UPDATE agent_actions SET proposal = jsonb_set(proposal, '{{dining_hall}}', '\"hall-{index}\"') WHERE id = '{action.id}'")
    results = parallel(*(lambda action=action: api.owner.confirm_action(action.id, confirmation()) for action in actions))
    assert results.count('stale_plan') == 2
    saved = next(result.events[0].data.saved_plan_id for result in results if not isinstance(result, str))
    target = api.owner.get_saved_plan(saved)
    _, replacement = prepared_action(api.owner, target=target)
    api.pg.sql(f"UPDATE agent_actions SET proposal = jsonb_set(proposal, '{{date}}', '\"2026-10-11\"') WHERE id = '{replacement.id}'")
    with pytest.raises(routes.StorageError, match='stale_plan'):
        api.owner.confirm_action(replacement.id, confirmation())
    assert api.owner.get_saved_plan(saved) == target
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '1'


def test_legacy_update_scopes_both_ownership_check_and_write(api, monkeypatch):
    import app as application
    plan_id, queries = str(uuid4()), []

    def table(name):
        assert name == 'saved_meal_plans'
        query = {'filters': []}
        queries.append(query)

        class Query:
            def select(self, columns): query['operation'] = 'select'; return self
            def update(self, payload): query['operation'] = 'update'; query['payload'] = payload; return self
            def eq(self, key, value): query['filters'].append((key, value)); return self
            def limit(self, count): return self
            def execute(self): return SimpleNamespace(data=[{'id': plan_id}])
        return Query()

    monkeypatch.setattr(application, 'supabase', SimpleNamespace(table=table))
    api.client.app.add_api_route('/meal-plan/save', application.save_meal_plan, methods=['POST'])
    result = api.client.post('/meal-plan/save', json={'plan_id': plan_id, 'dining_hall': 'busch-dining-hall',
        'date': '2026-10-10', 'meal_plan_data': {'meal_plan': {'meals': {}}}})
    assert result.status_code == 200
    assert [query['operation'] for query in queries] == ['select', 'update']
    assert all(query['filters'] == [('id', plan_id), ('user_id', api.owner.user_id)] for query in queries)
    assert queries[-1]['payload']['user_id'] == api.owner.user_id


@pytest.mark.parametrize('legacy_hall', ['busch', 'Busch Dining Hall'])
def test_existing_legacy_hall_alias_can_be_refined_without_moving_the_plan(api, monkeypatch, legacy_hall):
    plan_id, original = seed_plan(api, hall=legacy_hall)
    install_model(monkeypatch)
    conversation = api.owner.create_conversation()
    result = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=turn_body())
    assert result.status_code == 200, result.text
    action = events(result)[-2]['data']['action']
    applied = api.client.post(f"/agent/actions/{action['id']}/confirm", json=confirmation().model_dump(mode='json'))
    assert applied.status_code == 200, applied.text
    assert applied.json()['events'][0]['data']['saved_plan_id'] == str(plan_id)
    plan = api.owner.get_saved_plan(plan_id)['plan']
    assert plan['dining_hall'] == legacy_hall and plan['date'] == '2026-10-10'
    assert plan['meal_plan_data']['meal_plan']['meals']['breakfast'] == original['meal_plan']['meals']['breakfast']


@pytest.mark.parametrize('other_hall', ['livingston', 'Livingston Dining Commons', 'livingston-dining-commons', 'Busch Dining Hall Annex'])
def test_alias_resolution_must_not_accept_another_or_unregistered_hall(api, monkeypatch, other_hall):
    plan_id, original = seed_plan(api, hall=other_hall)
    requests = install_model(monkeypatch)
    conversation = api.owner.create_conversation()
    body = turn_body()
    result = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=body)
    assert result.status_code == 409
    assert requests == []
    assert api.owner.get_saved_plan(plan_id)['plan']['meal_plan_data'] == original
    assert api.owner.get_conversation(conversation.id).pending_actions == []
    assert api.owner.get_run(UUID(body['request_id'])).status == 'error'


@pytest.mark.parametrize('field,value', [('user_id', str(uuid4())), ('meal_plan_data', {}),
                                       ('target_plan_id', str(uuid4()))])
def test_confirmation_never_accepts_client_selected_owner_or_plan_payload(api, field, value):
    _, action = prepared_action(api.owner)
    body = confirmation().model_dump(mode='json') | {field: value}
    result = api.client.post(f'/agent/actions/{action.id}/confirm', json=body)
    assert result.status_code == 422
    assert api.database.calls == []
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '0'


def test_actual_legacy_route_cannot_save_over_another_owners_existing_id(api, monkeypatch):
    import app as application
    foreign = SimpleNamespace(owner=api.other, pg=api.pg)
    plan_id, original = seed_plan(foreign)
    monkeypatch.setattr(application, 'supabase', api.database)
    api.client.app.add_api_route('/meal-plan/save', application.save_meal_plan, methods=['POST'])
    result = api.client.post('/meal-plan/save', json={'plan_id': str(plan_id), 'dining_hall': 'busch',
        'date': '2026-10-10', 'meal_plan_data': {'malicious_replacement': True}})
    assert result.status_code == 404
    assert api.other.get_saved_plan(plan_id)['plan']['meal_plan_data'] == original
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '0'
