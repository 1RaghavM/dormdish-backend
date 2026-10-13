"""Synthetic evaluation harness must measure providers without touching user data."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest


def test_offline_cli_uses_real_sdks_without_network_database_or_credentials():
    # A new interpreter also catches forbidden import-time clients and dotenv use.
    code = '''
import importlib.abc, runpy, sys
import httpx
class NoDatabase(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'app', 'supabase', 'psycopg', 'psycopg2'}:
            raise AssertionError('Database/application import forbidden')
sys.meta_path.insert(0, NoDatabase())
def forbidden(*args, **kwargs):
    raise AssertionError('Offline network transport forbidden')
httpx.HTTPTransport = httpx.AsyncHTTPTransport = forbidden
sys.argv = ['agent_baseline_compare.py']
runpy.run_module('scripts.agent_baseline_compare', run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', code],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['mode'] == 'offline'
    assert report['outcome'] == 'passed'
