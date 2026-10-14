"""Exact client-version admission never prevents owned reconciliation or deletion."""
from uuid import UUID

from fastapi import HTTPException
import pytest

from tests.test_agent_routes import api, route_pg, prepared_action, turn_body, events
from tests.test_agent_storage_unit import pg, stores, confirmation


DISABLED = dict(conversation_enabled=False, completion_enabled=False,
                preference_editing_enabled=False)


@pytest.fixture(autouse=True)
def version_configuration(monkeypatch):
    monkeypatch.delenv('DORMDISH_AGENT_APP_VERSION', raising=False)
    monkeypatch.delenv('DORMDISH_AGENT_INTERNAL_USER_IDS', raising=False)
    monkeypatch.setenv('DORMDISH_COMPLETION_ENABLED', 'true')
    monkeypatch.setenv('DORMDISH_EXPLICIT_MEMORY_ENABLED', 'true')


@pytest.mark.parametrize('version', [None, '', '2.2.3', '2.2.4', '2.2.6', '2.2.50', 'v2.2.5', ' 2.2.5', '2.2.5 '])
def test_wrong_version_hides_capabilities_and_blocks_all_mutations_before_storage(api, version):
    conversation, action = prepared_action(api.owner)
    api.client.headers.pop('X-App-Version', None)
    if version is not None:
        api.client.headers['X-App-Version'] = version
    response = api.client.get('/agent/availability')
    assert response.status_code == 200 and response.json() == DISABLED
    assert response.headers['cache-control'] == 'no-store'
    for path, body in [('/agent/conversations', {}),
        (f'/agent/conversations/{conversation.id}/messages', turn_body()),
        (f'/agent/actions/{action.id}/confirm', confirmation().model_dump(mode='json'))]:
        assert api.client.post(path, json=body).status_code == 503
    assert api.database.calls == []
    assert api.owner.get_conversation(conversation.id).pending_actions[0].id == action.id
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '0'


@pytest.mark.parametrize('version', [None, '2.2.3', '2.2.4', '2.2.6'])
def test_outdated_client_can_recover_and_delete_only_its_own_data(api, version):
    conversation, _ = prepared_action(api.owner)
    run_id = api.pg.sql(f"SELECT id FROM agent_runs WHERE conversation_id = '{conversation.id}'")
    api.client.headers.pop('X-App-Version', None)
    if version:
        api.client.headers['X-App-Version'] = version
    api.client.headers['Authorization'] = 'Bearer other'
    for method, path in [('GET', f'/agent/conversations/{conversation.id}'),
                         ('GET', f'/agent/runs/{run_id}'),
                         ('DELETE', f'/agent/conversations/{conversation.id}')]:
        assert api.client.request(method, path).status_code == 404
    api.client.headers['Authorization'] = 'Bearer owner'
    assert api.client.get(f'/agent/conversations/{conversation.id}').status_code == 200
    assert api.client.get(f'/agent/runs/{run_id}').json()['status'] == 'complete'
    assert api.client.delete(f'/agent/conversations/{conversation.id}').status_code == 204
    assert api.client.get(f'/agent/conversations/{conversation.id}').status_code == 404


@pytest.mark.parametrize('token', ['owner', 'other'])
def test_current_version_admits_all_authenticated_owners_at_full_rollout(api, token):
    api.client.headers.update({'Authorization': 'Bearer ' + token, 'X-App-Version': '2.2.5'})
    assert api.client.get('/agent/availability').json() == dict.fromkeys(DISABLED, True)
    created = api.client.post('/agent/conversations', json={})
    assert created.status_code == 201
    owner = getattr(api, token)
    assert str(owner.get_conversation(UUID(created.json()['id'])).id) == created.json()['id']
    response = api.client.post(f"/agent/conversations/{created.json()['id']}/messages",
                              json=turn_body(context=None))
    assert response.status_code == 200 and events(response)[-1]['type'] == 'complete'
    _, action = prepared_action(owner)
    applied = api.client.post(f'/agent/actions/{action.id}/confirm',
                             json=confirmation().model_dump(mode='json'))
    assert applied.status_code == 200 and applied.json()['events'][-1]['data']['saved_plan_id']


@pytest.mark.parametrize('configured', ['', ' ', '2.2.4', '2.3.0'])
def test_configured_version_is_exact_and_blank_fails_closed(api, monkeypatch, configured):
    monkeypatch.setenv('DORMDISH_AGENT_APP_VERSION', configured)
    api.client.headers['X-App-Version'] = '2.2.5'
    assert api.client.get('/agent/availability').json() == DISABLED
    assert api.client.post('/agent/conversations', json={}).status_code == 503
    api.client.headers['X-App-Version'] = configured
    expected = bool(configured.strip())
    assert api.client.get('/agent/availability').json()['conversation_enabled'] is expected
    assert api.client.post('/agent/conversations', json={}).status_code == (201 if expected else 503)


@pytest.mark.parametrize('gate', ['auth', 'premium', 'kill', 'disabled', 'cohort'])
def test_matching_version_does_not_bypass_existing_admission(api, monkeypatch, gate):
    import app as application
    api.client.headers['X-App-Version'] = '2.2.5'
    if gate == 'auth':
        api.client.headers['Authorization'] = 'Bearer invalid'
    elif gate == 'premium':
        def denied():
            raise HTTPException(403, 'Premium required')
        api.client.app.dependency_overrides[application.require_premium] = denied
    else:
        name, value = {'kill': ('DORMDISH_AGENT_KILL_SWITCH', 'true'),
                       'disabled': ('DORMDISH_AGENT_ENABLED', 'false'),
                       'cohort': ('DORMDISH_AGENT_ROLLOUT_PERCENT', '0')}[gate]
        monkeypatch.setenv(name, value)
    expected = {'auth': 401, 'premium': 403}.get(gate, 503)
    assert api.client.post('/agent/conversations', json={}).status_code == expected
    availability = api.client.get('/agent/availability')
    assert availability.status_code == (expected if gate in ('auth', 'premium') else 200)
    if availability.status_code == 200:
        assert availability.json() == DISABLED
    assert api.database.calls == []
