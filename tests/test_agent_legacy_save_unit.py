"""Check exact-candidate recovery; SQL/concurrency coverage lives in the companion test PR."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dormdish_agent import legacy


@pytest.mark.parametrize('reply', ['saved', 'empty', 'lost', 'unavailable'])
def test_save_calls_once_and_reconciles_only_own_candidate(reply):
    row = {'id': str(uuid4()), 'user_id': str(uuid4()), 'date': '2030-01-02',
           'dining_hall': 'busch', 'meal_plan_data': {'meal': 'dinner'}}
    calls, filters = [], []
    class Client:
        def rpc(self, name, params):
            calls.append((name, params))
            def execute():
                if reply in ('lost', 'unavailable'):
                    raise ConnectionError('private database details')
                return SimpleNamespace(data=[{'id': row['id']}] if reply == 'saved' else [])
            return SimpleNamespace(execute=execute)
        def table(self, name):
            assert name == 'saved_meal_plans'
            return self
        def select(self, columns):
            assert columns == 'id'
            return self
        def eq(self, name, value):
            filters.append((name, value))
            return self
        def execute(self):
            if reply == 'unavailable':
                raise ConnectionError('read failed')
            return SimpleNamespace(data=[{'id': row['id']}] if reply == 'lost' else [])
    if reply == 'unavailable':
        with pytest.raises(ConnectionError, match='read failed'):
            legacy.save(Client(), row)
    else:
        assert bool(legacy.save(Client(), row).data) == (reply in ('saved', 'lost'))
    assert calls == [('agent_save_legacy_plan', {'p_user_id': row['user_id'], 'p_plan_id': row['id'],
        'p_dining_hall': row['dining_hall'], 'p_date': row['date'], 'p_payload': row['meal_plan_data']})]
    assert filters == ([] if reply == 'saved' else [('user_id', row['user_id']), ('id', row['id'])])
