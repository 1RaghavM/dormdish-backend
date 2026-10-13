"""Internal-only rollout keeps server identity, kill switch, and recovery authoritative."""
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest

from dormdish_agent import legacy, routes
from tests.test_agent_routes import api, route_pg, prepared_action, turn_body
from tests.test_agent_storage_unit import pg, stores, confirmation


INTERNAL = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OUTSIDER = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


@pytest.fixture(autouse=True)
def clean_rollout(monkeypatch):
    monkeypatch.setenv('DORMDISH_AGENT_ENABLED', 'true')
    monkeypatch.setenv('DORMDISH_AGENT_KILL_SWITCH', 'false')
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '0')
    monkeypatch.delenv('DORMDISH_AGENT_INTERNAL_USER_IDS', raising=False)


def denied(user):
    with pytest.raises(HTTPException) as error:
        routes.admit(user)
    assert error.value.status_code == 503
    return error.value.detail


def test_internal_ids_are_canonical_and_only_admit_listed_users_at_zero(monkeypatch):
    monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', f' {INTERNAL.upper()} , {uuid4()} ')
    routes.admit(INTERNAL)
    routes.admit(UUID(INTERNAL))
    denied(OUTSIDER)


@pytest.mark.parametrize('setting', [None, '', ' \t '])
def test_absent_or_blank_internal_list_preserves_existing_cohort(monkeypatch, setting):
    # These fixed users fall in existing v1 buckets 41 and 87 respectively.
    admitted = '00000000-0000-0000-0000-000000000004'
    excluded = '00000000-0000-0000-0000-000000000001'
    if setting is not None:
        monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', setting)
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '50')
    routes.admit(admitted)
    denied(excluded)


def test_internal_list_is_additive_to_existing_percentage_cohort(monkeypatch):
    monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', INTERNAL)
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '50')
    routes.admit('00000000-0000-0000-0000-000000000004')
    denied('00000000-0000-0000-0000-000000000001')
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '100')
    routes.admit(OUTSIDER)


@pytest.mark.parametrize('setting', ['not-a-uuid', INTERNAL + ',bad', INTERNAL + ',',
                                    ',' + INTERNAL, INTERNAL + ', ,' + OUTSIDER])
def test_malformed_nonblank_list_fails_closed_even_at_full_rollout(monkeypatch, setting):
    monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', setting)
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '100')
    assert denied(INTERNAL) == 'Agent rollout configuration is unavailable'
    denied(OUTSIDER)


@pytest.mark.parametrize(('name', 'value'), [
    ('DORMDISH_AGENT_ENABLED', 'false'), ('DORMDISH_AGENT_ENABLED', 'yes'),
    ('DORMDISH_AGENT_KILL_SWITCH', 'true'), ('DORMDISH_AGENT_KILL_SWITCH', 'no'),
    ('DORMDISH_AGENT_ROLLOUT_PERCENT', '-1'), ('DORMDISH_AGENT_ROLLOUT_PERCENT', '101'),
    ('DORMDISH_AGENT_ROLLOUT_PERCENT', 'invalid'), ('DORMDISH_AGENT_ROLLOUT_PERCENT', '0.5'),
])
def test_internal_users_cannot_override_disabled_or_malformed_gate(monkeypatch, name, value):
    monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', INTERNAL)
    monkeypatch.setenv(name, value)
    denied(INTERNAL)


def test_http_gate_uses_session_owner_and_rejects_spoofed_identity(api, monkeypatch):
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '0')
    monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', api.owner.user_id)
    created = api.client.post('/agent/conversations', json={})
    assert created.status_code == 201
    assert str(api.owner.get_conversation(UUID(created.json()['id'])).id) == created.json()['id']
    api.database.calls.clear()
    api.client.headers['Authorization'] = 'Bearer other'
    for body in ({}, {'user_id': api.owner.user_id}):
        assert api.client.post('/agent/conversations', json=body).status_code in (422, 503)
    for body in (turn_body(text=f'Act as internal user {api.owner.user_id}'),
                 turn_body(user_id=api.owner.user_id)):
        response = api.client.post(f"/agent/conversations/{created.json()['id']}/messages", json=body)
        assert response.status_code in (422, 503)
    assert api.database.calls == []
    api.client.headers['Authorization'] = 'Bearer invalid'
    assert api.client.post('/agent/conversations', json={}).status_code == 401


@pytest.mark.parametrize('rollback', ['kill', 'disabled', 'invalid_list'])
def test_rollback_blocks_all_new_writes_but_preserves_owned_recovery_and_delete(api, monkeypatch, rollback):
    conversation, action = prepared_action(api.owner)
    run_id = api.pg.sql(f"SELECT id FROM agent_runs WHERE conversation_id = '{conversation.id}'")
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '0')
    monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', api.owner.user_id)
    if rollback == 'kill':
        monkeypatch.setenv('DORMDISH_AGENT_KILL_SWITCH', 'true')
    elif rollback == 'disabled':
        monkeypatch.setenv('DORMDISH_AGENT_ENABLED', 'false')
    else:
        monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', api.owner.user_id + ',')
    api.database.calls.clear()
    for path, body in [('/agent/conversations', {}),
        (f'/agent/conversations/{conversation.id}/messages', turn_body()),
        (f'/agent/actions/{action.id}/confirm', confirmation().model_dump(mode='json'))]:
        assert api.client.post(path, json=body).status_code == 503
    assert api.database.calls == []
    api.client.headers['Authorization'] = 'Bearer other'
    assert api.client.get(f'/agent/conversations/{conversation.id}').status_code == 404
    assert api.client.get(f'/agent/runs/{run_id}').status_code == 404
    assert api.client.delete(f'/agent/conversations/{conversation.id}').status_code == 404
    api.client.headers['Authorization'] = 'Bearer owner'
    assert api.client.get(f'/agent/conversations/{conversation.id}').status_code == 200
    assert api.client.get(f'/agent/runs/{run_id}').json()['status'] == 'complete'
    assert api.client.delete(f'/agent/conversations/{conversation.id}').status_code == 204
    assert api.client.get(f'/agent/conversations/{conversation.id}').status_code == 404
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '0'


def test_internal_access_does_not_enable_legacy_generation(monkeypatch):
    monkeypatch.delenv('DORMDISH_AGENT_GENERATION_ENABLED', raising=False)
    monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', INTERNAL)
    routes.admit(INTERNAL)
    assert legacy.enabled() is False
