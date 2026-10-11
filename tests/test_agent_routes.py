"""Independent FastAPI/AgentStore acceptance tests using disposable local PostgreSQL."""
import asyncio
from ast import literal_eval
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from dormdish_agent import routes
from dormdish_agent.conversations import AgentStore
from dormdish_agent.models import Nutrition
from tests.test_agent_storage_unit import pg, stores, prepared_action, confirmation


class RouteDatabase:
    """Minimal PostgREST transport: all selected rows and RPC writes execute real SQL."""
    def __init__(self, pg):
        self.pg, self.calls = pg, []

    def rpc(self, name, params):
        self.calls.append((name, deepcopy(params)))
        return self.pg.rpc(name, params)

    def table(self, table):
        assert table in {'user_preferences', 'saved_meal_plans', 'menus', 'food_log'}
        pg, filters = self.pg, []

        class Query:
            columns, ordering, count = '*', '', 1000

            def select(self, columns):
                assert all(part.replace('_', '').isalnum() for part in columns.split(',')) or columns == '*'
                self.columns = columns
                return self

            def eq(self, key, value):
                assert key in {'user_id', 'date', 'university', 'dining_hall', 'id'}
                filters.append(f"{key} = '{str(value).replace(chr(39), chr(39) * 2)}'")
                return self

            def order(self, key, desc=False):
                assert key == 'fetched_at'
                self.ordering = ' ORDER BY fetched_at' + (' DESC' if desc else '')
                return self

            def limit(self, count):
                self.count = int(count)
                return self

            def execute(self):
                where = ' WHERE ' + ' AND '.join(filters) if filters else ''
                result = pg.sql(f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]') FROM (SELECT {self.columns} "
                                f"FROM {table}{where}{self.ordering} LIMIT {self.count}) t")
                return SimpleNamespace(data=json.loads(result))

        return Query()


@pytest.fixture(scope='module')
def route_pg(pg):
    pg.sql('''CREATE TABLE user_preferences(user_id uuid, category text, item_name text,
        item_type text, meal_type text, reason text);
        CREATE TABLE menus(university text, dining_hall text, date date,
            fetched_at timestamptz, menu_data jsonb);
        CREATE TABLE food_log(id text, user_id uuid, date date, calories numeric,
            protein_g numeric, fat_g numeric, carbs_g numeric);''')
    pg.sql((Path(__file__).resolve().parents[1] / 'migrations/enforce_agent_daily_save_policy.sql').read_text())
    menu = {'items': [{'name': 'Rice 🌶', 'meal': 'dinner', 'serving_size': '1 cup',
        'calories': 200, 'protein_g': 4, 'fat_g': 1, 'carbs_g': 44, 'allergens': [], 'dietary_tags': ['vegan']}]}
    pg.sql("INSERT INTO menus VALUES ('Rutgers University', 'busch-dining-hall', '2026-10-10', now(), "
           + "'" + json.dumps(menu).replace("'", "''") + "')")
    return pg


@pytest.fixture
def api(route_pg, stores, monkeypatch):
    import app as application
    owner, other = stores
    database = RouteDatabase(route_pg)
    profiles = {token: {'id': store.user_id, 'university': 'rutgers', 'dietary_restrictions': []}
                for token, store in [('owner', owner), ('other', other)]}
    monkeypatch.setattr(application, 'verify_session_token', profiles.get)
    for key, value in {'DORMDISH_AGENT_ENABLED': 'true', 'DORMDISH_AGENT_KILL_SWITCH': 'false',
                       'DORMDISH_AGENT_ROLLOUT_PERCENT': '100'}.items():
        monkeypatch.setenv(key, value)
    app = FastAPI()
    app.include_router(routes.create_router(application.get_current_user, application.require_premium,
        lambda: database, lambda _: Nutrition(calories=2000, protein_g=100, fat_g=70, carbs_g=200)))
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer owner'
        yield SimpleNamespace(client=client, owner=owner, other=other, database=database, pg=route_pg, profiles=profiles)


def turn_body(**changes):
    return {'request_id': str(uuid4()), 'text': 'Synthetic dinner', 'context': {
        'dining_hall': 'busch', 'date': '2026-10-10', 'meal_type': 'dinner'}, **changes}


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


def install_model(monkeypatch):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        number = len(requests)
        output, deltas = [], []
        if number <= 2:
            arguments = {}
            if number == 2:
                result = next(item['output'] for item in payload['input'] if item.get('type') == 'function_call_output')
                item_id = literal_eval(result)['items'][0]['menu_item_id']
                arguments = {'selections': [{'menu_item_id': item_id, 'servings': 1.5}]}
            output = [{'type': 'function_call', 'name': 'retrieve_menu' if number == 1 else 'preview_meal',
                'call_id': f'call_{number}', 'id': f'fc_{number}', 'arguments': json.dumps(arguments), 'status': 'completed'}]
        else:
            deltas = [{'type': 'response.output_text.delta', 'delta': 'Dinner 🌶', 'item_id': 'msg_1',
                       'content_index': 0, 'output_index': 0, 'sequence_number': 0}]
            output = [{'type': 'message', 'role': 'assistant', 'id': 'msg_1', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': 'Dinner 🌶', 'annotations': []}]}]
        deltas.append({'type': 'response.completed', 'sequence_number': 1, 'response': {
            'id': f'resp_{number}', 'object': 'response', 'created_at': 0, 'model': 'gpt-6-luna',
            'status': 'completed', 'output': output, 'usage': {'input_tokens': 20, 'output_tokens': 10,
            'total_tokens': 30, 'input_tokens_details': {'cached_tokens': 0},
            'output_tokens_details': {'reasoning_tokens': 0}}}})
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            text=''.join('data: ' + json.dumps(event) + '\n\n' for event in deltas))

    monkeypatch.setattr(routes, 'AsyncOpenAI', lambda **kwargs: AsyncOpenAI(api_key='synthetic',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), **kwargs))
    return requests


@pytest.mark.parametrize('header', [None, 'Bearer expired', 'Basic owner'])
def test_real_auth_dependency_rejects_before_storage(api, header):
    api.client.headers.pop('Authorization')
    if header:
        api.client.headers['Authorization'] = header
    assert api.client.post('/agent/conversations', json={}).status_code == 401
    assert api.database.calls == []


@pytest.mark.parametrize('target', ['conversation', 'run', 'delete', 'message', 'action'])
def test_cross_owner_resources_fail_before_stream_or_write(api, target):
    conversation, action = prepared_action(api.other)
    run_id = api.pg.sql(f"SELECT id FROM agent_runs WHERE conversation_id = '{conversation.id}'")
    paths = {'conversation': ('get', f'/agent/conversations/{conversation.id}', None),
        'run': ('get', f'/agent/runs/{run_id}', None),
        'delete': ('delete', f'/agent/conversations/{conversation.id}', None),
        'message': ('post', f'/agent/conversations/{conversation.id}/messages', turn_body()),
        'action': ('post', f'/agent/actions/{action.id}/confirm', confirmation().model_dump(mode='json'))}
    method, path, body = paths[target]
    response = api.client.request(method, path, **({'json': body} if body else {}))
    assert response.status_code == 404
    assert 'text/event-stream' not in response.headers.get('content-type', '')
    assert all(params['p_user_id'] == api.owner.user_id for _, params in api.database.calls)
    assert api.other.get_conversation(conversation.id).pending_actions[0].id == action.id
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.other.user_id}'") == '0'


@pytest.mark.parametrize('body', [turn_body(user_id=str(uuid4())), turn_body(text='x' * 4001),
    turn_body(request_id='bad-id'), turn_body(text=' '), turn_body(context={'dining_hall': 'busch',
        'date': 'tomorrow', 'meal_type': 'dinner'}), {'request_id': str(uuid4()), 'text': 123}])
def test_invalid_input_never_claims_a_turn_or_echoes_private_body(api, body):
    response = api.client.post(f'/agent/conversations/{uuid4()}/messages', json=body)
    assert response.status_code == 422
    assert response.json() == {'detail': 'Invalid agent request'}
    assert api.database.calls == []


def test_recoverable_turn_real_sdk_apply_and_duplicate_replay(api, monkeypatch):
    requests = install_model(monkeypatch)
    created = api.client.post('/agent/conversations', json={})
    assert created.status_code == 201
    conversation = created.json()['id']
    body = turn_body()
    response = api.client.post(f'/agent/conversations/{conversation}/messages', json=body)
    assert response.status_code == 200
    streamed = events(response)
    assert [event['type'] for event in streamed] == ['status', 'status', 'meal_preview', 'text_delta', 'action_required', 'complete']
    names = [name for name, _ in api.database.calls]
    assert names.index('agent_claim_turn') < names.index('agent_get_conversation') < names.index('agent_finish_turn')
    assert all(event['run_id'] == body['request_id'] for event in streamed)
    assert [event['sequence'] for event in streamed] == list(range(1, len(streamed) + 1))
    restored = api.client.get(f'/agent/conversations/{conversation}').json()
    assert len(restored['messages']) == 2 and restored['active_run_id'] is None
    action = restored['pending_actions'][0]
    assert action == streamed[-2]['data']['action']
    assert api.client.get('/agent/runs/' + body['request_id']).json()['events'] == streamed
    replay = api.client.post(f'/agent/conversations/{conversation}/messages', json=body)
    assert replay.content == response.content and len(requests) == 3
    confirmation_body = {'request_id': str(uuid4()), 'proposal_version': action['proposal']['version']}
    applied = api.client.post(f"/agent/actions/{action['id']}/confirm", json=confirmation_body)
    assert applied.status_code == 200
    assert api.client.post(f"/agent/actions/{action['id']}/confirm", json=confirmation_body).json() == applied.json()
    plan_id = applied.json()['events'][0]['data']['saved_plan_id']
    plan = api.owner.get_saved_plan(UUID(plan_id))['plan']
    assert plan['date'] == '2026-10-10' and plan['dining_hall'] == 'busch-dining-hall'
    assert plan['meal_plan_data']['meal_plan']['totals'] == dict(calories=300, protein_g=6, fat_g=1.5, carbs_g=66)
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '1'
