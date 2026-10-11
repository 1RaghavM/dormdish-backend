"""Independent read-tool trust boundaries; only the storage transport is synthetic."""
import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agents import RunContextWrapper

from dormdish_agent.models import Nutrition, PlanningContext
from dormdish_agent.tools import FoodContext, preview_meal, read_intake, read_profile, retrieve_menu


class ReadDatabase:
    def __init__(self, **tables):
        self.tables, self.queries = tables, []

    def table(self, name):
        query = {'table': name, 'filters': []}
        self.queries.append(query)
        database = self

        class Query:
            def select(self, fields):
                query['select'] = fields
                return self

            def eq(self, key, value):
                query['filters'].append((key, value))
                return self

            def order(self, key, **options):
                query['order'] = (key, options)
                return self

            def limit(self, count):
                query['limit'] = count
                return self

            def execute(self):
                # Deliberately do not filter: privileged storage rows must be revalidated.
                return SimpleNamespace(data=deepcopy(database.tables.get(name, [])))

        return Query()


def menu_row():
    return {'university': 'Rutgers University', 'dining_hall': 'busch-dining-hall',
            'date': '2026-10-10', 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'menu_data': {'items': [{'id': 'rice', 'name': 'Rice', 'meal': 'dinner',
                'serving_size': '1 cup', 'calories': 200, 'protein_g': 4, 'fat_g': 1,
                'carbs_g': 44, 'allergens': [], 'dietary_tags': ['vegan']}]}}


def food_context(*, row=None, logs=(), **kwargs):
    return FoodContext(ReadDatabase(menus=[row or menu_row()], food_log=list(logs)), str(uuid4()),
        'rutgers', PlanningContext(dining_hall='busch', date=date(2026, 10, 10), meal_type='dinner'),
        Nutrition(calories=2000, protein_g=100, fat_g=70, carbs_g=200), **kwargs)


def invoke(tool, context, arguments='{}'):
    return asyncio.run(tool.on_invoke_tool(RunContextWrapper(context), arguments))


@pytest.mark.parametrize('field,value', [
    ('university', 'Other University'), ('dining_hall', 'other-hall'), ('date', '2026-10-11'),
    ('meal', 'lunch'), ('fetched_at', '2026-01-01T00:00:00+00:00'),
    ('fetched_at', '2099-01-01T00:00:00+00:00'), ('fetched_at', '2026-10-10T12:00:00'),
])
def test_menu_scope_and_freshness_fail_closed(field, value):
    row = menu_row()
    row[field] = value
    context = food_context(row=row)
    result = invoke(retrieve_menu, context)
    assert isinstance(result, str) and 'could not verify' in result
    assert context.snapshot is None
    query = context.client.queries[0]
    assert query['filters'] == [('university', 'Rutgers University'),
        ('dining_hall', 'busch-dining-hall'), ('date', '2026-10-10')]
    assert query['limit'] == 10


@pytest.mark.parametrize('kwargs,item_change', [
    ({'allergens': ('milk',)}, {'allergens': None}),
    ({'allergens': ('milk',)}, {'allergens': ['dairy']}),
    ({'allergens': ('milk',)}, {'allergens': ['unrecognized']}),
    ({'restrictions': ('vegan',)}, {'dietary_tags': None}),
    ({'restrictions': ('vegan',)}, {'dietary_tags': ['vegetarian']}),
])
def test_tool_preview_never_relaxes_hard_safety(kwargs, item_change):
    import json
    row = menu_row()
    row['menu_data']['items'][0].update(item_change)
    context = food_context(row=row, **kwargs)
    invoke(retrieve_menu, context)
    key = context.snapshot.items[0]['menu_item_id']
    result = invoke(preview_meal, context, json.dumps({'selections': [{'menu_item_id': key, 'servings': 1}]}))
    assert isinstance(result, str) and 'could not verify' in result
    assert context.proposal is None


def test_intake_rejects_cross_owner_wrong_date_and_conflicting_duplicate():
    context = food_context()
    log = dict(id='log-1', user_id=context.user_id, date='2026-10-10',
               calories=300, protein_g=15, fat_g=10, carbs_g=40)
    context.client.tables['food_log'] = [log, deepcopy(log)]
    result = invoke(read_intake, context)
    assert result['remaining_after_food_logs'] == dict(calories=1700, protein_g=85, fat_g=60, carbs_g=160)
    assert result['completion_records_available'] is False and result['limitation']
    for change in [{'user_id': str(uuid4())}, {'date': '2026-10-11'}, {'calories': 301}]:
        context.client.tables['food_log'] = [log, {**log, **change}]
        result = invoke(read_intake, context)
        assert isinstance(result, str) and 'could not verify' in result
    assert all(q['filters'] == [('user_id', context.user_id), ('date', '2026-10-10')]
               for q in context.client.queries)


@pytest.mark.parametrize('boundary', ['menu', 'food_logs'])
def test_storage_context_size_limits(boundary):
    context = food_context()
    if boundary == 'menu':
        context.client.tables['menus'][0]['menu_data']['items'][0]['dietary_tags'] = ['x' * 128_001]
        result = invoke(retrieve_menu, context)
    else:
        context.client.tables['food_log'] = [{}] * 501
        result = invoke(read_intake, context)
        assert context.client.queries[-1]['limit'] == 501
    assert isinstance(result, str) and 'could not verify' in result


def test_stale_snapshot_cannot_be_previewed_after_read():
    import json
    context = food_context()
    invoke(retrieve_menu, context)
    from dataclasses import replace
    context.snapshot = replace(context.snapshot, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    key = context.snapshot.items[0]['menu_item_id']
    assert 'could not verify' in invoke(preview_meal, context,
        json.dumps({'selections': [{'menu_item_id': key, 'servings': 1}]}))
    assert context.proposal is None


def test_read_profile_excludes_identifiers_and_does_not_persist_tastes():
    context = food_context(preferences={'dislikes': ['mushrooms']})
    result = invoke(read_profile, context)
    assert result['preferences'] == {'dislikes': ['mushrooms']}
    assert not {'user_id', 'email', 'client', 'subscription'} & result.keys()
    assert context.client.queries == []
