"""Focused self-checks for the legacy generation adapters; the companion PR owns acceptance cases."""
import asyncio
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import pytest

from dormdish_agent import legacy
from dormdish_agent.models import MealItem, MealProposal, Nutrition, PlanningContext
from dormdish_agent.tools import FoodContext

HINT = {'calories': 600, 'protein_g': 40, 'fat_g': 20, 'carbs_g': 60}


def proposal(meal, calories=500.0):
    item = MealItem(menu_item_id=f'digest:{meal}', name=f'{meal} bowl', serving_size='1 bowl', servings=2.0,
                    calories=calories, protein_g=30.0, fat_g=10.0, carbs_g=50.0)
    return MealProposal(id=uuid4(), version=1, dining_hall='busch-dining-hall', date=date(2026, 10, 12),
                        meal_type=meal, description='', items=[item], totals=Nutrition(**item.model_dump(
                            include=set(Nutrition.model_fields))))


class Turns:
    """Stub of the model boundary: records the budget each meal receives."""
    def __init__(self, model_calls=5, attempts=1, fail=()):
        self.seen, self.model_calls, self.attempts, self.fail = [], model_calls, attempts, fail

    def __call__(self, context, request, conversation_id, history, *, client, deadline_seconds):
        outer = self
        meal = request.context.meal_type
        outer.seen.append((meal, context.model_limit, context.validation_limit, deadline_seconds, context.restaurant))

        class Turn:
            state = SimpleNamespace(status='complete')
            assistant_text = f'{meal} text'

            async def stream(self):
                yield SimpleNamespace(type='status', data=SimpleNamespace(message='Checking meal options'))
                context.model_calls = min(outer.model_calls, context.model_limit)
                context.validation_attempts = min(outer.attempts, context.validation_limit)
                context.proposal = None if meal in outer.fail else proposal(meal)
                yield SimpleNamespace(type='complete')
        return Turn()


@pytest.fixture
def turns(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        async def close(self): pass
    monkeypatch.setattr(legacy, 'AsyncOpenAI', Client)
    monkeypatch.setattr(legacy, 'build_context', lambda client, user, request, get_targets: SimpleNamespace(
        model_calls=0, validation_attempts=0, proposal=None))
    def install(**kwargs):
        stub = Turns(**kwargs)
        monkeypatch.setattr(legacy, 'AgentTurn', stub)
        return stub
    return install


def collect(targets, restaurant=None):
    async def run():
        return [chunk async for chunk in legacy.events(None, {}, None, 'busch', '2026-10-12', targets, restaurant)]
    return asyncio.run(run())


def test_flag_defaults_off_and_kill_switch_wins(monkeypatch):
    monkeypatch.delenv('DORMDISH_AGENT_GENERATION_ENABLED', raising=False)
    monkeypatch.delenv('DORMDISH_AGENT_KILL_SWITCH', raising=False)
    assert not legacy.enabled()
    monkeypatch.setenv('DORMDISH_AGENT_GENERATION_ENABLED', 'TRUE')
    assert not legacy.enabled()
    monkeypatch.setenv('DORMDISH_AGENT_GENERATION_ENABLED', 'true')
    assert legacy.enabled()
    monkeypatch.setenv('DORMDISH_AGENT_KILL_SWITCH', 'true')
    assert not legacy.enabled()


def test_full_day_shares_one_budget_and_recomputes_totals(turns):
    stub = turns(model_calls=5, attempts=1)
    chunks = collect({m: HINT for m in ('breakfast', 'lunch', 'dinner')}, restaurant='Cafe')
    assert [c['type'] for c in chunks] == ['thinking'] * 3 + ['complete']
    assert [(meal, models, repairs) for meal, models, repairs, _, _ in stub.seen] == [
        ('breakfast', 8, 2), ('lunch', 8, 2), ('dinner', 6, 2)]
    assert all(0 < deadline <= 45 for *_, deadline, _ in stub.seen)
    assert {restaurant for *_, restaurant in stub.seen} == {'Cafe'}
    plan = chunks[-1]['meal_plan']
    assert list(plan['meals']) == ['breakfast', 'lunch', 'dinner']
    assert plan['totals'] == {'calories': 1500.0, 'protein_g': 90.0, 'fat_g': 30.0, 'carbs_g': 150.0}
    assert plan['meals']['lunch']['items'][0]['menu_item_id'] == 'digest:lunch'
    assert chunks[-1]['raw_response'] == plan['explanation'] == 'breakfast text\n\nlunch text\n\ndinner text'


def test_single_repair_is_shared_across_meals(turns):
    stub = turns(model_calls=4, attempts=2)
    collect({m: HINT for m in ('breakfast', 'lunch', 'dinner')})
    assert [repairs for _, _, repairs, _, _ in stub.seen] == [2, 1, 1]
    single = turns()
    collect({'dinner': HINT})
    assert single.seen[0][1:3] == (8, 2)


def test_budget_exhaustion_or_missing_proposal_fails_before_complete(turns):
    turns(model_calls=8)
    with pytest.raises(ValueError):
        collect({m: HINT for m in ('breakfast', 'lunch', 'dinner')})
    turns(fail=('lunch',))
    with pytest.raises(ValueError):
        collect({'breakfast': HINT, 'lunch': HINT})
    with pytest.raises(ValueError):
        collect({})


def test_restaurant_filter_and_limits_apply_to_food_context():
    rows = [{'university': 'Rutgers University', 'dining_hall': 'busch-dining-hall', 'date': '2026-10-10',
             'fetched_at': datetime.now(timezone.utc).isoformat(), 'menu_data': {'items': [
                 {'name': name, 'meal': 'dinner', 'restaurant': restaurant, 'serving_size': '1 cup',
                  'calories': 200, 'protein_g': 4, 'fat_g': 1, 'carbs_g': 44}
                 for name, restaurant in (('Rice', 'Wok'), ('Pizza', 'Oven'))]}}]
    query = SimpleNamespace(execute=lambda: SimpleNamespace(data=rows))
    for name in ('select', 'eq', 'order', 'limit'):
        setattr(query, name, lambda *a, **k: query)
    context = FoodContext(SimpleNamespace(table=lambda _: query), str(uuid4()), 'rutgers',
                          PlanningContext(dining_hall='busch', date=date(2026, 10, 10), meal_type='dinner'),
                          Nutrition(calories=2000, protein_g=100, fat_g=70, carbs_g=200), restaurant='wok',
                          model_limit=1, validation_limit=1)
    assert [item['name'] for item in asyncio.run(context.menu()).items] == ['Rice']
    context.claim_model()
    with pytest.raises(ValueError):
        context.claim_model()


class Storage:
    def __init__(self):
        self.inserts, self.filters = [], []

    def rpc(self, name, params):
        assert name == 'agent_save_legacy_plan'
        row = {'id': params['p_plan_id'], 'user_id': params['p_user_id'],
               'dining_hall': params['p_dining_hall'], 'date': params['p_date'],
               'meal_plan_data': params['p_payload']}
        return self.table('saved_meal_plans').insert(row)

    def table(self, name):
        storage = self

        class Query:
            data = []
            def select(self, *a): return self
            def eq(self, *a): storage.filters.append(('eq',) + a); return self
            def in_(self, *a): storage.filters.append(('in',) + a); return self
            def update(self, *a): return self
            def insert(self, row):
                storage.inserts.append((name, row))
                return SimpleNamespace(execute=lambda: SimpleNamespace(data=[row]))
            def execute(self): return self
        return Query()


@pytest.fixture
def legacy_app(monkeypatch):
    import app
    user = {'id': str(uuid4()), 'age': 20, 'height_feet': 5, 'height_inches': 10, 'weight_lbs': 160,
            'activity_level': 'moderately_active', 'goal': 'maintain', 'gender': 'male', 'university': 'rutgers'}
    storage = Storage()
    calls = []
    async def menu(hall, day):
        return {'items': [{'name': 'Rice', 'meal': 'dinner', 'calories': 200, 'protein_g': 4}]}
    async def gemini(**kwargs):
        calls.append('gemini')
        raise ValueError('rollback path reached')
    async def agent(client, user_arg, get_targets, hall, day, targets, restaurant=None):
        calls.append(('agent', day, list(targets)))
        yield {'type': 'thinking', 'content': 'Checking meal options'}
        yield {'type': 'complete', 'meal_plan': {'meals': {m: {'description': '', 'items': []} for m in targets},
               'totals': {'calories': 1.0, 'protein_g': 1.0, 'fat_g': 1.0, 'carbs_g': 1.0}, 'explanation': 'x'},
               'raw_response': 'x'}
    async def nothing(*a, **k): pass
    import meal_plan_agent
    monkeypatch.setattr(app, 'supabase', storage)
    monkeypatch.setattr(app, 'verify_session_token', lambda token: dict(user))
    monkeypatch.setattr(app, 'get_dining_hall_menu', menu)
    monkeypatch.setattr(app, 'check_and_fulfill_referral', nothing)
    monkeypatch.setattr(meal_plan_agent, 'generate_meal_plan_with_agent', gemini)
    monkeypatch.setattr(legacy, 'events', agent)
    monkeypatch.setenv('DORMDISH_AGENT_GENERATION_ENABLED', 'true')
    monkeypatch.delenv('DORMDISH_AGENT_KILL_SWITCH', raising=False)
    return SimpleNamespace(app=app, storage=storage, calls=calls, monkeypatch=monkeypatch)


def test_single_meal_agent_path_saves_requested_date(legacy_app):
    app = legacy_app.app
    result = asyncio.run(app.generate_single_meal_route(
        app.SingleMealRequest(meal='Dinner', dining_hall='busch', date='2030-01-02'), 'Bearer t'))
    assert legacy_app.calls == [('agent', '2030-01-02', ['dinner'])]
    assert result['auto_saved'] is True and result['meal'] == 'dinner' and result['generation_id']
    (table, row), = legacy_app.storage.inserts
    assert table == 'saved_meal_plans' and row['date'] == '2030-01-02' and row['id'] == result['saved_plan_id']
    assert row['meal_plan_data']['meal'] == 'dinner'


def test_full_plan_agent_path_checks_requested_date_and_respects_exclusions(legacy_app):
    app = legacy_app.app
    result = asyncio.run(app.generate_meal_plan_route(
        app.MealPlanRequest(dining_hall='busch', date='2030-01-02', exclude_meals=['Breakfast']), 'Bearer t'))
    assert legacy_app.calls == [('agent', '2030-01-02', ['lunch', 'dinner'])]
    assert ('in', 'date', [app.datetime.now(app.ZoneInfo('America/New_York')).date().isoformat(), '2030-01-02']) \
        in legacy_app.storage.filters
    assert legacy_app.storage.inserts[0][1]['date'] == '2030-01-02' and result['ask_feedback']


def test_stream_agent_path_keeps_sse_shape_and_saves_requested_date(legacy_app):
    app = legacy_app.app
    async def body(response):
        return [json.loads(chunk.removeprefix('data: ')) async for chunk in response.body_iterator]
    response = asyncio.run(app.generate_single_meal_stream_route(
        app.SingleMealRequest(meal='dinner', dining_hall='busch', date='2030-01-02'), 'Bearer t'))
    chunks = asyncio.run(body(response))
    assert [c['type'] for c in chunks] == ['thinking', 'thinking', 'complete']
    assert chunks[-1]['auto_saved'] is True and chunks[-1]['meal'] == 'dinner'
    (_, row), = legacy_app.storage.inserts
    assert row['date'] == row['meal_plan_data']['date'] == '2030-01-02'


def test_agent_failure_uses_legacy_error_and_never_writes(legacy_app):
    app = legacy_app.app
    async def broken(*a, **k):
        raise ValueError('agent failed')
        yield
    legacy_app.monkeypatch.setattr(legacy, 'events', broken)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(app.generate_single_meal_route(
            app.SingleMealRequest(meal='dinner', dining_hall='busch', date='2030-01-02'), 'Bearer t'))
    assert (caught.value.status_code, caught.value.detail) == (500, 'Please try again.')
    response = asyncio.run(app.generate_meal_plan_stream_route(
        app.MealPlanRequest(dining_hall='busch', date='2030-01-02'), 'Bearer t'))
    async def body():
        return [chunk async for chunk in response.body_iterator]
    assert asyncio.run(body()) == ['data: {"type": "error", "error": "Please try again."}\n\n']
    assert legacy_app.storage.inserts == [] and 'gemini' not in legacy_app.calls


def test_flag_off_keeps_gemini_rollback(legacy_app):
    app = legacy_app.app
    legacy_app.monkeypatch.setenv('DORMDISH_AGENT_GENERATION_ENABLED', 'false')
    with pytest.raises(HTTPException):
        asyncio.run(app.generate_single_meal_route(
            app.SingleMealRequest(meal='dinner', dining_hall='busch', date='2030-01-02'), 'Bearer t'))
    assert legacy_app.calls == ['gemini']
