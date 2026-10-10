"""RPC boundary checks and opt-in real PostgreSQL storage selfchecks.

DD_AGENT_LOCAL_PG=1 python -m pytest tests/test_agent_storage_unit.py
uses only the local dd-agent-migration-db Docker container and a throwaway DB.
"""
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from dormdish_agent.conversations import AgentStore, StorageError, StoredAction
from dormdish_agent.models import ConfirmActionRequest, PendingAction, RunState, TurnRequest

ROOT = Path(__file__).resolve().parents[1]


class RecordingClient:
    def rpc(self, name, params):
        self.call = name, params
        return self

    def execute(self):
        return SimpleNamespace(data=[])


def test_every_call_uses_authenticated_owner():
    client = RecordingClient()
    owner = uuid4()
    AgentStore(client, owner).load_history(uuid4())
    assert client.call[1]['p_user_id'] == str(owner)
    with pytest.raises(ValueError):
        AgentStore(client, 'not-an-id')


def test_storage_errors_do_not_expose_sql_or_private_details():
    class FailedClient(RecordingClient):
        def execute(self):
            raise RuntimeError('private user SQL credentials')
    with pytest.raises(StorageError, match='^unavailable$'):
        AgentStore(FailedClient(), uuid4()).load_history(uuid4())


def test_replacement_requires_server_snapshot():
    fixture = json.loads((ROOT / 'contracts/agent-v1.json').read_text())
    action = PendingAction.model_validate(fixture['conversation']['pending_actions'][0])
    with pytest.raises(ValueError, match='Replacement requires'):
        StoredAction(action, {}, uuid4()).payload()


class LocalPG:
    def __init__(self, database):
        self.database = database

    def sql(self, sql):
        result = subprocess.run(['docker', 'exec', '-i', 'dd-agent-migration-db', 'psql',
            '-U', 'postgres', '-d', self.database, '-v', 'ON_ERROR_STOP=1', '-Atq'],
            input=sql, capture_output=True, text=True)
        if result.returncode:
            error = RuntimeError(result.stderr)
            match = re.search(r'ERROR:\s+(agent_\w+)', result.stderr)
            error.message = match[1] if match else 'database error'
            raise error
        return result.stdout.strip()

    def rpc(self, name, params):
        def literal(value):
            if value is None:
                return 'NULL'
            if isinstance(value, bool):
                return str(value)
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            return "'" + str(value).replace("'", "''") + "'"
        query = f"SET ROLE service_role; SELECT {name}(" + ', '.join(f'{key} => {literal(value)}' for key, value in params.items()) + ');'
        def execute():
            data = self.sql(query)
            return SimpleNamespace(data=json.loads(data) if data else None)
        return SimpleNamespace(execute=execute)


@pytest.fixture(scope='module')
def pg():
    if os.getenv('DD_AGENT_LOCAL_PG') != '1':
        pytest.skip('opt-in local PostgreSQL selfcheck')
    name = 'dd_agent_selfcheck_' + uuid4().hex
    admin = LocalPG('postgres')
    admin.sql('''DO $$ BEGIN
        IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon; END IF;
        IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated; END IF;
        IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN CREATE ROLE service_role BYPASSRLS; END IF;
        END $$;''')
    admin.sql(f'CREATE DATABASE {name}')
    database = LocalPG(name)
    try:
        database.sql('''CREATE TABLE users(id uuid PRIMARY KEY);
            CREATE TABLE saved_meal_plans(id uuid PRIMARY KEY, user_id uuid REFERENCES users(id), dining_hall text,
                date date, meal_plan_data jsonb, created_at timestamptz, updated_at timestamptz);
            GRANT SELECT, UPDATE ON users TO service_role;
            GRANT SELECT, INSERT, UPDATE ON saved_meal_plans TO service_role;''')
        database.sql((ROOT / 'migrations/add_agent_persistence.sql').read_text())
        yield database
    finally:
        admin.sql(f'DROP DATABASE {name} WITH (FORCE)')


@pytest.fixture
def stores(pg):
    owners = [uuid4(), uuid4()]
    for owner in owners:
        pg.sql(f"INSERT INTO users VALUES ('{owner}')")
    return [AgentStore(pg, owner) for owner in owners]


def terminal(run):
    return RunState(id=run.id, conversation_id=run.conversation_id, status='complete', events=[{
        'type': 'complete', 'sequence': 1, 'run_id': str(run.id), 'data': {}}])


def test_pg_owner_duplicate_and_atomic_history(stores):
    store, other = stores
    conversation = store.create_conversation()
    request = TurnRequest(request_id=uuid4(), text='Dinner 🌶️')
    claimed, run = store.claim_turn(conversation.id, request)
    assert claimed
    assert not store.claim_turn(conversation.id, request)[0]
    with pytest.raises(StorageError, match='conflict'):
        store.claim_turn(conversation.id, request.model_copy(update={'text': 'Changed'}))
    with pytest.raises(StorageError, match='busy'):
        store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Another turn'))
    for call in [lambda: other.get_conversation(conversation.id), lambda: other.get_run(run.id),
                 lambda: other.load_history(conversation.id), lambda: other.delete_conversation(conversation.id)]:
        with pytest.raises(StorageError, match='not_found'):
            call()
    complete = terminal(run)
    history = [{'role': 'user', 'content': request.text}]
    assert store.finish_turn(complete, history, 'Here is dinner').status == 'complete'
    assert store.load_history(conversation.id) == history
    assert [m.text for m in store.get_conversation(conversation.id).messages] == ['Dinner 🌶️', 'Here is dinner']
    assert store.finish_turn(complete, history, 'Duplicate response') == complete
    assert len(store.get_conversation(conversation.id).messages) == 2


def test_pg_confirmation_is_exact_atomic_and_idempotent(stores, pg):
    store, other = stores
    payload = {'meal': {'name': 'Trusted chili', 'calories': 300}}
    conversation, action = prepared_action(store, payload=payload)
    request = ConfirmActionRequest(request_id=uuid4(), proposal_version=1)
    with pytest.raises(StorageError, match='not_found'):
        other.confirm_action(action.id, request)
    result = store.confirm_action(action.id, request)
    assert store.confirm_action(action.id, request) == result
    saved_id = result.events[-1].data.saved_plan_id
    repeated = store.confirm_action(action.id, request.model_copy(update={'request_id': uuid4()}))
    assert repeated.events[-1].data.saved_plan_id == saved_id
    assert store.get_saved_plan(saved_id)['plan']['meal_plan_data'] == payload
    assert pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{store.user_id}'") == '1'
    assert not store.get_conversation(conversation.id).pending_actions


def prepared_action(store, *, target=None, payload=None):
    from datetime import datetime, timedelta, timezone
    conversation = store.create_conversation()
    _, run = store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Plan dinner'))
    data = json.loads((ROOT / 'contracts/agent-v1.json').read_text())['conversation']['pending_actions'][0]
    data.update(id=str(uuid4()), expires_at=(datetime.now(timezone.utc) + timedelta(hours=23)).isoformat())
    action = PendingAction.model_validate(data)
    complete = terminal(run)
    complete = RunState(id=run.id, conversation_id=run.conversation_id, status='complete', events=[
        {'type': 'action_required', 'sequence': 1, 'run_id': str(run.id), 'data': {'action': action}},
        complete.events[0].model_copy(update={'sequence': 2})])
    stored = StoredAction(action, payload or {'meal': {'name': 'Trusted chili'}},
        UUID(target['plan']['id']) if target else None, target['version'] if target else None)
    store.finish_turn(complete, [], 'Preview', stored)
    return conversation, action


def confirmation():
    return ConfirmActionRequest(request_id=uuid4(), proposal_version=1)


def test_pg_stale_replacement_and_expected_absence(stores, pg):
    store, _ = stores
    _, original = prepared_action(store)
    saved = store.confirm_action(original.id, confirmation()).events[-1].data.saved_plan_id
    snapshot = store.get_saved_plan(saved)
    _, replacement = prepared_action(store, target=snapshot, payload={'meal': {'name': 'Replacement'}})
    # Existing legacy writes can omit updated_at; the full-row version still changes.
    pg.sql(f"UPDATE saved_meal_plans SET meal_plan_data = '{{\"legacy\":true}}' WHERE id = '{saved}'")
    with pytest.raises(StorageError, match='stale_plan'):
        store.confirm_action(replacement.id, confirmation())
    assert store.get_saved_plan(saved)['plan']['meal_plan_data'] == {'legacy': True}
    _, current = prepared_action(store, target=store.get_saved_plan(saved), payload={'meal': {'name': 'Current'}})
    assert store.confirm_action(current.id, confirmation()).events[-1].data.saved_plan_id == saved
    assert store.get_saved_plan(saved)['plan']['meal_plan_data'] == {'meal': {'name': 'Current'}}
    _, create_another = prepared_action(store)
    with pytest.raises(StorageError, match='stale_plan'):
        store.confirm_action(create_another.id, confirmation())


def test_pg_expiration_and_late_worker(stores, pg):
    store, _ = stores
    conversation = store.create_conversation()
    _, run = store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Dinner'))
    pg.sql(f"UPDATE agent_runs SET deadline = now() - interval '1 minute' WHERE user_id = '{store.user_id}'")
    expired = store.get_run(run.id)
    assert expired.status == 'error'
    assert expired.events[-1].data.retryable is False
    with pytest.raises(StorageError, match='conflict'):
        store.finish_turn(terminal(run), [], 'Late worker')
    _, action = prepared_action(store)
    pg.sql(f"UPDATE agent_actions SET expires_at = now() - interval '1 minute' WHERE id = '{action.id}'")
    with pytest.raises(StorageError, match='expired'):
        store.confirm_action(action.id, confirmation())
    assert pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{store.user_id}'") == '0'


def test_pg_forgetting_inflight_and_history_bounds(stores):
    store, _ = stores
    conversation = store.create_conversation()
    _, run = store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Forget mushrooms'))
    store.clear_history()
    store.finish_turn(terminal(run), [{'role': 'user', 'content': 'Remember mushrooms'}], 'Forgotten')
    assert store.load_history(conversation.id) == []
    _, run = store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='New turn'))
    store.finish_turn(terminal(run), [{'role': 'user', 'content': 'item'}] * 129, 'New response')
    assert store.load_history(conversation.id) == []
    assert len(store.get_conversation(conversation.id).messages) == 4


def test_pg_failed_finish_rolls_back_every_write(stores):
    store, _ = stores
    conversation = store.create_conversation()
    _, run = store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Preview'))
    # An action_required event without the exact matching server-held action cannot finish.
    data = json.loads((ROOT / 'contracts/agent-v1.json').read_text())['conversation']['pending_actions'][0]
    incomplete = RunState(id=run.id, conversation_id=run.conversation_id, status='complete', events=[
        {'type': 'action_required', 'sequence': 1, 'run_id': str(run.id), 'data': {'action': data}},
        terminal(run).events[0].model_copy(update={'sequence': 2})])
    with pytest.raises(StorageError, match='invalid'):
        store.finish_turn(incomplete, [{'role': 'user', 'content': 'unsaved'}], 'Unsaved')
    assert store.get_run(run.id).status == 'running'
    assert store.load_history(conversation.id) == []
    assert store.get_conversation(conversation.id).messages == []
    assert store.get_conversation(conversation.id).pending_actions == []


def test_pg_retention_account_deletion_and_role_denial(stores, pg):
    store, other = stores
    conversation, action = prepared_action(store)
    for role in ['anon', 'authenticated']:
        for statement in ['SELECT * FROM agent_conversations', f"SELECT agent_get_conversation('{store.user_id}', '{conversation.id}')"]:
            with pytest.raises(RuntimeError, match='permission denied'):
                pg.sql(f'SET ROLE {role}; {statement}')
    pg.sql(f"UPDATE agent_conversations SET expires_at = now() - interval '1 day' WHERE id = '{conversation.id}'")
    with pytest.raises(StorageError, match='not_found'):
        store.get_conversation(conversation.id)
    pg.sql('SET ROLE service_role; SELECT agent_purge_expired()')
    assert pg.sql(f"SELECT count(*) FROM agent_runs WHERE user_id = '{store.user_id}'") == '0'
    assert pg.sql(f"SELECT count(*) FROM agent_actions WHERE id = '{action.id}'") == '0'
    prepared_action(other)
    pg.sql(f"DELETE FROM users WHERE id = '{other.user_id}'")
    for table in ['agent_conversations', 'agent_runs', 'agent_actions', 'agent_quotas']:
        assert pg.sql(f"SELECT count(*) FROM {table} WHERE user_id = '{other.user_id}'") == '0'


def test_pg_quota_survives_conversation_deletion(stores, pg):
    store, _ = stores
    conversation = store.create_conversation()
    _, run = store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Dinner'))
    store.delete_conversation(conversation.id)
    pg.sql(f"UPDATE agent_quotas SET requests = 60 WHERE user_id = '{store.user_id}'")
    conversation = store.create_conversation()
    with pytest.raises(StorageError, match='quota'):
        store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Retry'))
    assert pg.sql(f"SELECT requests FROM agent_quotas WHERE user_id = '{store.user_id}'") == '60'


def test_pg_parallel_claims_and_confirmations(stores):
    from concurrent.futures import ThreadPoolExecutor
    store, _ = stores
    conversation = store.create_conversation()
    request = TurnRequest(request_id=uuid4(), text='Dinner')
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: store.claim_turn(conversation.id, request), range(2)))
    assert sorted(claimed for claimed, _ in results) == [False, True]
    store.finish_turn(terminal(results[0][1]), [], 'Done')
    _, action = prepared_action(store)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: store.confirm_action(action.id, confirmation()), range(2)))
    assert results[0].events[-1].data.saved_plan_id == results[1].events[-1].data.saved_plan_id


def test_pg_confirmation_waits_for_legacy_insert(stores, pg):
    import time
    from concurrent.futures import ThreadPoolExecutor
    store, _ = stores
    _, action = prepared_action(store)
    # Take the same ROW EXCLUSIVE table lock as an ordinary legacy insert, then hold
    # the uncommitted row until confirm is waiting. No unique index is assumed.
    with ThreadPoolExecutor(2) as pool:
        legacy = pool.submit(pg.sql, f"""BEGIN;
            INSERT INTO saved_meal_plans(id, user_id, dining_hall, date, meal_plan_data)
            VALUES('{uuid4()}', '{store.user_id}', 'Busch', '2026-10-10', '{{"legacy":true}}');
            SELECT pg_sleep(1); COMMIT;""")
        for _ in range(30):
            if pg.sql("SELECT count(*) FROM pg_locks WHERE relation = 'saved_meal_plans'::regclass AND mode = 'RowExclusiveLock'") != '0':
                break
            time.sleep(.02)
        else:
            pytest.fail('legacy write did not acquire its lock')
        pending = pool.submit(store.confirm_action, action.id, confirmation())
        legacy.result()
        with pytest.raises(StorageError, match='stale_plan'):
            pending.result()
    assert pg.sql(f"SELECT count(*) FROM saved_meal_plans WHERE user_id = '{store.user_id}'") == '1'
