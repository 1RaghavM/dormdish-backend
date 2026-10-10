"""Independent transaction regressions; opt in with DD_AGENT_LOCAL_PG=1.

Reuse the local Docker transport/fixture, not mocked persistence. Pytest creates
this module its own synthetic database and drops only that database afterwards.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from dormdish_agent.conversations import StorageError
from dormdish_agent.models import ConfirmActionRequest, TurnRequest
from tests.test_agent_storage_unit import (
    pg, stores, terminal, prepared_action, confirmation,
)


def parallel(*calls):
    barrier = Barrier(len(calls))

    def invoke(call):
        barrier.wait(timeout=10)
        try:
            return call()
        except StorageError as error:
            return error.code

    with ThreadPoolExecutor(len(calls)) as pool:
        return list(pool.map(invoke, calls))


def begin(store, conversation=None, request=None):
    conversation = conversation or store.create_conversation()
    request = request or TurnRequest(request_id=uuid4(), text='Synthetic dinner')
    return conversation, store.claim_turn(conversation.id, request)[1]


def count(pg, table, owner):
    return int(pg.sql(f"SELECT count(*) FROM {table} WHERE user_id = '{owner}'"))


def test_distinct_concurrent_turns_have_one_winner_and_charge_once(stores, pg):
    store, _ = stores
    conversation = store.create_conversation()
    results = parallel(*(lambda: begin(store, conversation) for _ in range(4)))
    assert results.count('busy') == 3
    assert count(pg, 'agent_runs', store.user_id) == 1
    assert pg.sql(f"SELECT requests FROM agent_quotas WHERE user_id = '{store.user_id}'") == '1'


def test_last_quota_slot_is_atomic_across_conversations(stores, pg):
    store, _ = stores
    conversations = [store.create_conversation() for _ in range(2)]
    pg.sql(f"INSERT INTO agent_quotas(user_id, requests) VALUES ('{store.user_id}', 59)")
    results = parallel(*(lambda c=c: begin(store, c) for c in conversations))
    assert results.count('quota') == 1
    assert count(pg, 'agent_runs', store.user_id) == 1
    assert pg.sql(f"SELECT requests FROM agent_quotas WHERE user_id = '{store.user_id}'") == '60'
    pg.sql(f"UPDATE agent_quotas SET window_start = now() - interval '61 minutes' WHERE user_id = '{store.user_id}'")
    available = next(c for c in conversations if store.get_conversation(c.id).active_run_id is None)
    begin(store, available)
    assert pg.sql(f"SELECT requests FROM agent_quotas WHERE user_id = '{store.user_id}'") == '1'


def test_last_conversation_slot_is_atomic(stores, pg):
    store, _ = stores
    for _ in range(19):
        store.create_conversation()
    results = parallel(store.create_conversation, store.create_conversation)
    assert results.count('quota') == 1
    assert count(pg, 'agent_conversations', store.user_id) == 20


def test_request_ids_are_owner_scoped_but_cannot_move_conversations(stores):
    first, second = stores
    request = TurnRequest(request_id=uuid4(), text='Identical synthetic input')
    conversation, run = begin(first, request=request)
    other_conversation, other_run = begin(second, request=request)
    assert run.id == other_run.id
    assert run.conversation_id != other_conversation.id
    with pytest.raises(StorageError, match='^conflict$'):
        first.claim_turn(first.create_conversation().id, request)
    with pytest.raises(StorageError, match='^not_found$'):
        second.finish_turn(terminal(run), [], 'Do not write')
    assert second.get_run(request.request_id).conversation_id == other_conversation.id
    assert first.get_conversation(conversation.id).messages == []


def test_confirmation_request_cannot_be_reused_for_another_action_or_version(stores):
    store, _ = stores
    _, first = prepared_action(store)
    _, second = prepared_action(store)
    request = confirmation()
    result = store.confirm_action(first.id, request)
    for action_id, version in [(second.id, 1), (first.id, 2)]:
        with pytest.raises(StorageError, match='^conflict$'):
            store.confirm_action(action_id, ConfirmActionRequest(request_id=request.request_id, proposal_version=version))
    assert store.get_run(request.request_id) == result


def test_confirmation_cannot_reuse_a_generation_request_id(stores, pg):
    store, _ = stores
    conversation, action = prepared_action(store)
    request_id = pg.sql(f"SELECT id FROM agent_runs WHERE conversation_id = '{conversation.id}'")
    with pytest.raises(StorageError, match='^conflict$'):
        store.confirm_action(action.id, ConfirmActionRequest(request_id=request_id, proposal_version=1))
    assert count(pg, 'saved_meal_plans', store.user_id) == 0


def test_competing_proposals_create_only_one_plan(stores, pg):
    store, _ = stores
    actions = [prepared_action(store)[1] for _ in range(2)]
    results = parallel(*(lambda a=a: store.confirm_action(a.id, confirmation()) for a in actions))
    assert results.count('stale_plan') == 1
    assert count(pg, 'saved_meal_plans', store.user_id) == 1
    assert pg.sql(f"SELECT count(*) FROM agent_actions WHERE user_id = '{store.user_id}' AND applied_plan_id IS NOT NULL") == '1'


def test_competing_replacements_use_compare_and_swap(stores):
    store, _ = stores
    _, initial = prepared_action(store)
    saved = store.confirm_action(initial.id, confirmation()).events[-1].data.saved_plan_id
    snapshot = store.get_saved_plan(saved)
    proposals = [prepared_action(store, target=snapshot, payload={'winner': index})[1] for index in range(2)]
    results = parallel(*(lambda a=a: store.confirm_action(a.id, confirmation()) for a in proposals))
    assert results.count('stale_plan') == 1
    winner = next(index for index, result in enumerate(results) if result != 'stale_plan')
    assert store.get_saved_plan(saved)['plan']['meal_plan_data'] == {'winner': winner}


def test_cross_owner_saved_plan_cannot_be_read_or_used_for_replacement(stores, pg):
    store, other = stores
    _, action = prepared_action(store)
    saved = store.confirm_action(action.id, confirmation()).events[-1].data.saved_plan_id
    with pytest.raises(StorageError, match='^not_found$'):
        other.get_saved_plan(saved)
    with pytest.raises(StorageError, match='^not_found$'):
        prepared_action(other, target=store.get_saved_plan(saved))
    assert count(pg, 'agent_actions', other.user_id) == 0
    assert count(pg, 'saved_meal_plans', other.user_id) == 0


def test_failed_confirmation_rolls_back_meal_receipt_quota_and_action(stores, pg):
    store, _ = stores
    conversation, action = prepared_action(store)
    request = confirmation()
    pg.sql(f"""CREATE FUNCTION reject_test_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.id = '{request.request_id}' THEN RAISE EXCEPTION 'synthetic storage failure'; END IF;
        RETURN NEW; END $$;
        CREATE TRIGGER reject_test_receipt BEFORE INSERT ON agent_runs
        FOR EACH ROW EXECUTE FUNCTION reject_test_receipt();""")
    try:
        with pytest.raises(StorageError, match='^unavailable$'):
            store.confirm_action(action.id, request)
        assert count(pg, 'saved_meal_plans', store.user_id) == 0
        assert len(store.get_conversation(conversation.id).pending_actions) == 1
        assert pg.sql(f"SELECT requests FROM agent_quotas WHERE user_id = '{store.user_id}'") == '1'
        with pytest.raises(StorageError, match='^not_found$'):
            store.get_run(request.request_id)
    finally:
        pg.sql('DROP TRIGGER reject_test_receipt ON agent_runs; DROP FUNCTION reject_test_receipt()')
    result = store.confirm_action(action.id, request)
    assert store.get_run(request.request_id) == result
    assert count(pg, 'saved_meal_plans', store.user_id) == 1


def test_ambiguous_confirmation_response_reconciles_without_another_write(stores, pg):
    store, _ = stores
    _, action = prepared_action(store)
    request = confirmation()
    # Simulate losing the response after the database has committed.
    store.confirm_action(action.id, request)
    recovered = store.get_run(request.request_id)
    assert recovered.status == 'complete'
    assert store.confirm_action(action.id, request) == recovered
    assert count(pg, 'saved_meal_plans', store.user_id) == 1
    assert pg.sql(f"SELECT requests FROM agent_quotas WHERE user_id = '{store.user_id}'") == '2'


def test_forgetting_clears_every_owned_replay_and_prevents_inflight_resurrection(stores):
    store, other = stores
    old = [{'role': 'user', 'content': 'Remember a synthetic preference'}]
    conversations = [begin(store) for _ in range(2)]
    foreign, foreign_run = begin(other)
    other.finish_turn(terminal(foreign_run), old, 'Retained')
    store.finish_turn(terminal(conversations[0][1]), old, 'Stored')
    store.clear_history()
    store.finish_turn(terminal(conversations[1][1]), old, 'Late response')
    assert all(store.load_history(c.id) == [] for c, _ in conversations)
    assert other.load_history(foreign.id) == old
    conversation, run = begin(store, conversations[0][0])
    new = [{'role': 'user', 'content': 'New explicit instruction'}]
    store.finish_turn(terminal(run), new, 'Fresh')
    assert store.load_history(conversation.id) == new


@pytest.mark.parametrize('history,retained', [
    ([{'role': 'user', 'content': 'x'}] * 128, True),
    ([{'role': 'user', 'content': 'x'}] * 129, False),
    ([{'type': 'function_call', 'call_id': 'c', 'name': 'menu', 'arguments': '{}'},
      {'type': 'function_call_output', 'call_id': 'c', 'output': '🌶' * 70000}], False),
])
def test_replay_bounds_keep_complete_arrays_or_clear_whole_tool_pairs(stores, history, retained):
    store, _ = stores
    conversation, run = begin(store)
    store.finish_turn(terminal(run), history, 'Visible response remains')
    assert store.load_history(conversation.id) == (history if retained else [])
    assert store.get_conversation(conversation.id).messages[-1].text == 'Visible response remains'


def test_expired_run_duplicate_reconciles_then_new_turn_can_start(stores, pg):
    store, _ = stores
    request = TurnRequest(request_id=uuid4(), text='Synthetic dinner')
    conversation, run = begin(store, request=request)
    pg.sql(f"UPDATE agent_runs SET deadline = now() - interval '1 second' WHERE user_id = '{store.user_id}'")
    claimed, expired = store.claim_turn(conversation.id, request)
    assert not claimed and expired.status == 'error'
    assert not expired.events[-1].data.retryable
    assert store.get_conversation(conversation.id).active_run_id is None
    _, new_run = begin(store, conversation)
    with pytest.raises(StorageError, match='^conflict$'):
        store.finish_turn(terminal(run), [], 'Late')
    assert store.get_run(new_run.id).status == 'running'


def test_deleted_conversation_removes_receipts_but_keeps_confirmed_meal(stores, pg):
    store, _ = stores
    conversation, action = prepared_action(store)
    request = confirmation()
    saved = store.confirm_action(action.id, request).events[-1].data.saved_plan_id
    store.delete_conversation(conversation.id)
    assert count(pg, 'agent_actions', store.user_id) == 0
    assert count(pg, 'agent_runs', store.user_id) == 0
    assert store.get_saved_plan(saved)['plan']['id'] == str(saved)
    with pytest.raises(StorageError, match='^not_found$'):
        store.confirm_action(action.id, request)


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
def test_unprivileged_roles_cannot_read_write_or_call_any_agent_rpc(pg, role):
    tables = ['agent_quotas', 'agent_conversations', 'agent_runs', 'agent_actions']
    for table in tables:
        for statement in [f'SELECT * FROM {table}', f'DELETE FROM {table}']:
            with pytest.raises(RuntimeError, match='permission denied'):
                pg.sql(f'SET ROLE {role}; {statement}')
        assert pg.sql(f"SELECT relrowsecurity FROM pg_class WHERE oid = '{table}'::regclass") == 't'
    # pg_proc enumerates every overload; PUBLIC's implicit execution grant counts too.
    assert pg.sql(f"SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'agent_%' AND has_function_privilege('{role}', oid, 'EXECUTE')") == '0'
    assert pg.sql("SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'agent_%' AND prosecdef") == '0'
