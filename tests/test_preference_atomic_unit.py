"""Synthetic PostgreSQL selfchecks for the existing preference UI's shared writes."""
from concurrent.futures import ThreadPoolExecutor
import ast
from pathlib import Path
from uuid import uuid4

import pytest

from memory.preference_manager import PreferenceManager
from tests.test_agent_storage_unit import pg, stores, terminal
from dormdish_agent.models import TurnRequest


@pytest.fixture(scope='module', autouse=True)
def preference_schema(pg):
    pg.sql('''CREATE TABLE user_preferences (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        category text NOT NULL CHECK (category IN ('favorite', 'dislike')), item_name text NOT NULL,
        item_type text NOT NULL CHECK (item_type IN ('meal_plan', 'food_item')),
        meal_type text NOT NULL CHECK (meal_type IN ('breakfast', 'lunch', 'dinner')),
        meal_plan_id text, reason text, created_at timestamptz NOT NULL DEFAULT now());
        CREATE UNIQUE INDEX ON user_preferences(user_id, category, lower(item_name), meal_type);
        GRANT SELECT, INSERT, UPDATE, DELETE ON user_preferences TO service_role;''')
    pg.sql((Path(__file__).resolve().parents[1] / 'migrations/atomic_preferences.sql').read_text())


def test_button_choices_replace_opposite_and_keep_other_owner_and_meal(stores):
    owner, other = stores
    manager = PreferenceManager(owner.client)
    first = manager.add_favorite_food_item(owner.user_id, 'Mushrooms', 'dinner')['data'][0]
    manager.add_favorite_food_item(owner.user_id, 'Mushrooms', 'lunch')
    manager.add_favorite_food_item(other.user_id, 'Mushrooms', 'dinner')
    updated = manager.add_dislike(owner.user_id, ' mushrooms ', 'food_item', 'dinner')['data'][0]
    assert updated['id'] == first['id']
    assert updated['category'] == 'dislike'
    assert owner.client.sql(f"SELECT count(*) FROM user_preferences WHERE user_id = '{owner.user_id}'") == '2'
    assert owner.client.sql(f"SELECT category FROM user_preferences WHERE user_id = '{other.user_id}'") == 'favorite'


def test_forget_and_replay_invalidation_are_one_transaction(stores):
    owner, other = stores
    manager = PreferenceManager(owner.client)
    manager.add_favorite_food_item(owner.user_id, 'Mushrooms', 'dinner')
    c = owner.create_conversation()
    _, run = owner.claim_turn(c.id, TurnRequest(request_id=uuid4(), text='An earlier instruction'))
    assert manager.remove_favorite(owner.user_id, ' MUSHROOMS ')['status'] == 'deleted'
    owner.finish_turn(terminal(run), [{'role': 'user', 'content': 'old favorite'}])
    assert owner.load_history(c.id) == []
    assert manager.remove_favorite(owner.user_id, 'Mushrooms')['status'] == 'not_found'
    assert owner.client.sql(f"SELECT count(*) FROM user_preferences WHERE user_id = '{owner.user_id}'") == '0'


def test_history_failure_rolls_back_preference_write(stores):
    owner, _ = stores
    manager = PreferenceManager(owner.client)
    owner.create_conversation()
    owner.client.sql('''CREATE FUNCTION reject_preference_epoch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic history failure'; END $$;
        CREATE TRIGGER reject_preference_epoch BEFORE UPDATE ON agent_conversations
        FOR EACH ROW EXECUTE FUNCTION reject_preference_epoch();''')
    try:
        with pytest.raises(RuntimeError, match='refresh preferences before retrying'):
            manager.add_favorite_food_item(owner.user_id, 'Mushrooms', 'dinner')
        assert owner.client.sql(f"SELECT count(*) FROM user_preferences WHERE user_id = '{owner.user_id}'") == '0'
    finally:
        owner.client.sql('DROP TRIGGER reject_preference_epoch ON agent_conversations; DROP FUNCTION reject_preference_epoch()')


def test_concurrent_opposite_buttons_leave_one_choice(stores):
    owner, _ = stores
    manager = PreferenceManager(owner.client)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(manager.add_favorite_food_item, owner.user_id, 'Rice', 'dinner'),
                   pool.submit(manager.add_dislike, owner.user_id, 'RICE', 'food_item', 'dinner')]
        assert all(f.result()['status'] == 'success' for f in futures)
    assert owner.client.sql(f"SELECT count(*) FROM user_preferences WHERE user_id = '{owner.user_id}'") == '1'


def test_untrusted_database_roles_cannot_select_another_owner(stores):
    owner, _ = stores
    for role in ('anon', 'authenticated'):
        with pytest.raises(RuntimeError, match='permission denied'):
            owner.client.sql(f"SET ROLE {role}; SELECT preference_forget('{owner.user_id}', 'favorite', 'Rice')")


def test_invalid_preferences_do_not_write_or_leak_sql(stores):
    owner, _ = stores
    manager = PreferenceManager(owner.client)
    with pytest.raises(RuntimeError) as failure:
        manager.add_dislike(owner.user_id, ' ', 'food_item', 'dinner')
    assert 'SQL' not in str(failure.value) and 'preference_invalid' not in str(failure.value)
    assert owner.client.sql(f"SELECT count(*) FROM user_preferences WHERE user_id = '{owner.user_id}'") == '0'


def test_http_preference_models_reject_invalid_choices_before_storage():
    from typing import Literal, Optional
    from pydantic import BaseModel, Field, ValidationError
    source = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text())
    classes = [node for node in source.body if isinstance(node, ast.ClassDef)
               and node.name in ('FavoriteRequest', 'DislikeRequest')]
    scope = dict(BaseModel=BaseModel, Field=Field, Literal=Literal, Optional=Optional)
    exec(compile(ast.Module(body=classes, type_ignores=[]), 'app.py', 'exec'), scope)
    for name in ('FavoriteRequest', 'DislikeRequest'):
        valid = dict(item_name='Rice', item_type='food_item', meal_type='dinner')
        scope[name](**valid)
        for field, value in [('item_name', ' \t'), ('item_name', 'x' * 201),
                             ('item_type', 'instruction'), ('meal_type', 'unknown')]:
            with pytest.raises(ValidationError):
                scope[name](**{**valid, field: value})
