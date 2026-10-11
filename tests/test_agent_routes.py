"""Independent FastAPI/AgentStore acceptance tests using disposable local PostgreSQL."""
import asyncio
from ast import literal_eval
from copy import deepcopy
from dataclasses import replace
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
from dormdish_agent.models import Nutrition
from tests.test_agent_storage_unit import pg, stores, prepared_action as legacy_prepared_action, confirmation


def prepared_action(store, **kwargs):
    # Keep historical migration fixtures unchanged; current routes require a profile guard.
    def finish(run, history, text, action):
        return store.finish_turn(run, history, text, replace(action,
            expected_profile_version=store.get_profile()['version']))
    adapter = SimpleNamespace(create_conversation=store.create_conversation,
        claim_turn=store.claim_turn, finish_turn=finish)
    return legacy_prepared_action(adapter, **kwargs)


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
    pg.sql('''ALTER TABLE users ADD COLUMN university text DEFAULT 'rutgers',
        ADD COLUMN dietary_restrictions jsonb DEFAULT '[]', ADD COLUMN dietary_preference text;
        CREATE TABLE user_preferences(user_id uuid, category text, item_name text,
        item_type text, meal_type text, reason text);
        CREATE TABLE menus(university text, dining_hall text, date date,
            fetched_at timestamptz, menu_data jsonb);
        CREATE TABLE food_log(id text, user_id uuid, date date, calories numeric,
            protein_g numeric, fat_g numeric, carbs_g numeric);''')
    pg.sql((Path(__file__).resolve().parents[1] / 'migrations/enforce_agent_daily_save_policy.sql').read_text())
    pg.sql((Path(__file__).resolve().parents[1] / 'migrations/enforce_agent_profile_safety.sql').read_text())
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


def lose_committed_rpc_reply(database, monkeypatch, selected):
    original = database.rpc

    def rpc(name, parameters):
        call = original(name, parameters)
        if name != selected:
            return call

        def execute():
            call.execute()
            raise ConnectionError('synthetic-private-storage-response-lost')
        return SimpleNamespace(execute=execute)

    monkeypatch.setattr(database, 'rpc', rpc)


def test_lost_confirmation_reply_reads_committed_receipt_without_repeating_write(api, monkeypatch):
    _, action = prepared_action(api.owner)
    lose_committed_rpc_reply(api.database, monkeypatch, 'agent_confirm_action')
    body = confirmation().model_dump(mode='json')
    result = api.client.post(f'/agent/actions/{action.id}/confirm', json=body)
    assert result.status_code == 200
    assert [name for name, _ in api.database.calls] == ['agent_confirm_action', 'agent_get_run']
    assert result.json()['events'][0]['data']['saved_plan_id']
    assert api.client.get('/agent/runs/' + body['request_id']).json() == result.json()
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '1'


def test_lost_terminal_write_reply_ends_without_terminal_then_get_recovers(api, monkeypatch):
    requests = install_model(monkeypatch)
    conversation = api.owner.create_conversation()
    lose_committed_rpc_reply(api.database, monkeypatch, 'agent_finish_turn')
    body = turn_body()
    result = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=body)
    assert result.status_code == 200
    assert not any(event['type'] in ('complete', 'error', 'action_required') for event in events(result))
    recovered = api.client.get('/agent/runs/' + body['request_id'])
    assert recovered.status_code == 200 and recovered.json()['status'] == 'complete'
    assert recovered.json()['events'][-2]['type'] == 'action_required'
    assert [name for name, _ in api.database.calls].count('agent_finish_turn') == 1
    assert len(requests) == 3
    assert api.pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{api.owner.user_id}'") == '0'


@pytest.mark.parametrize('reason,status', [('running', 409), ('conflict', 409), ('quota', 429)])
def test_admission_errors_are_http_preflight_and_do_not_load_history(api, reason, status):
    from dormdish_agent.models import TurnRequest
    conversation = api.owner.create_conversation()
    body = turn_body()
    if reason in ('running', 'conflict'):
        api.owner.claim_turn(conversation.id, TurnRequest.model_validate(body))
        if reason == 'conflict':
            body['text'] = 'Conflicting request body'
    else:
        api.pg.sql(f"INSERT INTO agent_quotas(user_id, requests) VALUES ('{api.owner.user_id}', 60)")
    result = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=body)
    assert result.status_code == status
    assert not result.headers['content-type'].startswith('text/event-stream')
    assert [name for name, _ in api.database.calls] == ['agent_claim_turn']


def test_unknown_profile_safety_fails_before_sse_and_persists_error(api, monkeypatch):
    requests = install_model(monkeypatch)
    api.pg.sql(f"UPDATE users SET dietary_restrictions = '[\"mystery-allergen\"]' WHERE id = '{api.owner.user_id}'")
    conversation = api.owner.create_conversation()
    body = turn_body()
    result = api.client.post(f'/agent/conversations/{conversation.id}/messages', json=body)
    assert result.status_code == 422
    restored = api.owner.get_run(UUID(body['request_id']))
    assert restored.status == 'error' and restored.events[-1].data.code == 'context_unavailable'
    assert requests == []
    assert api.owner.load_history(conversation.id) == []


def test_chunked_oversize_body_is_bounded_without_content_length(api):
    async def request():
        pieces = iter([b' ' * 20_000, b' ' * 13_000])
        sent = []

        async def receive():
            chunk = next(pieces)
            return {'type': 'http.request', 'body': chunk, 'more_body': len(chunk) == 20_000}

        async def send(message):
            sent.append(message)

        await api.client.app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': '/agent/conversations',
            'query_string': b'', 'headers': [(b'authorization', b'Bearer owner'),
            (b'content-type', b'application/json')], 'server': ('test', 80), 'client': ('test', 1)}, receive, send)
        assert sent[0]['status'] == 413

    asyncio.run(request())
    assert api.database.calls == []


def test_action_and_terminal_are_committed_before_asgi_delivery(api, monkeypatch):
    install_model(monkeypatch)
    conversation = api.owner.create_conversation()
    body = turn_body()

    async def request():
        sent = []

        async def receive():
            return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}

        async def send(message):
            sent.append(message)
            chunk = message.get('body', b'')
            if b'"type":"action_required"' in chunk or b'"type":"complete"' in chunk:
                persisted = api.owner.get_run(UUID(body['request_id']))
                assert persisted.status == 'complete'
                assert any(event.type == 'action_required' for event in persisted.events)
                assert len(api.owner.get_conversation(conversation.id).pending_actions) == 1

        await api.client.app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
            'path': f'/agent/conversations/{conversation.id}/messages', 'query_string': b'',
            'headers': [(b'authorization', b'Bearer owner'), (b'content-type', b'application/json')],
            'server': ('test', 80), 'client': ('test', 1)}, receive, send)
        assert sent[0]['status'] == 200
        assert b'"type":"complete"' in b''.join(message.get('body', b'') for message in sent)

    asyncio.run(request())


def test_real_asgi_disconnect_cancels_sdk_and_persists_reconcilable_outcome(api, monkeypatch):
    conversation = api.owner.create_conversation()
    body = turn_body()

    async def request():
        disconnected, model_closed = asyncio.Event(), asyncio.Event()
        request_read = False

        class ModelBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                try:
                    event = {'type': 'response.output_text.delta', 'delta': 'partial ' * 20,
                        'item_id': 'msg_1', 'output_index': 0, 'content_index': 0, 'sequence_number': 0}
                    yield ('data: ' + json.dumps(event) + '\n\n').encode()
                    await asyncio.Event().wait()
                finally:
                    model_closed.set()

        monkeypatch.setattr(routes, 'AsyncOpenAI', lambda **kwargs: AsyncOpenAI(api_key='synthetic',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
                200, headers={'content-type': 'text/event-stream'}, stream=ModelBody()))), **kwargs))

        async def receive():
            nonlocal request_read
            if not request_read:
                request_read = True
                return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}
            await disconnected.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            if b'"type":"text_delta"' in message.get('body', b''):
                disconnected.set()

        await asyncio.wait_for(api.client.app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
            'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
            'path': f'/agent/conversations/{conversation.id}/messages', 'query_string': b'',
            'headers': [(b'authorization', b'Bearer owner'), (b'content-type', b'application/json')],
            'server': ('test', 80), 'client': ('test', 1)}, receive, send), 5)
        assert disconnected.is_set()
        await asyncio.wait_for(model_closed.wait(), 1)

    asyncio.run(request())
    recovered = api.client.get('/agent/runs/' + body['request_id']).json()
    assert recovered['status'] == 'error' and recovered['events'][-1]['data']['code'] == 'cancelled'
    assert api.owner.load_history(conversation.id) == []
    assert [name for name, _ in api.database.calls].count('agent_finish_turn') == 1
