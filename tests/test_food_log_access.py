"""Independent local SQL and authenticated HTTP compatibility checks for PR #21."""
import importlib.util
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from tests.test_agent_storage_unit import pg

ROOT = Path(__file__).resolve().parents[1]
DAY = '2026-10-11'


def literal(value):
    return 'NULL' if value is None else "'" + str(value).replace("'", "''") + "'"


@pytest.fixture(scope='module')
def schema(pg):
    # Reconstruct the observed PUBLIC policy and client grants, with API-used columns.
    pg.sql('''CREATE TABLE food_log (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL,
        date text NOT NULL, meal_type text NOT NULL, name text NOT NULL,
        barcode text, brand text, serving_size text, servings double precision DEFAULT 1.0,
        calories double precision DEFAULT 0, protein_g double precision DEFAULT 0, carbs_g double precision,
        fat_g double precision, fiber_g double precision, sugar_g double precision, image_url text,
        created_at timestamptz DEFAULT now());
        ALTER TABLE food_log ENABLE ROW LEVEL SECURITY;
        CREATE POLICY "Allow service role full access" ON food_log FOR ALL USING (true);
        GRANT SELECT ON food_log TO anon, authenticated;
        GRANT ALL ON food_log TO service_role;
        CREATE TABLE user_sessions(user_id uuid REFERENCES users(id), session_token text,
            expires_at timestamptz);
        GRANT SELECT ON user_sessions TO service_role;''')
    owner, entry = str(uuid4()), str(uuid4())
    pg.sql(f"INSERT INTO users VALUES ('{owner}'); INSERT INTO food_log (id,user_id,date,meal_type,name,calories) VALUES ('{entry}','{owner}','{DAY}','dinner','Existing meal',321)")
    before = pg.sql('SELECT jsonb_agg(to_jsonb(f)) FROM food_log f')
    for role in ('anon', 'authenticated'):
        assert pg.sql(f'SET ROLE {role}; SELECT id FROM food_log') == entry
    # An external no-op file replays these unchanged assertions against the old policy.
    migration = Path(os.environ.get('DD_FOOD_LOG_MIGRATION', ROOT / 'migrations/secure_food_log_access.sql')).read_text()
    pg.sql(migration)
    pg.sql(migration)
    return before, pg.sql('SELECT jsonb_agg(to_jsonb(f)) FROM food_log f')


@pytest.fixture
def rows(pg, schema):
    pg.sql('TRUNCATE food_log, user_sessions')
    owner, other = str(uuid4()), str(uuid4())
    pg.sql(f"INSERT INTO users VALUES ('{owner}'), ('{other}'); INSERT INTO user_sessions VALUES ('{owner}','owner-session',now()+interval '1 day'), ('{other}','other-session',now()+interval '1 day'), ('{owner}','expired-session',now()-interval '1 day')")
    entries = [str(uuid4()) for _ in range(3)]
    for entry, user, day in zip(entries, [owner, other, owner], [DAY, DAY, '2026-10-10']):
        pg.sql(f"INSERT INTO food_log(id,user_id,date,meal_type,name,calories) VALUES ('{entry}','{user}','{day}','dinner','Stored food',123)")
    return SimpleNamespace(owner=owner, other=other, entries=entries)


def test_existing_rows_survive_repeated_migration(pg, schema):
    assert schema[0] == schema[1]
    assert pg.sql("SELECT relrowsecurity FROM pg_class WHERE oid='food_log'::regclass") == 't'
    assert pg.sql("SELECT count(*) FROM pg_policies WHERE tablename='food_log' AND policyname='Allow service role full access'") == '0'


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
@pytest.mark.parametrize('operation', ['SELECT', 'INSERT', 'UPDATE', 'DELETE'])
def test_direct_client_crud_is_denied(pg, rows, role, operation):
    queries = {
        'SELECT': 'SELECT * FROM food_log',
        'INSERT': f"INSERT INTO food_log(user_id,date,meal_type,name) VALUES ('{rows.owner}','{DAY}','lunch','Injected')",
        'UPDATE': "UPDATE food_log SET name='Injected'",
        'DELETE': 'DELETE FROM food_log',
    }
    before = pg.sql('SELECT jsonb_agg(to_jsonb(f) ORDER BY id) FROM food_log f')
    with pytest.raises(RuntimeError, match='permission denied'):
        pg.sql(f'SET ROLE {role}; {queries[operation]}')
    assert pg.sql('SELECT jsonb_agg(to_jsonb(f) ORDER BY id) FROM food_log f') == before


class SQLTransport:
    """Translate only used PostgREST operations; all data/privilege checks run in PG."""
    def __init__(self, pg, table=None):
        self.pg, self.name = pg, table
        self.filters = []
        self.operation = 'select'

    def table(self, name):
        assert name in ('food_log', 'user_sessions')
        return SQLTransport(self.pg, name)

    def select(self, fields):
        assert fields in ('*', '*, users(*)')
        return self

    def eq(self, key, value):
        assert key in ('id', 'user_id', 'date', 'session_token')
        self.filters.append(f't.{key} = {literal(value)}')
        return self

    def gt(self, key, value):
        assert key == 'expires_at'
        self.filters.append(f't.{key} > {literal(value)}')
        return self

    def order(self, key):
        assert key == 'created_at'
        return self

    def insert(self, values):
        self.operation, self.values = 'insert', values
        return self

    def delete(self):
        self.operation = 'delete'
        return self

    def execute(self):
        where = ' AND '.join(self.filters) or 'true'
        if self.operation == 'insert':
            # Column names come from the actual route, never request-controlled SQL.
            assert set(self.values) <= {'user_id', 'date', 'meal_type', 'barcode', 'name', 'brand', 'serving_size', 'servings', 'calories', 'protein_g', 'carbs_g', 'fat_g', 'fiber_g', 'sugar_g', 'image_url'}
            columns = ','.join(self.values)
            query = f'INSERT INTO food_log ({columns}) VALUES ({",".join(literal(v) for v in self.values.values())}) RETURNING *'
        elif self.operation == 'delete':
            query = f'DELETE FROM food_log t WHERE {where} RETURNING *'
        elif self.name == 'user_sessions':
            query = f'SELECT t.*, to_jsonb(u) AS users FROM user_sessions t JOIN users u ON u.id=t.user_id WHERE {where}'
        else:
            query = f'SELECT * FROM food_log t WHERE {where} ORDER BY created_at'
        raw = self.pg.sql(f"SET ROLE service_role; WITH result AS ({query}) SELECT coalesce(jsonb_agg(to_jsonb(result)), '[]'::jsonb) FROM result")
        return SimpleNamespace(data=json.loads(raw))


@pytest.fixture(scope='module')
def api():
    spec = importlib.util.spec_from_file_location('food_log_access_app', ROOT / 'app.py')
    module = importlib.util.module_from_spec(spec)
    with patch('dotenv.load_dotenv', return_value=False), patch.dict(os.environ, {}, clear=True):
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(api, pg, rows, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('No production/model network in local food-log checks')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(api, 'supabase', SQLTransport(pg))
    monkeypatch.setattr(api, 'check_and_fulfill_referral', AsyncMock())
    with TestClient(api.app) as client:
        yield client


def body(**extra):
    return dict(date=DAY, meal_type='lunch', name="Rice O'Brien 🌶️", calories=210.5,
                protein_g=8, servings=1.5, **extra)


def test_authenticated_backend_crud_preserves_wire_contract(client, rows, pg):
    headers = {'Authorization': 'Bearer owner-session'}
    response = client.post('/food-log/add', headers=headers, json=body(user_id=rows.other))
    assert response.status_code == 200
    result = response.json()
    assert result['success'] is True and result['message'] == 'Item logged'
    entry = result['entry']
    assert entry['user_id'] == rows.owner
    for field, expected in body().items():
        assert entry[field] == expected
    assert entry['barcode'] is None and entry['carbs_g'] is None
    assert entry['created_at'] and entry['id']
    response = client.get('/food-log/' + DAY, headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result['success'] is True and result['message'] == 'Food log retrieved'
    assert {r['id'] for r in result['entries']} == {rows.entries[0], entry['id']}
    response = client.delete('/food-log/' + entry['id'], headers=headers)
    assert response.status_code == 200
    assert response.json() == {'success': True, 'message': 'Entry deleted'}
    assert pg.sql(f"SELECT count(*) FROM food_log WHERE id='{entry['id']}'") == '0'


def test_other_owner_cannot_read_or_delete_entries(client, rows, pg):
    headers = {'Authorization': 'Bearer other-session'}
    entries = client.get('/food-log/' + DAY, headers=headers).json()['entries']
    assert [entry['id'] for entry in entries] == [rows.entries[1]]
    before = pg.sql('SELECT jsonb_agg(to_jsonb(f) ORDER BY id) FROM food_log f')
    response = client.delete('/food-log/' + rows.entries[0], headers=headers)
    # Existing API deliberately does not disclose whether the entry exists.
    assert response.status_code == 200
    assert response.json() == {'success': True, 'message': 'Entry deleted'}
    assert pg.sql('SELECT jsonb_agg(to_jsonb(f) ORDER BY id) FROM food_log f') == before


@pytest.mark.parametrize('authorization', [None, 'Bearer invalid-session', 'Bearer expired-session'])
def test_unsigned_invalid_and_expired_sessions_cannot_use_any_route(client, rows, pg, authorization):
    headers = {} if authorization is None else {'Authorization': authorization}
    before = pg.sql('SELECT jsonb_agg(to_jsonb(f) ORDER BY id) FROM food_log f')
    for method, path, kwargs in [('POST', '/food-log/add', {'json': body()}),
                                  ('GET', '/food-log/' + DAY, {}),
                                  ('DELETE', '/food-log/' + rows.entries[0], {})]:
        assert client.request(method, path, headers=headers, **kwargs).status_code == 401
    assert pg.sql('SELECT jsonb_agg(to_jsonb(f) ORDER BY id) FROM food_log f') == before
