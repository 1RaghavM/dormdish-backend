"""Implementation selfchecks for agent_save_turn on the local PostgreSQL container (DD_AGENT_LOCAL_PG=1)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

import pytest

from dormdish_agent.conversations import StorageError, StoredAction
from dormdish_agent.models import PendingAction, RunState, TurnRequest
from tests.test_agent_storage_unit import ROOT, pg, stores  # noqa: F401


@pytest.fixture(scope='module')
def save_pg(pg):
    for name in ('enforce_agent_daily_save_policy', 'enforce_agent_profile_safety', 'add_agent_controlled_save'):
        pg.sql((ROOT / f'migrations/{name}.sql').read_text())
    return pg


@pytest.fixture
def store(save_pg, stores):
    return stores[0]


@pytest.mark.parametrize('text,intent', [
    ('Save a high-protein dinner at Busch', 'save'), ('please generate and save dinner for tonight', 'save'),
    ('Replace my dinner with something spicy', 'replace'), ('Overwrite my lunch', None),
    ('Should I save this?', None), ("Don't save, just show me dinner", None), ('Save it without asking', None),
    ('What would you save?', None), ('Find dinner and maybe save it', None), ('Saved plans', None),
    ('Save dinner?!', None), ('Save dinner!?', None), ('Save dinner？', None), ('Save dinner?.', None),
    ('Save dinner?\u200b\ufeff\u3000', None), ('Save dinner, don‘t wait', None), ('Save dinner, donʼt', None),
    ('Save dinner, don`t', None), ('Save dinner, don′t', None), ('\u200bSave dinner!\u00a0', 'save'),
    ('Save dinner? 🙂', None), ('Save dinner? !', None), ('Save dinner?)', None), ('Save dinner?"', None),
    ('Save dinner?”', None), ('Save dinner?…', None), ('Save dinner?\ufe0f', None), ('Save dinner?\u2060', None),
    ('Save dinner‽', None), ('Save dinner⁇', None), ('Save dinner﹖', None), ('Save dinner؟', None),
    ('Replace my dinner? 🙂', None), ('Save, don´t', None), ('Save, don＇t', None), ("Save, do\u200bn't", None),
    ('Save dinner', 'save'), ('Save dinner.', 'save'), ('Save dinner 🙂', 'save'), ('Save dinner. Thanks!', 'save'),
    ("Save dinner, it's great", 'save'), ('Save dinner, donated', 'save'), ('Save donuts for dinner', 'save'),
    ('\ufeff\u200dSave dinner', 'save'), ('Save a donut', 'save'), ('Save donuts for breakfast', 'save'),
    ("Save dinner, don't", None), ('Save dinner, don’t', None), ('Save dinner, dont', None)])
def test_intent_is_strict(save_pg, text, intent):
    value = save_pg.sql("SET ROLE service_role; SELECT agent_save_intent('" + text.replace("'", "''") + "', 'dinner')")
    assert (value or None) == intent


def turn(store, text, target=None):
    conversation = store.create_conversation()
    request = TurnRequest(request_id=uuid4(), text=text,
                          context={'dining_hall': 'busch', 'date': '2026-10-10', 'meal_type': 'dinner'})
    _, run = store.claim_turn(conversation.id, request)
    data = json.loads((ROOT / 'contracts/agent-v1.json').read_text())['conversation']['pending_actions'][0]
    data.update(id=str(uuid4()), expires_at=(datetime.now(timezone.utc) + timedelta(hours=23)).isoformat())
    action = PendingAction.model_validate(data)
    events = [{'type': 'text_delta', 'data': {'text': 'Chili'}}, {'type': 'action_required', 'data': {'action': action}},
              {'type': 'complete', 'data': {}}]
    state = RunState(id=run.id, conversation_id=run.conversation_id, status='complete', events=[
        {**event, 'sequence': n, 'run_id': str(run.id)} for n, event in enumerate(events, 1)])
    stored = StoredAction(action, {'meal': 'new'}, UUID(target['plan']['id']) if target else None,
                          target['version'] if target else None, store.get_profile()['version'])
    return state, stored


def plans(pg, store):
    return pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{store.user_id}'")


def test_save_then_exact_replace_are_atomic(store, save_pg):
    state, action = turn(store, 'Save a spicy dinner')
    result = store.save_turn(state, [], 'Chili', action)
    assert [e.type for e in result.events] == ['text_delta', 'complete'] and result.events[-1].sequence == 2
    saved = result.events[-1].data.saved_plan_id
    assert store.get_saved_plan(saved)['plan']['meal_plan_data'] == {'meal': 'new'}
    assert store.get_run(state.id) == result
    with pytest.raises(StorageError, match='conflict'):
        store.save_turn(state, [], 'Chili', action)
    target = store.get_saved_plan(saved)
    state, action = turn(store, 'Save another dinner', target)
    assert store.save_turn(state, [], '', action).events[-2].type == 'action_required'
    state, action = turn(store, 'Replace my dinner', target)
    assert store.save_turn(state, [], '', replace(action, save_payload={'meal': 'replaced'})).events[-1].data.saved_plan_id == saved
    assert store.get_saved_plan(saved)['plan']['meal_plan_data'] == {'meal': 'replaced'}
    assert plans(save_pg, store) == '1'


def test_guard_failure_rolls_back_everything(store, save_pg):
    state, action = turn(store, 'Save dinner')
    save_pg.sql(f"INSERT INTO saved_meal_plans VALUES ('{uuid4()}', '{store.user_id}', 'Busch', '2026-10-10', '{{}}', now(), now())")
    with pytest.raises(StorageError, match='stale_plan'):
        store.save_turn(state, [], '', action)
    assert store.get_run(state.id).status == 'running'
    assert save_pg.sql(f"SELECT count(*) FROM agent_actions WHERE id = '{action.action.id}'") == '0'
    assert plans(save_pg, store) == '1'


@pytest.mark.parametrize('flag,failure', [('false', None), ('true', 'stale_plan'), ('true', 'conflict'), ('true', 'unavailable')])
def test_route_gates_save_and_never_retries_it(monkeypatch, flag, failure):
    import asyncio
    from types import SimpleNamespace
    from dormdish_agent import routes
    data = json.loads((ROOT / 'contracts/agent-v1.json').read_text())['conversation']['pending_actions'][0]
    action = StoredAction(PendingAction.model_validate(data), {}, expected_profile_version='0' * 64)
    calls = []
    class Turn:
        def __init__(self, context, request, cid, history, **kw):
            self.state = RunState(id=request.request_id, conversation_id=cid, status='running')
            self.history, self.assistant_text = [], 'Chili'
        async def stream(self):
            yield routes.append_event(self.state, 'complete', {'citations': []})
    class Store:
        def finish_turn(self, state, history, text, action=None):
            calls.append(('finish', state.model_copy(deep=True)))
            return state
        def save_turn(self, state, *args):
            calls.append(('save', state))
            raise routes.StorageError(failure)
        def get_run(self, request_id):
            calls.append(('get_run', None))
            return recovered
    class Client:
        def __init__(self, **kw): pass
        async def close(self): pass
    monkeypatch.setenv('DORMDISH_CONTROLLED_SAVE_ENABLED', flag)
    monkeypatch.setattr(routes, 'AsyncOpenAI', Client)
    monkeypatch.setattr(routes, 'AgentTurn', Turn)
    monkeypatch.setattr(routes, 'stored_action', lambda *args: action)
    state = RunState(id=uuid4(), conversation_id=uuid4(), status='running')
    recovered = RunState(id=state.id, conversation_id=state.conversation_id, status='complete', events=[
        {'type': 'complete', 'sequence': 1, 'run_id': str(state.id), 'data': {'saved_plan_id': str(uuid4())}}])
    async def run():
        return [frame async for frame in routes.run_stream(Store(), state, TurnRequest(request_id=state.id, text='Save dinner'),
                                                           SimpleNamespace(proposal=object()), [], None)]
    frames = asyncio.run(run())
    kinds = [kind for kind, _ in calls]
    if failure is None:
        assert kinds == ['finish'] and b'action_required' in frames[0]
    elif failure != 'unavailable':
        assert kinds == ['save', 'finish'] and calls[1][1].events[-1].data.code == failure
        assert len(frames) == 1 and f'"{failure}"'.encode() in frames[0] and b'"retryable":true' in frames[0]
    else:
        assert kinds == ['save', 'get_run'] and frames == [routes.frame(recovered.events[0])]


def test_no_intent_keeps_preview(store, save_pg):
    state, action = turn(store, 'Find dinner and maybe save it')
    assert store.save_turn(state, [], '', action) == store.get_run(state.id)
    assert store.get_run(state.id).events[-2].type == 'action_required'
    assert plans(save_pg, store) == '0'
