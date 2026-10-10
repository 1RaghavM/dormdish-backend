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
    from datetime import datetime, timedelta, timezone
    store, other = stores
    conversation = store.create_conversation()
    _, run = store.claim_turn(conversation.id, TurnRequest(request_id=uuid4(), text='Plan dinner'))
    action_data = json.loads((ROOT / 'contracts/agent-v1.json').read_text())['conversation']['pending_actions'][0]
    action_data.update(id=str(uuid4()), expires_at=(datetime.now(timezone.utc) + timedelta(hours=23)).isoformat())
    action = PendingAction.model_validate(action_data)
    payload = {'meal': {'name': 'Trusted chili', 'calories': 300}}
    complete = terminal(run)
    complete = RunState(id=run.id, conversation_id=run.conversation_id, status='complete', events=[
        {'type': 'action_required', 'sequence': 1, 'run_id': str(run.id), 'data': {'action': action}},
        complete.events[0].model_copy(update={'sequence': 2})])
    store.finish_turn(complete, [], 'Preview', StoredAction(action, payload))
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
