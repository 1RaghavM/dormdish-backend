"""Legacy generation routes: flag-off characterization and flag-on shared-agent adapter contract.

Real app routes, real SDK runtime/tools and real disposable PostgreSQL (DD_AGENT_LOCAL_PG=1).
Only session auth, referral side effects, the Gemini generator and the model HTTP transport are synthetic.
"""
from ast import literal_eval
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_agent_routes import route_pg, pg, stores, events  # noqa: F401 (fixtures)

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo('America/New_York')
# A requested date that is never "today", so autosave-date bugs are observable.
DAY = (datetime.now(ET).date() + timedelta(days=3)).isoformat()
ROUTES = ['/generate-meal-plan', '/generate-meal', '/generate-meal-plan-stream', '/generate-meal-stream']
STREAMS = {'/generate-meal-plan-stream', '/generate-meal-stream'}
PLAN_KEYS = {'success', 'user_profile', 'nutrition_targets', 'bmr_tdee', 'available_items', 'meal_plan',
             'preferences_snapshot', 'generation_id', 'ask_feedback', 'processing_info', 'meta',
             'saved_plan_id', 'auto_saved'}
MEAL_KEYS = {'success', 'meal', 'meal_plan', 'nutrition_targets', 'daily_targets', 'already_planned',
             'available_items', 'preferences_snapshot', 'generation_id', 'processing_info', 'meta',
             'saved_plan_id', 'auto_saved'}
PLAN_STREAM_KEYS = {'type', 'meal_plan', 'raw_response', 'preferences_snapshot', 'saved_plan_id', 'auto_saved'}
MEAL_STREAM_KEYS = PLAN_STREAM_KEYS | {'meal', 'nutrition_targets', 'daily_targets', 'already_planned'}
ITEM_KEYS = {'name', 'servings', 'calories', 'protein_g', 'fat_g', 'carbs_g', 'serving_size'}
MACROS = ('calories', 'protein_g', 'fat_g', 'carbs_g')


def item(name, meal, cal, pro, fat, carb, allergens=(), tags=()):
    return {'name': name, 'meal': meal, 'serving_size': '1 cup', 'calories': cal, 'protein_g': pro,
            'fat_g': fat, 'carbs_g': carb, 'allergens': None if allergens is None else list(allergens),
            'dietary_tags': list(tags)}


MENU = [item('Oatmeal', 'breakfast', 150, 5, 3, 27, tags=['vegan']),
        item('Peanut Pancakes', 'breakfast', 400, 9, 14, 60, allergens=['peanut', 'wheat']),
        item('Chicken Bowl', 'lunch', 500, 40, 12, 50),
        item('Peanut Noodles', 'dinner', 600, 18, 22, 80, allergens=['peanut', 'wheat']),
        item('Mystery Stew', 'dinner', 450, 25, 15, 40, allergens=None),
        item('Rice 🌶', 'dinner', 200, 4, 1, 44, tags=['vegan'])]
BY_NAME = {entry['name']: entry for entry in MENU}
SAFE = ['Oatmeal', 'Chicken Bowl', 'Rice 🌶']
SERVINGS = 1.5
PROFILE = {'university': 'rutgers', 'dietary_restrictions': [], 'dietary_preference': None, 'age': 20,
           'height_feet': 5, 'height_inches': 10, 'weight_lbs': 170, 'gender': 'male',
           'activity_level': 'moderately_active', 'goal': 'maintain', 'goal_weight_lbs': None,
           'weight_change_speed': None}


def trusted(*names):
    return {key: sum(BY_NAME[name][key] * SERVINGS for name in names) for key in MACROS}


class LegacyDB:
    """PostgREST-shaped client; every table read/write and RPC executes real SQL."""
    def __init__(self, pg):
        self.pg, self.calls, self.lose_plan_write = pg, [], False

    def plan_count(self):
        return int(self.pg.sql('SELECT count(*) FROM saved_meal_plans'))

    def _lossy(self, run):
        before = self.plan_count() if self.lose_plan_write else None
        result = run()
        if self.lose_plan_write and self.plan_count() > before:
            self.lose_plan_write = False
            raise ConnectionError('synthetic-storage-response-lost')
        return result

    def rpc(self, name, params):
        self.calls.append(('rpc', name))
        call = self.pg.rpc(name, params)
        return SimpleNamespace(execute=lambda: self._lossy(call.execute))

    def table(self, table):
        assert re.fullmatch(r'[a-z_]+', table)
        return Query(self, table)


def literal(value):
    if value is None:
        return 'NULL'
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    return "'" + str(value).replace("'", "''") + "'"


class Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters = db, table, []
        self.kind, self.columns, self.ordering, self.count, self.values = 'select', '*', '', 1000, None

    def select(self, columns='*', **_):
        parts = [part.strip() for part in columns.split(',')]
        assert columns == '*' or all(re.fullmatch(r'[a-z_]+', part) for part in parts), columns
        self.columns = columns if columns == '*' else ', '.join(parts)
        return self

    def _filter(self, op, key, value):
        assert re.fullmatch(r'[a-z_]+', key)
        self.filters.append(f'{self.table}.{key}::text {op} {literal(value)}')
        return self

    def eq(self, key, value):
        return self._filter('=', key, value)

    def gt(self, key, value):
        return self._filter('>', key, value)

    def order(self, key, desc=False):
        assert re.fullmatch(r'[a-z_]+', key)
        self.ordering = f' ORDER BY {key}' + (' DESC' if desc else '')
        return self

    def limit(self, count):
        self.count = int(count)
        return self

    def insert(self, values):
        self.kind, self.values = 'insert', values if isinstance(values, list) else [values]
        return self

    def update(self, values):
        self.kind, self.values = 'update', values
        return self

    def delete(self):
        self.kind = 'delete'
        return self

    def execute(self):
        self.db.calls.append((self.kind, self.table))
        where = ' WHERE ' + ' AND '.join(self.filters) if self.filters else ''
        t = self.table
        if self.kind == 'select':
            sql = f'SELECT {self.columns} FROM {t}{where}{self.ordering} LIMIT {self.count}'
        elif self.kind == 'insert':
            sql = (f'INSERT INTO {t} SELECT * FROM jsonb_populate_recordset(NULL::{t}, {literal(self.values)}) '
                   'RETURNING *')
        elif self.kind == 'update':
            assert all(re.fullmatch(r'[a-z_]+', key) for key in self.values)
            sets = ', '.join(f'{key} = p.{key}' for key in self.values)
            sql = (f'UPDATE {t} SET {sets} FROM jsonb_populate_record(NULL::{t}, {literal(self.values)}) p'
                   f'{where} RETURNING {t}.*')
        else:
            sql = f'DELETE FROM {t}{where} RETURNING *'
        run = lambda: SimpleNamespace(data=json.loads(self.db.pg.sql(
            f"WITH r AS ({sql}) SELECT coalesce(jsonb_agg(to_jsonb(r)), '[]') FROM r")))
        return self.db._lossy(run) if self.kind != 'select' else run()


@pytest.fixture(scope='module')
def legacy_pg(route_pg):
    route_pg.sql((ROOT / 'migrations/add_agent_controlled_save.sql').read_text())
    route_pg.sql('''ALTER TABLE users ADD COLUMN IF NOT EXISTS age integer, ADD COLUMN IF NOT EXISTS height_feet integer,
        ADD COLUMN IF NOT EXISTS height_inches integer, ADD COLUMN IF NOT EXISTS weight_lbs numeric,
        ADD COLUMN IF NOT EXISTS gender text, ADD COLUMN IF NOT EXISTS activity_level text,
        ADD COLUMN IF NOT EXISTS goal text, ADD COLUMN IF NOT EXISTS goal_weight_lbs numeric,
        ADD COLUMN IF NOT EXISTS weight_change_speed text, ADD COLUMN IF NOT EXISTS custom_calories integer,
        ADD COLUMN IF NOT EXISTS custom_protein_g integer, ADD COLUMN IF NOT EXISTS custom_fat_g integer,
        ADD COLUMN IF NOT EXISTS custom_carbs_g integer, ADD COLUMN IF NOT EXISTS bmr numeric,
        ADD COLUMN IF NOT EXISTS tdee numeric;
        ALTER TABLE menus ADD COLUMN IF NOT EXISTS id text;
        ALTER TABLE user_preferences ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();''')
    menu = json.dumps({'items': MENU}).replace("'", "''")
    route_pg.sql(f"INSERT INTO menus(id, university, dining_hall, date, fetched_at, menu_data) VALUES "
                 f"('rutgers_busch-dining-hall_{DAY}', 'Rutgers University', 'busch-dining-hall', '{DAY}', now(), '{menu}')")
    return route_pg


def gemini_plan(meals=('breakfast', 'lunch', 'dinner')):
    names = dict(zip(('breakfast', 'lunch', 'dinner'), SAFE))
    return {'totals': {'calories': 1111.0, 'protein_g': 77.0, 'fat_g': 33.0, 'carbs_g': 222.0},
            'meals': {meal: {'description': f'Gemini {meal}', 'items': [{
                'name': names[meal], 'servings': 1.0, 'serving_size': '1 cup',
                **{key: float(BY_NAME[names[meal]][key]) for key in MACROS}}]} for meal in meals},
            'explanation': 'Gemini plan'}


@pytest.fixture
def legacy(legacy_pg, stores, monkeypatch):
    import app as application
    import meal_plan_agent
    import menu_cache
    owner, other = stores
    db = LegacyDB(legacy_pg)
    profiles = {}
    for token, store in [('owner', owner), ('other', other)]:
        profiles[token] = {'id': store.user_id, **PROFILE}
        sets = ', '.join(f'{key} = {literal(value)}' for key, value in PROFILE.items())
        legacy_pg.sql(f"UPDATE users SET {sets} WHERE id = '{store.user_id}'")
    gemini, referrals = [], []

    async def fake_agent(**kwargs):
        gemini.append(('agent', deepcopy(kwargs)))
        if gemini_failure:
            raise ValueError('synthetic Gemini failure')
        meals = [m for m in ('breakfast', 'lunch', 'dinner') if m not in (kwargs.get('exclude_meals') or [])]
        plan = gemini_plan(meals)
        return SimpleNamespace(model_dump=lambda: deepcopy(plan)), '{"raw": "gemini"}'

    async def fake_stream(**kwargs):
        gemini.append(('stream', deepcopy(kwargs)))
        yield {'type': 'thinking', 'content': 'Analyzing menu items and nutritional requirements...'}
        if gemini_failure:
            raise RuntimeError('synthetic Gemini failure')
        meals = [m for m in ('breakfast', 'lunch', 'dinner') if m not in (kwargs.get('exclude_meals') or [])]
        yield {'type': 'complete', 'meal_plan': gemini_plan(meals), 'raw_response': '{"raw": "gemini"}'}

    async def referral(user_id):
        referrals.append(user_id)
        return False

    async def no_cache(**_):
        return None

    gemini_failure = False
    monkeypatch.setattr(application, 'supabase', db)
    monkeypatch.setattr(application, 'verify_session_token', lambda token: deepcopy(profiles.get(token)))
    monkeypatch.setattr(application, 'check_and_fulfill_referral', referral)
    monkeypatch.setattr(meal_plan_agent, 'generate_meal_plan_with_agent', fake_agent)
    monkeypatch.setattr(meal_plan_agent, 'generate_meal_plan_with_streaming', fake_stream)
    monkeypatch.setattr(menu_cache, 'get_cache_name', no_cache)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic')
    for key in ('DORMDISH_AGENT_GENERATION_ENABLED', 'DORMDISH_CONTROLLED_SAVE_ENABLED'):
        monkeypatch.delenv(key, raising=False)
    model = install_model(monkeypatch)
    with TestClient(application.app) as client:
        client.headers['Authorization'] = 'Bearer owner'
        state = SimpleNamespace(client=client, owner=owner, other=other, db=db, pg=legacy_pg, gemini=gemini,
                                referrals=referrals, model=model, profiles=profiles)

        def fail_gemini():
            nonlocal gemini_failure
            gemini_failure = True
        state.fail_gemini = fail_gemini
        yield state


@pytest.fixture
def agent_on(legacy, monkeypatch):
    for key, value in {'DORMDISH_AGENT_GENERATION_ENABLED': 'true', 'DORMDISH_AGENT_ENABLED': 'true',
                       'DORMDISH_AGENT_KILL_SWITCH': 'false', 'DORMDISH_AGENT_ROLLOUT_PERCENT': '100'}.items():
        monkeypatch.setenv(key, value)
    return legacy


def install_model(monkeypatch):
    """Scripted Responses API at the HTTP transport: retrieve_menu, read_intake, then preview_meal (preferred
    names first, one repair), then a final message whose text falsely claims nutrition. Never reaches the network."""
    model = SimpleNamespace(requests=[], prefer=SAFE, mode='ok')

    def output_for(payload):
        items = payload.get('input')
        items = items if isinstance(items, list) else []
        names = {entry.get('call_id'): entry.get('name') for entry in items if entry.get('type') == 'function_call'}
        outputs = [(names.get(entry.get('call_id')), entry.get('output')) for entry in items
                   if entry.get('type') == 'function_call_output']
        menu = next((out for name, out in outputs if name == 'retrieve_menu'), None)
        previews = [out for name, out in outputs if name == 'preview_meal']
        number = len(model.requests)
        for tool in ('retrieve_menu', 'read_intake'):
            if model.mode == 'loop' or tool not in [name for name, _ in outputs]:
                return [{'type': 'function_call', 'name': tool, 'call_id': f'call_{number}',
                         'id': f'fc_{number}', 'arguments': '{}', 'status': 'completed'}]
        if not previews or ('could not verify' in str(previews[-1]) and len(previews) < 2):
            menu_items = literal_eval(menu)['items'] if isinstance(menu, str) else menu['items']
            ordered = [i for name in model.prefer for i in menu_items if i['name'] == name] + menu_items
            choice = ordered[len(previews)]
            return [{'type': 'function_call', 'name': 'preview_meal', 'call_id': f'call_{number}',
                     'id': f'fc_{number}', 'status': 'completed', 'arguments': json.dumps(
                         {'selections': [{'menu_item_id': choice['menu_item_id'], 'servings': SERVINGS}]})}]
        return [{'type': 'message', 'role': 'assistant', 'id': f'msg_{number}', 'status': 'completed',
                 'content': [{'type': 'output_text', 'annotations': [],
                              'text': 'Enjoy 🌶! This has 9999 calories and 999g protein.'}]}]

    async def handle(self, request):
        if request.url.host != 'api.openai.com':
            raise RuntimeError('Network disabled in legacy adapter tests: ' + request.url.host)
        payload = json.loads(await request.aread())
        model.requests.append(payload)
        if model.mode == 'fail':
            return httpx.Response(500, json={'error': {'message': 'synthetic model failure'}})
        output = output_for(payload)
        number = len(model.requests)
        response = {'id': f'resp_{number}', 'object': 'response', 'created_at': 0, 'model': payload.get('model'),
                    'status': 'completed', 'output': output, 'parallel_tool_calls': False, 'tool_choice': 'auto',
                    'tools': [], 'usage': {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30,
                                           'input_tokens_details': {'cached_tokens': 0},
                                           'output_tokens_details': {'reasoning_tokens': 0}}}
        if not payload.get('stream'):
            return httpx.Response(200, json=response)
        sse = [{'type': 'response.output_text.delta', 'delta': part['text'], 'item_id': entry['id'],
                'content_index': 0, 'output_index': 0, 'sequence_number': 0}
               for entry in output if entry['type'] == 'message' for part in entry['content']]
        sse.append({'type': 'response.completed', 'sequence_number': 1, 'response': response})
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              text=''.join('data: ' + json.dumps(event) + '\n\n' for event in sse))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', handle)
    return model


def post(legacy, route, **changes):
    body = {'dining_hall': 'busch', 'date': DAY, **({'meal': 'dinner'} if 'meal' in route[1:] and 'plan' not in route else {}),
            **changes}
    return legacy.client.post(route, json=body)


def plans(legacy, store=None):
    owner = (store or legacy.owner).user_id
    return json.loads(legacy.pg.sql(f"SELECT coalesce(jsonb_agg(to_jsonb(s) ORDER BY created_at), '[]') "
                                    f"FROM saved_meal_plans s WHERE user_id = '{owner}'"))


def seed(legacy, payload, date=DAY):
    encoded = json.dumps(payload).replace("'", "''")
    legacy.pg.sql(f"INSERT INTO saved_meal_plans VALUES (gen_random_uuid(), '{legacy.owner.user_id}', 'busch', "
                  f"'{date}', '{encoded}', now(), now())")


def today():
    return datetime.now(ET).date().isoformat()


def complete(response):
    streamed = events(response)
    assert response.status_code == 200 and streamed, response.text
    assert streamed[-1]['type'] == 'complete', streamed
    return streamed, streamed[-1]


def result(legacy, route, **changes):
    """The route's success payload in its legacy shape: JSON body or the SSE complete chunk."""
    response = post(legacy, route, **changes)
    if route in STREAMS:
        return complete(response)[1]
    assert response.status_code == 200, response.text
    return response.json()


def assert_error_shape(response, route):
    """Existing failure shapes: the app's HTTP error envelope or the in-stream terminal error chunk."""
    if route in STREAMS:
        assert response.status_code == 200 and events(response)[-1] == {'type': 'error', 'error': 'Please try again.'}
    else:
        assert response.json() == http_error(500, 'Please try again.')


def http_error(status, message):
    return {'error': 'HTTP error', 'message': message, 'status_code': status}


def close(actual, expected):
    return all(abs(float(actual[key]) - float(expected[key])) < 1e-6 for key in MACROS)


# ---------------- Flag off: characterization of the current Gemini path ----------------

@pytest.mark.parametrize('flag', [None, 'false'])
def test_flag_off_full_plan_json_shape_and_today_autosave(legacy, monkeypatch, flag):
    if flag:
        monkeypatch.setenv('DORMDISH_AGENT_GENERATION_ENABLED', flag)
    body = result(legacy, '/generate-meal-plan')
    assert set(body) == PLAN_KEYS
    assert body['success'] is True and body['auto_saved'] is True
    assert body['ask_feedback'] == 'Please rate this meal plan' and UUID(body['generation_id'])
    assert body['meal_plan']['meals'] == gemini_plan()['meals']
    assert body['meal_plan']['totals'] == gemini_plan()['totals']  # legacy trusts model totals
    assert set(body['nutrition_targets']) == set(MACROS) and 'agent_raw_response' in body['meta']
    [row] = plans(legacy)
    # Legacy rollback path saves under "today" even for another requested date.
    assert row['id'] == body['saved_plan_id'] and row['date'] == today() and row['dining_hall'] == 'busch'
    assert row['meal_plan_data']['generation_id'] == body['generation_id']
    assert [kind for kind, _ in legacy.gemini] == ['agent'] and legacy.model.requests == []
    assert legacy.gemini[0][1]['menu_payload']['requested_date'] == DAY
    assert legacy.referrals == [legacy.owner.user_id]


def test_flag_off_single_meal_json_shape_limit_and_today_autosave(legacy):
    body = result(legacy, '/generate-meal')
    assert set(body) == MEAL_KEYS and body['meal'] == 'dinner' and body['already_planned'] == {}
    assert body['meal_plan']['meals'] == gemini_plan(['dinner'])['meals']
    assert legacy.gemini[0][1]['exclude_meals'] == ['breakfast', 'lunch']
    [row] = plans(legacy)
    assert row['id'] == body['saved_plan_id'] and row['date'] == today()
    assert row['meal_plan_data']['meal'] == 'dinner'
    seed(legacy, {'meal': 'dinner', 'meal_plan': {'totals': {}}})
    limited = post(legacy, '/generate-meal')
    assert limited.status_code == 429
    assert limited.json() == http_error(429, f'You already have a generated dinner for {DAY}. '
                                             'Wait until tomorrow or delete the existing entry.')


def test_flag_off_full_plan_daily_limit_uses_today(legacy):
    seed(legacy, {'meal_plan': {}}, date=today())
    response = post(legacy, '/generate-meal-plan')
    assert response.status_code == 409
    assert response.json() == http_error(409, 'You already have a meal plan for today. You can generate a new one '
                                         'at the start of tomorrow or use the existing plan.')
    assert legacy.gemini == []


def test_flag_off_plan_stream_sse_shape(legacy):
    response = post(legacy, '/generate-meal-plan-stream')
    assert response.headers['content-type'].startswith('text/event-stream')
    assert all(line.startswith('data: ') for line in response.text.split('\n\n') if line)
    streamed, done = complete(response)
    assert [event['type'] for event in streamed] == ['thinking', 'complete']
    assert set(done) == PLAN_STREAM_KEYS and done['auto_saved'] is True
    [row] = plans(legacy)
    assert row['id'] == done['saved_plan_id'] and row['date'] == today()
    assert set(row['meal_plan_data']) == {'success', 'meal_plan', 'nutrition_targets', 'dining_hall', 'date'}


def test_flag_off_meal_stream_sse_shape(legacy):
    streamed, done = complete(post(legacy, '/generate-meal-stream'))
    assert streamed[0] == {'type': 'thinking', 'content': 'Analyzing menu items for dinner...'}
    assert streamed[1] == {'type': 'thinking', 'content': 'Processing dinner menu items...'}
    assert set(done) == MEAL_STREAM_KEYS and done['meal'] == 'dinner'
    [row] = plans(legacy)
    assert row['id'] == done['saved_plan_id'] and row['date'] == today()
    assert row['meal_plan_data']['meal'] == 'dinner'


@pytest.mark.parametrize('route', sorted(STREAMS))
def test_flag_off_stream_generator_failure_is_terminal_error_chunk_without_save(legacy, route):
    legacy.fail_gemini()
    response = post(legacy, route)
    assert response.status_code == 200
    assert events(response)[-1] == {'type': 'error', 'error': 'Please try again.'}
    assert plans(legacy) == []


@pytest.mark.parametrize('flag', [None, 'true'])
@pytest.mark.parametrize('route', ROUTES)
def test_auth_and_unknown_hall_fail_before_generation_or_write(legacy, monkeypatch, route, flag):
    if flag:
        for key in ('DORMDISH_AGENT_GENERATION_ENABLED', 'DORMDISH_AGENT_ENABLED'):
            monkeypatch.setenv(key, flag)
        monkeypatch.setenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '100')
    legacy.client.headers.pop('Authorization')
    unauthenticated = post(legacy, route)
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == http_error(401, 'Authentication required.' if route in STREAMS
                                                else 'Authentication required. Guest users not supported.')
    legacy.client.headers['Authorization'] = 'Bearer owner'
    unknown = post(legacy, route, dining_hall='nowhere')
    assert unknown.json() == http_error(400, 'Unknown dining hall: nowhere')
    assert legacy.gemini == [] and legacy.model.requests == [] and plans(legacy) == []


# ---------------- Flag on: shared agent generation behind the legacy contracts ----------------

def assert_items_are_trusted(meal, *names):
    assert [entry['name'] for entry in meal['items']] == list(names)
    for entry in meal['items']:
        assert ITEM_KEYS <= set(entry)
        assert entry['servings'] == SERVINGS
        assert close(entry, trusted(entry['name']))


@pytest.mark.parametrize('route', ['/generate-meal', '/generate-meal-stream'])
def test_flag_on_single_meal_uses_agent_and_keeps_legacy_shape(agent_on, route):
    body = result(agent_on, route)
    assert set(body) == (MEAL_KEYS if route not in STREAMS else MEAL_STREAM_KEYS)
    assert agent_on.gemini == [] and agent_on.model.requests
    assert body['meal'] == 'dinner' and body['auto_saved'] is True
    meals = body['meal_plan']['meals']
    assert set(meals) == {'dinner'}
    assert_items_are_trusted(meals['dinner'], 'Rice 🌶')
    assert close(body['meal_plan']['totals'], trusted('Rice 🌶'))  # never the model text's 9999
    [row] = plans(agent_on)
    assert row['id'] == body['saved_plan_id'] and row['user_id'] == agent_on.owner.user_id
    assert row['date'] == DAY and row['meal_plan_data']['meal'] == 'dinner'
    assert close(row['meal_plan_data']['meal_plan']['totals'], trusted('Rice 🌶'))
    if route in STREAMS:
        assert row['meal_plan_data']['date'] == DAY
    else:
        assert UUID(body['generation_id']) and row['meal_plan_data']['generation_id'] == body['generation_id']


@pytest.mark.parametrize('route', ['/generate-meal-plan', '/generate-meal-plan-stream'])
def test_flag_on_full_plan_uses_agent_and_saves_requested_date(agent_on, route):
    body = result(agent_on, route)
    assert set(body) == (PLAN_KEYS if route not in STREAMS else PLAN_STREAM_KEYS)
    assert agent_on.gemini == [] and agent_on.model.requests and body['auto_saved'] is True
    meals = body['meal_plan']['meals']
    assert set(meals) == {'breakfast', 'lunch', 'dinner'}
    for meal, name in zip(('breakfast', 'lunch', 'dinner'), SAFE):
        assert_items_are_trusted(meals[meal], name)
    assert close(body['meal_plan']['totals'], trusted(*SAFE))
    assert body['meal_plan']['explanation']
    [row] = plans(agent_on)
    assert row['id'] == body['saved_plan_id'] and row['date'] == DAY and 'meal' not in row['meal_plan_data']
    assert close(row['meal_plan_data']['meal_plan']['totals'], trusted(*SAFE))
    if route in STREAMS:
        assert row['meal_plan_data']['date'] == DAY
    else:
        assert body['ask_feedback'] == 'Please rate this meal plan' and UUID(body['generation_id'])
        assert row['meal_plan_data']['generation_id'] == body['generation_id']
    assert agent_on.referrals == [agent_on.owner.user_id]


def test_flag_on_full_plan_honors_exclude_meals(agent_on):
    body = result(agent_on, '/generate-meal-plan', exclude_meals=['breakfast'])
    assert set(body['meal_plan']['meals']) == {'lunch', 'dinner'}
    assert close(body['meal_plan']['totals'], trusted('Chicken Bowl', 'Rice 🌶'))


@pytest.mark.parametrize('route', ['/generate-meal-plan-stream', '/generate-meal-stream'])
def test_flag_on_streams_keep_legacy_sse_framing(agent_on, route):
    response = post(agent_on, route)
    assert response.headers['content-type'].startswith('text/event-stream')
    streamed, _ = complete(response)
    assert {event['type'] for event in streamed} <= {'thinking', 'complete'}
    for event in streamed[:-1]:
        assert set(event) == {'type', 'content'} and isinstance(event['content'], str)
    assert not any(key in event for event in streamed for key in ('run_id', 'sequence', 'data'))


@pytest.mark.parametrize('route', ['/generate-meal', '/generate-meal-stream'])
def test_flag_on_single_meal_targets_match_legacy_for_requested_date(agent_on, monkeypatch, route):
    seed(agent_on, {'meal': 'breakfast', 'meal_plan': {'totals': {'calories': 500, 'protein_g': 30,
                                                                  'fat_g': 10, 'carbs_g': 60}}})
    agent_on.pg.sql(f"INSERT INTO food_log VALUES ('log-1', '{agent_on.owner.user_id}', '{DAY}', 400, 30, 10, 40)")
    monkeypatch.setenv('DORMDISH_AGENT_GENERATION_ENABLED', 'false')
    legacy_body = result(agent_on, route)
    agent_on.pg.sql(f"DELETE FROM saved_meal_plans WHERE id = '{legacy_body['saved_plan_id']}'")
    monkeypatch.setenv('DORMDISH_AGENT_GENERATION_ENABLED', 'true')
    agent_body = result(agent_on, route)
    for key in ('nutrition_targets', 'daily_targets', 'already_planned'):
        assert agent_body[key] == legacy_body[key], key
    assert agent_body['already_planned'] == {'breakfast': {'calories': 500.0, 'protein_g': 30.0,
                                                           'fat_g': 10.0, 'carbs_g': 60.0}}


@pytest.mark.parametrize('route', ['/generate-meal', '/generate-meal-stream'])
@pytest.mark.parametrize('first', ['Peanut Noodles', 'Mystery Stew'])
def test_flag_on_hard_allergen_and_unknown_metadata_are_never_selected(agent_on, route, first):
    agent_on.profiles['owner']['dietary_restrictions'] = ['peanut']
    agent_on.pg.sql(f"""UPDATE users SET dietary_restrictions = '["peanut"]' WHERE id = '{agent_on.owner.user_id}'""")
    agent_on.model.prefer = [first, 'Rice 🌶']
    body = result(agent_on, route)
    assert_items_are_trusted(body['meal_plan']['meals']['dinner'], 'Rice 🌶')
    [row] = plans(agent_on)
    saved = row['meal_plan_data']['meal_plan']['meals']['dinner']['items']
    assert [entry['name'] for entry in saved] == ['Rice 🌶']


@pytest.mark.parametrize('route', ROUTES)
def test_flag_on_model_failure_before_write_returns_existing_error_without_gemini(agent_on, route):
    agent_on.model.mode = 'fail'
    response = post(agent_on, route)
    assert_error_shape(response, route)
    assert agent_on.gemini == [] and plans(agent_on) == []


@pytest.mark.parametrize('route,cap', [('/generate-meal', 8), ('/generate-meal-stream', 8),
                                       ('/generate-meal-plan', 16), ('/generate-meal-plan-stream', 16)])
def test_flag_on_model_budget_is_bounded_per_request(agent_on, route, cap):
    agent_on.model.mode = 'loop'
    response = post(agent_on, route)
    assert_error_shape(response, route)
    # A full day shares one budget; three independent per-meal budgets would allow 24 model requests.
    assert 0 < len(agent_on.model.requests) <= cap
    assert agent_on.gemini == [] and plans(agent_on) == []


@pytest.mark.parametrize('route', ['/generate-meal-plan', '/generate-meal-plan-stream'])
def test_flag_on_full_day_shares_one_validation_repair(agent_on, route):
    agent_on.profiles['owner']['dietary_restrictions'] = ['peanut']
    agent_on.pg.sql(f"""UPDATE users SET dietary_restrictions = '["peanut"]' WHERE id = '{agent_on.owner.user_id}'""")
    agent_on.model.prefer = ['Peanut Pancakes', 'Oatmeal', 'Chicken Bowl', 'Peanut Noodles', 'Rice 🌶']
    response = post(agent_on, route)
    assert_error_shape(response, route)
    assert agent_on.gemini == [] and plans(agent_on) == []


def test_kill_switch_keeps_gemini_path(agent_on, monkeypatch):
    monkeypatch.setenv('DORMDISH_AGENT_KILL_SWITCH', 'true')
    body = result(agent_on, '/generate-meal')
    assert [kind for kind, _ in agent_on.gemini] == ['agent'] and agent_on.model.requests == []
    assert body['meal_plan']['meals'] == gemini_plan(['dinner'])['meals']


@pytest.mark.parametrize('route', ['/generate-meal', '/generate-meal-stream'])
def test_flag_on_existing_meal_limit_is_preserved_before_model(agent_on, route):
    seed(agent_on, {'meal': 'dinner', 'meal_plan': {'totals': {}}})
    response = post(agent_on, route)
    assert response.status_code == 429
    assert response.json() == http_error(429, f'You already have a generated dinner for {DAY}. '
                                              'Wait until tomorrow or delete the existing entry.')
    assert agent_on.model.requests == [] and len(plans(agent_on)) == 1


@pytest.mark.parametrize('route', ROUTES)
def test_flag_on_retry_for_same_date_never_double_saves(agent_on, route):
    result(agent_on, route)
    agent_on.model.requests.clear()
    retry = post(agent_on, route)
    assert retry.status_code in (409, 429) and retry.json()['error'] == 'HTTP error'
    assert len(plans(agent_on)) == 1 and agent_on.gemini == []


@pytest.mark.parametrize('route', ROUTES)
def test_flag_on_lost_write_reply_never_falls_back_or_writes_twice(agent_on, route):
    agent_on.db.lose_plan_write = True
    response = post(agent_on, route)
    [row] = plans(agent_on)
    assert row['date'] == DAY and agent_on.gemini == []
    if response.status_code == 200:
        body = complete(response)[1] if route in STREAMS else response.json()
        if body.get('auto_saved'):
            assert body['saved_plan_id'] == row['id']
    post(agent_on, route)
    assert len(plans(agent_on)) == 1 and agent_on.gemini == []


def test_flag_on_writes_are_owner_scoped(agent_on):
    agent_on.client.headers['Authorization'] = 'Bearer other'
    body = result(agent_on, '/generate-meal', user_id=agent_on.owner.user_id)
    assert plans(agent_on) == []
    [row] = plans(agent_on, agent_on.other)
    assert row['id'] == body['saved_plan_id']
