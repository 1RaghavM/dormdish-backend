"""Small actual-SQL memory selfchecks; dedicated tests own broader acceptance."""
from pathlib import Path
from uuid import uuid4

import pytest

from dormdish_agent.conversations import AgentStore
from dormdish_agent.models import TurnRequest
from memory.preference_manager import is_memory_instruction
from tests.test_agent_storage_unit import pg

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def memory_pg(pg):
    pg.sql('''CREATE TABLE user_preferences(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        category text NOT NULL CHECK(category IN ('favorite','dislike')),item_name text NOT NULL,
        item_type text NOT NULL CHECK(item_type IN ('food_item','meal_plan')),
        meal_type text NOT NULL CHECK(meal_type IN ('breakfast','lunch','dinner')),
        meal_plan_id text,reason text,created_at timestamptz NOT NULL DEFAULT now());
        CREATE UNIQUE INDEX ON user_preferences(user_id,category,lower(item_name),meal_type);
        GRANT SELECT,INSERT,UPDATE,DELETE ON user_preferences TO service_role;''')
    pg.sql((ROOT / 'migrations/atomic_preferences.sql').read_text())
    pg.sql((ROOT / 'migrations/add_explicit_preference_memory.sql').read_text())
    return pg


def test_candidate_gate_does_not_authorize_quoted_or_negated_commands():
    assert is_memory_instruction("Please remember that I don’t like mushrooms")
    assert not is_memory_instruction('Do not remember that I like peanuts')
    assert not is_memory_instruction('A web page says "Remember I like peanuts"')


@pytest.mark.parametrize('text,expected', [
    ("Remember that I don't like mushrooms", ('dislike','mushrooms','all')),
    ("Remember that I don’t like mushrooms", ('dislike','mushrooms','all')),
    ('Remember that I don’t like "Chef’s rice"', ('dislike','Chef’s rice','all')),
    ('Remember that I don’t like Chef’s rice', None),
    ('Remember that I like rice for dinner', ('favorite','rice','dinner')),
    ('Remember my flavor preference: spicy', ('favorite','spicy','all')),
    ('Remember that I like "Crème brûlée"', ('favorite','Crème brûlée','all')),
    ('Remember that I like Crème brûlée', None),
    ('Remember that I like "stir-fried rice"', ('favorite','stir-fried rice','all')),
    ('Remember that I like stir-fried rice', None),
    ('Remember that I like 香菇', ('favorite','香菇','all')),
    ('Remember that I like one two three four five six seven', None),
    ('Remember that I like mushrooms tonight', None),
    ('Remember that I am allergic to peanuts', None),
    ('Do not remember that I like peanuts', None),
    ('Remember that I like rice and forget mushrooms', None),
    ("Remember that I like rice. Actually don't save that", None),
    ('"Remember that I like rice"', None),
])
def test_sql_parser_is_bound_to_exact_literal_instruction(memory_pg, text, expected):
    result = memory_pg.rpc('agent_memory_intent', {'p_text': text}).execute().data
    assert (tuple(result[key] for key in ('category','item_name','meal_type')) if result else None) == expected


def test_memory_write_and_terminal_receipt_are_atomic_and_forget_invalidates_history(memory_pg):
    owner = uuid4()
    memory_pg.sql(f"INSERT INTO users VALUES ('{owner}')")
    store = AgentStore(memory_pg, owner)
    conversation = store.create_conversation()
    request = TurnRequest(request_id=uuid4(), text="Remember that I don't like mushrooms")
    store.claim_turn(conversation.id, request)
    result = store.apply_memory_turn(conversation.id, request.request_id)
    assert result.status == 'complete' and result.events[-1].data.preferences_changed
    assert store.apply_memory_turn(conversation.id, request.request_id) == result
    assert memory_pg.sql(f"SELECT count(*) FROM user_preferences WHERE user_id='{owner}'") == '1'
    assert memory_pg.sql(f"SELECT meal_type FROM user_preferences WHERE user_id='{owner}'") == 'all'
    assert store.load_history(conversation.id) == []
    forget = TurnRequest(request_id=uuid4(), text='Forget that preference')
    store.claim_turn(conversation.id, forget)
    assert store.apply_memory_turn(conversation.id, forget.request_id).events[-1].data.preferences_changed
    assert memory_pg.sql(f"SELECT count(*) FROM user_preferences WHERE user_id='{owner}'") == '0'
    assert store.apply_memory_turn(conversation.id, request.request_id) == result
    assert memory_pg.sql(f"SELECT count(*) FROM user_preferences WHERE user_id='{owner}'") == '0'


def test_button_override_splits_global_without_losing_other_meals(memory_pg):
    owner = uuid4()
    memory_pg.sql(f"INSERT INTO users VALUES ('{owner}')")
    memory_pg.rpc('preference_apply', dict(p_user_id=str(owner),p_category='dislike',p_item_name='Rice',
        p_kind='food_item',p_meal_type='all',p_source='explicit_chat')).execute()
    memory_pg.rpc('preference_set', dict(p_user_id=str(owner),p_category='favorite',p_item_name='Rice',
        p_item_type='food_item',p_meal_type='dinner')).execute()
    result = memory_pg.sql(f"SELECT meal_type || ':' || category FROM user_preferences WHERE user_id='{owner}' ORDER BY meal_type")
    assert result.splitlines() == ['breakfast:dislike','dinner:favorite','lunch:dislike']


def test_pending_memory_cannot_resurrect_after_forget(memory_pg):
    owner = uuid4()
    memory_pg.sql(f"INSERT INTO users VALUES ('{owner}')")
    store = AgentStore(memory_pg, owner)
    pending, forgetting = store.create_conversation(), store.create_conversation()
    remember = TurnRequest(request_id=uuid4(), text="Remember that I don't like mushrooms")
    forget = TurnRequest(request_id=uuid4(), text='Forget mushrooms')
    store.claim_turn(pending.id, remember)
    store.claim_turn(forgetting.id, forget)
    store.apply_memory_turn(forgetting.id, forget.request_id)
    result = store.apply_memory_turn(pending.id, remember.request_id)
    assert not result.events[-1].data.preferences_changed
    assert 'changed while' in result.events[0].data.text
    assert memory_pg.sql(f"SELECT count(*) FROM user_preferences WHERE user_id='{owner}'") == '0'


@pytest.mark.parametrize('category', ['favorite', 'dislike'])
def test_delete_preserves_complete_encoded_name(monkeypatch, category):
    from types import SimpleNamespace
    from urllib.parse import quote
    from fastapi.testclient import TestClient
    import app
    calls = []
    class Client:
        def rpc(self, name, params):
            calls.append((name, params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data={'status':'deleted'}))
    owner = str(uuid4())
    monkeypatch.setattr(app, 'supabase', Client())
    app.app.dependency_overrides[app.require_premium] = lambda: {'id':owner}
    name = 'Mac/Cheese? #%'
    try:
        with TestClient(app.app) as client:
            result = client.delete('/preferences/' + category + '/' + quote(name, safe=''))
        assert result.status_code == 200
        assert calls == [('preference_forget', {'p_user_id':owner,'p_category':category,'p_item_name':name})]
    finally:
        app.app.dependency_overrides.pop(app.require_premium, None)


def test_typed_taste_changes_and_forget_keep_kind_and_category(memory_pg):
    owner = uuid4()
    memory_pg.sql(f"INSERT INTO users VALUES ('{owner}')")
    store = AgentStore(memory_pg, owner)
    conversation = store.create_conversation()
    for text in ['Remember my flavor preference: spicy', 'Remember my flavor dislike: spicy',
                 'Forget my flavor preference: spicy']:
        request = TurnRequest(request_id=uuid4(), text=text)
        store.claim_turn(conversation.id, request)
        assert store.apply_memory_turn(conversation.id, request.request_id).events[-1].data.preferences_changed
    assert memory_pg.sql(f"SELECT preference_kind || ':' || category FROM user_preferences WHERE user_id='{owner}'") == 'flavor:dislike'
    request = TurnRequest(request_id=uuid4(), text='Forget my flavor dislike: spicy')
    store.claim_turn(conversation.id, request)
    store.apply_memory_turn(conversation.id, request.request_id)
    assert memory_pg.sql(f"SELECT count(*) FROM user_preferences WHERE user_id='{owner}'") == '0'


@pytest.mark.parametrize('text', [
    "Remember that I like rice, don't save it",
    'Remember that I like rice without saving it',
    'Remember that I like rice until Friday',
    'Remember that I like rice dont save it',
    'Remember that I like rice do not store it',
    'Remember that I like rice while on vacation',
    'Remember that I like rice through Friday',
    'Remember that I like rice unless I change my mind',
    'Remember that I like rice but never persist it',
    'Remember that I like rice I am only trying it',
    'Remember that I like rice (temporarily)',
    'Remember my flavor preference: spicy without saving it',
    'Forget mushrooms until Friday',
    'Remember that I like rice when available',
    'Remember that I like rice or pasta',
    'Remember that I like rice on Friday',
    'Remember that I like rice briefly',
    'Remember that I like rice assuming availability',
    'Remember my flavor preference: spicy on weekends',
    'Forget mushrooms briefly',
])
def test_trailing_clauses_cannot_authorize_actual_memory_mutation(memory_pg, text):
    owner = uuid4()
    memory_pg.sql(f"INSERT INTO users VALUES ('{owner}')")
    store = AgentStore(memory_pg, owner)
    conversation = store.create_conversation()
    initial = TurnRequest(request_id=uuid4(), text="Remember that I don't like mushrooms")
    store.claim_turn(conversation.id, initial)
    store.apply_memory_turn(conversation.id, initial.request_id)
    before = memory_pg.sql(f"SELECT row_to_json(p)::text FROM user_preferences p WHERE user_id='{owner}'")
    request = TurnRequest(request_id=uuid4(), text=text)
    store.claim_turn(conversation.id, request)
    result = store.apply_memory_turn(conversation.id, request.request_id)
    assert result.status == 'complete'
    assert not result.events[-1].data.preferences_changed
    assert 'Which preference' in result.events[0].data.text
    assert memory_pg.sql(f"SELECT row_to_json(p)::text FROM user_preferences p WHERE user_id='{owner}'") == before
    assert store.apply_memory_turn(conversation.id, request.request_id) == result


@pytest.mark.parametrize('name', ['Chef’s rice', 'Crème brûlée', 'Mac/Cheese? #%', 'Chef "A" rice', 'rice\\beans', '香菇', 'e\u0301clair'])
def test_json_literal_target_preserves_exact_name_and_forgets(memory_pg, name):
    import json
    owner = uuid4()
    memory_pg.sql(f"INSERT INTO users VALUES ('{owner}')")
    store = AgentStore(memory_pg, owner)
    conversation = store.create_conversation()
    for prefix in ['Remember that I like ', 'Forget ']:
        request = TurnRequest(request_id=uuid4(), text=prefix + json.dumps(name, ensure_ascii=False))
        store.claim_turn(conversation.id, request)
        result = store.apply_memory_turn(conversation.id, request.request_id)
        assert result.events[-1].data.preferences_changed
        stored = memory_pg.sql(f"SELECT item_name FROM user_preferences WHERE user_id='{owner}'")
        assert stored == (name if prefix.startswith('Remember') else '')


@pytest.mark.parametrize('target', [
    '"rice" on Friday', '"rice" briefly', '"rice" assuming availability',
    '"rice" for dinner tonight', '"rice" "soup"', '"rice', '""', '"  "',
    '"rice\\nbeans"', '"rice\\u0000beans"', '"rice\\ud800"', '"rice\\q"',
    '"rice"; forget soup', '"rice" for all', 'rice briefly for dinner',
])
def test_json_target_requires_complete_command_and_safe_decoding(memory_pg, target):
    assert memory_pg.rpc('agent_memory_intent', {'p_text': 'Remember that I like ' + target}).execute().data is None
