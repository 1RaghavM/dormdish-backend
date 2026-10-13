"""Native discovery must reflect authenticated server gates without starting work."""
from itertools import product

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from dormdish_agent import routes


OWNER = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
DISABLED = dict(conversation_enabled=False, completion_enabled=False,
                preference_editing_enabled=False)


@pytest.fixture
def client(monkeypatch):
    import app as application

    for name in ('DORMDISH_AGENT_ENABLED', 'DORMDISH_AGENT_KILL_SWITCH',
                 'DORMDISH_AGENT_ROLLOUT_PERCENT', 'DORMDISH_AGENT_INTERNAL_USER_IDS',
                 'DORMDISH_COMPLETION_ENABLED', 'DORMDISH_EXPLICIT_MEMORY_ENABLED',
                 'DORMDISH_AGENT_GENERATION_ENABLED'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('DORMDISH_AGENT_ENABLED', 'true')
    monkeypatch.setenv('DORMDISH_AGENT_INTERNAL_USER_IDS', OWNER)
    profiles = {'owner': {'id': OWNER}, 'other': {'id': OTHER},
                'cohort': {'id': '00000000-0000-0000-0000-000000000004'},
                'excluded': {'id': '00000000-0000-0000-0000-000000000001'}}
    monkeypatch.setattr(application, 'verify_session_token', profiles.get)

    def unexpected(*args, **kwargs):
        pytest.fail('Availability must not initialize storage, targets, or a model')

    monkeypatch.setattr(routes, 'AgentStore', unexpected)
    monkeypatch.setattr(routes, 'AsyncOpenAI', unexpected)
    app = FastAPI()
    app.include_router(routes.create_router(application.get_current_user,
        application.require_premium, unexpected, unexpected))
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer owner'
        yield client


@pytest.mark.parametrize('header', [None, 'Bearer expired', 'Basic owner', 'Bearer '])
def test_availability_requires_real_auth_dependency(client, header):
    client.headers.pop('Authorization')
    if header is not None:
        client.headers['Authorization'] = header
    assert client.get('/agent/availability').status_code == 401


@pytest.mark.parametrize(('completion', 'memory'), list(product((False, True), repeat=2)))
def test_availability_exposes_only_enabled_capabilities(client, monkeypatch, completion, memory):
    monkeypatch.setenv('DORMDISH_COMPLETION_ENABLED', str(completion).lower())
    monkeypatch.setenv('DORMDISH_EXPLICIT_MEMORY_ENABLED', str(memory).lower())
    response = client.get('/agent/availability')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == dict(conversation_enabled=True, completion_enabled=completion,
                                   preference_editing_enabled=memory)


@pytest.mark.parametrize(('name', 'value'), [
    ('DORMDISH_AGENT_ENABLED', None), ('DORMDISH_AGENT_ENABLED', 'false'),
    ('DORMDISH_AGENT_ENABLED', 'yes'), ('DORMDISH_AGENT_KILL_SWITCH', 'true'),
    ('DORMDISH_AGENT_KILL_SWITCH', 'no'), ('DORMDISH_AGENT_ROLLOUT_PERCENT', '-1'),
    ('DORMDISH_AGENT_ROLLOUT_PERCENT', '101'), ('DORMDISH_AGENT_ROLLOUT_PERCENT', 'invalid'),
    ('DORMDISH_AGENT_INTERNAL_USER_IDS', OWNER + ',bad'),
])
def test_disabled_or_invalid_rollout_hides_every_capability(client, monkeypatch, name, value):
    monkeypatch.setenv('DORMDISH_COMPLETION_ENABLED', 'true')
    monkeypatch.setenv('DORMDISH_EXPLICIT_MEMORY_ENABLED', 'true')
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    response = client.get('/agent/availability')
    assert response.status_code == 200
    assert response.json() == DISABLED


def test_authenticated_owner_controls_availability_not_client_claims(client, monkeypatch):
    monkeypatch.setenv('DORMDISH_COMPLETION_ENABLED', 'true')
    monkeypatch.setenv('DORMDISH_EXPLICIT_MEMORY_ENABLED', 'true')
    client.headers['Authorization'] = 'Bearer other'
    client.headers['X-User-ID'] = OWNER
    response = client.request('GET', '/agent/availability', params={'user_id': OWNER},
                              json={'user_id': OWNER, 'conversation_enabled': True})
    assert response.status_code == 200
    assert response.json() == DISABLED


def test_percentage_cohort_remains_available_without_internal_membership(client, monkeypatch):
    monkeypatch.delenv('DORMDISH_AGENT_INTERNAL_USER_IDS')
    monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '50')
    for token, expected in [('cohort', True), ('excluded', False)]:
        client.headers['Authorization'] = 'Bearer ' + token
        response = client.get('/agent/availability')
        assert response.status_code == 200
        assert response.json() == {**DISABLED, 'conversation_enabled': expected}


@pytest.mark.parametrize('value', [None, 'false', 'TRUE', 'yes'])
def test_optional_capabilities_require_exact_true_without_affecting_chat(client, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv('DORMDISH_COMPLETION_ENABLED', value)
        monkeypatch.setenv('DORMDISH_EXPLICIT_MEMORY_ENABLED', value)
    monkeypatch.setenv('DORMDISH_AGENT_GENERATION_ENABLED', 'true')
    response = client.get('/agent/availability')
    assert response.status_code == 200
    assert response.json() == {**DISABLED, 'conversation_enabled': True}
