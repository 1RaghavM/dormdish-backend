"""Production policy regression on synthetic rows; backend service access is retained."""
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_agent_storage_unit import pg


def test_food_log_migration_denies_public_roles_and_preserves_backend_access(pg):
    owner, entry = uuid4(), uuid4()
    pg.sql(f'''CREATE TABLE food_log(id uuid PRIMARY KEY, user_id uuid NOT NULL, name text NOT NULL);
        ALTER TABLE food_log ENABLE ROW LEVEL SECURITY;
        CREATE POLICY "Allow service role full access" ON food_log FOR ALL USING (true);
        GRANT ALL ON food_log TO PUBLIC, anon, authenticated, service_role;
        INSERT INTO food_log VALUES ('{entry}', '{owner}', 'Synthetic rice');''')
    before = pg.sql('SELECT jsonb_agg(to_jsonb(f)) FROM food_log f')
    assert pg.sql('SET ROLE anon; SELECT count(*) FROM food_log') == '1'
    migration = (Path(__file__).resolve().parents[1] / 'migrations/secure_food_log_access.sql').read_text()
    pg.sql(migration)
    pg.sql(migration)  # A repeated schema deployment is safe.
    assert pg.sql('SELECT jsonb_agg(to_jsonb(f)) FROM food_log f') == before
    for role in ('anon', 'authenticated'):
        for query in ('SELECT * FROM food_log', 'DELETE FROM food_log',
                      "UPDATE food_log SET name='Changed'",
                      f"INSERT INTO food_log VALUES ('{uuid4()}', '{owner}', 'Injected')"):
            with pytest.raises(RuntimeError, match='permission denied'):
                pg.sql(f'SET ROLE {role}; {query}')
    assert pg.sql('SET ROLE service_role; SELECT count(*) FROM food_log') == '1'
    pg.sql(f"SET ROLE service_role; UPDATE food_log SET name='Updated' WHERE id='{entry}'")
    assert pg.sql(f"SET ROLE service_role; SELECT name FROM food_log WHERE id='{entry}'") == 'Updated'
    pg.sql(f"SET ROLE service_role; DELETE FROM food_log WHERE id='{entry}'")
    pg.sql(f"SET ROLE service_role; INSERT INTO food_log VALUES ('{entry}', '{owner}', 'Restored')")
    assert pg.sql('SET ROLE service_role; SELECT count(*) FROM food_log') == '1'
