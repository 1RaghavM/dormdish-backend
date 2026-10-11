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
