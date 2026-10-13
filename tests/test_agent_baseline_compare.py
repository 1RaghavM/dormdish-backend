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


def test_paired_runs_alternate_order_and_keep_per_request_usage(monkeypatch):
    from scripts import agent_baseline_compare as compare
    monkeypatch.setenv('OPENAI_API_KEY', 'private-synthetic-openai')
    monkeypatch.setenv('GEMINI_API_KEY', 'private-synthetic-gemini')
    report = asyncio.run(compare.run_comparison(repetitions=2))
    assert report['outcome'] == 'passed' and report['usage_is_synthetic'] is True
    assert [row['provider'] for row in report['runs']] == ['gemini', 'luna', 'luna', 'gemini']
    assert [len(row['requests']) for row in report['runs']] == [1, 5, 5, 1]
    for row in report['runs']:
        key = 'total_tokens' if row['provider'] == 'luna' else 'total_token_count'
        assert all(request['usage'][key] == 30 for request in row['requests'])
    assert 'private-synthetic' not in json.dumps(report)


@pytest.mark.parametrize('repetitions', [0, 4, -1, True, 1.5, '1'])
def test_invalid_repetitions_make_no_provider_calls(repetitions):
    from scripts import agent_baseline_compare as compare
    report = asyncio.run(compare.run_comparison(repetitions=repetitions))
    assert report['error_code'] == 'invalid_repetitions' and report['runs'] == []


@pytest.mark.parametrize('missing', ['OPENAI_API_KEY', 'GEMINI_API_KEY'])
def test_live_requires_both_explicit_keys_before_any_request(monkeypatch, missing):
    from scripts import agent_baseline_compare as compare
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-openai')
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-gemini')
    monkeypatch.delenv(missing)
    report = asyncio.run(compare.run_comparison(live=True))
    assert report['error_code'] == 'missing_api_key' and report['runs'] == []


@pytest.mark.parametrize('corruption', ['unknown_item', 'nonvegan', 'disliked', 'wrong_item_math',
                                      'wrong_totals', 'missed_target', 'nan_item', 'nan_totals'])
def test_shared_validation_rejects_unsafe_or_misleading_plan(corruption):
    from scripts import agent_baseline_compare as compare
    ctx = compare.context()
    asyncio.run(ctx.menu())
    items = [dict(i, servings=1) for i in compare.MENU[:3]]
    totals = dict(compare.TARGETS)
    if corruption == 'unknown_item':
        items[0]['name'] = 'Invented item'
    elif corruption == 'nonvegan':
        items[0] = dict(compare.MENU[4], servings=1)
    elif corruption == 'disliked':
        items[0] = dict(compare.MENU[3], servings=1)
    elif corruption == 'wrong_item_math':
        items[0]['protein_g'] += 1
    elif corruption == 'wrong_totals':
        totals['fat_g'] += 1
    elif corruption == 'missed_target':
        items = [items[0]]
        totals = {key: items[0][key] for key in compare.TARGETS}
    elif corruption == 'nan_item':
        items[0]['calories'] = float('nan')
    else:
        totals['calories'] = float('nan')
    with pytest.raises((ValueError, KeyError)):
        compare.check_plan(ctx, items, totals)


def gemini_sdk_transport(monkeypatch, compare, respond):
    """Use the real Google SDK with only the HTTP boundary replaced."""
    import httpx
    original = compare.genai.Client
    calls = []
    def client(**kwargs):
        assert kwargs['http_options'].base_url == 'https://generativelanguage.googleapis.com'
        assert kwargs['http_options'].retry_options.attempts == 1
        assert kwargs['http_options'].timeout <= 10000
        def handler(request):
            assert request.url.host == 'generativelanguage.googleapis.com'
            assert request.url.path.endswith('/models/gemini-2.5-flash:generateContent')
            calls.append(json.loads(request.content))
            return respond(len(calls))
        kwargs['http_options'].httpx_client = httpx.Client(transport=httpx.MockTransport(handler))
        return original(**kwargs)
    monkeypatch.setattr(compare.genai, 'Client', client)
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-never-transmitted')
    return calls


def test_gemini_repair_counts_both_billed_responses_without_logging_content(monkeypatch, capsys):
    import httpx
    from scripts import agent_baseline_compare as compare
    def respond(number):
        text = 'private-invalid-model-output' if number == 1 else json.dumps({
            'totals': compare.TARGETS, 'meals': {'dinner': {'description': 'Synthetic',
                'items': [dict(i, servings=1) for i in compare.MENU[:3]]}}, 'explanation': 'Synthetic'})
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': text}],
            'role': 'model'}, 'finishReason': 'STOP'}], 'usageMetadata': {
                'promptTokenCount': 11 * number, 'candidatesTokenCount': 7,
                'thoughtsTokenCount': 3, 'totalTokenCount': 11 * number + 10}})
    calls = gemini_sdk_transport(monkeypatch, compare, respond)
    ctx = compare.context()
    asyncio.run(ctx.menu())
    requests = []
    asyncio.run(compare.gemini_case(ctx, requests, True))
    assert len(calls) == len(requests) == 2
    assert [request['usage']['total_token_count'] for request in requests] == [21, 32]
    assert all(call['generationConfig']['maxOutputTokens'] == 2048 for call in calls)
    assert [call['generationConfig']['temperature'] for call in calls] == [0.4, 0.1]
    assert capsys.readouterr() == ('', '')
    assert 'private-invalid-model-output' not in json.dumps(requests)


def test_gemini_overload_is_bounded_including_legacy_retry(monkeypatch, capsys):
    import httpx
    from scripts import agent_baseline_compare as compare
    calls = gemini_sdk_transport(monkeypatch, compare, lambda _: httpx.Response(503, json={
        'error': {'code': 503, 'message': 'private-provider-error', 'status': 'UNAVAILABLE'}}))
    ctx = compare.context()
    asyncio.run(ctx.menu())
    requests = []
    with pytest.raises(Exception):
        asyncio.run(compare.gemini_case(ctx, requests, True))
    assert len(calls) == len(requests) == 2
    assert all(request['usage'] is None for request in requests)
    assert capsys.readouterr() == ('', '')
    assert 'private-provider-error' not in json.dumps(requests)


@pytest.mark.parametrize('event_type', ['response.completed', 'response.incomplete', 'response.failed'])
def test_usage_is_preserved_across_byte_boundaries_and_failed_responses(event_type):
    import httpx
    from scripts import agent_baseline_compare as compare
    frame = {'type': event_type, 'response': {'usage': {
        'input_tokens': 11, 'output_tokens': 7, 'total_tokens': 18,
        'input_tokens_details': {'cached_tokens': 3},
        'output_tokens_details': {'reasoning_tokens': 2}}, 'text': 'private-🌶-output'}}
    wire = ('data: ' + json.dumps(frame, ensure_ascii=False) + '\n\n').encode()
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            for byte in wire:
                yield bytes([byte])
    record = {'usage': None}
    async def read():
        return b''.join([chunk async for chunk in compare.UsageStream(Bytes(), record)])
    assert asyncio.run(read()) == wire
    assert record['usage'] == dict(input_tokens=11, output_tokens=7, total_tokens=18,
                                   cached_tokens=3, reasoning_tokens=2)
    assert 'private' not in json.dumps(record)


def test_luna_eighth_request_is_last_permitted_transport_call():
    import httpx
    from scripts import agent_baseline_compare as compare
    calls, requests = [], []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=b'')
    transport = compare.LunaTransport(httpx.MockTransport(respond), requests)
    async def run():
        request = httpx.Request('POST', 'https://api.openai.com/v1/responses',
                               json={'model': 'gpt-6-luna', 'stream': True})
        for _ in range(8):
            await transport.handle_async_request(request)
        with pytest.raises(ValueError):
            await transport.handle_async_request(request)
    asyncio.run(run())
    assert len(calls) == len(requests) == 8


def test_luna_deadline_cancels_pending_http_without_retry(monkeypatch):
    import httpx
    from scripts import agent_baseline_compare as compare
    cancelled = []
    async def stall(request):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-key')
    monkeypatch.setitem(compare.CONFIG['luna'], 'deadline_seconds', 0.03)
    monkeypatch.setattr(compare.httpx, 'AsyncHTTPTransport', lambda **_: httpx.MockTransport(stall))
    requests = []
    with pytest.raises((ValueError, AttributeError)):
        asyncio.run(compare.luna_case(compare.context(), requests, True))
    assert len(requests) == 1 and cancelled == [True]


def test_gemini_deadline_keeps_usage_but_rejects_late_result(monkeypatch):
    import httpx
    from scripts import agent_baseline_compare as compare
    now = [100.0]
    monkeypatch.setattr(compare, 'monotonic', lambda: now[0])
    def late(_):
        now[0] = 146.0
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': '{}'}],
            'role': 'model'}, 'finishReason': 'STOP'}], 'usageMetadata': {
                'promptTokenCount': 11, 'candidatesTokenCount': 7, 'totalTokenCount': 18}})
    calls = gemini_sdk_transport(monkeypatch, compare, late)
    ctx = compare.context()
    asyncio.run(ctx.menu())
    requests = []
    with pytest.raises(ValueError, match='deadline'):
        asyncio.run(compare.gemini_case(ctx, requests, True))
    assert len(calls) == len(requests) == 1
    assert requests[0]['usage']['total_token_count'] == 18


@pytest.mark.parametrize('broken', ['missing_usage', 'missing_profile', 'malformed_private_output'])
def test_incomplete_luna_scenario_fails_with_sanitized_report(monkeypatch, broken, capsys):
    import httpx
    from scripts import agent_baseline_compare as compare
    original = compare.smoke.offline_response
    def response(ctx, number):
        normal = original(ctx, number)
        frame = json.loads(normal.read().decode()[6:])
        if broken == 'missing_usage':
            frame['response'].pop('usage')
        elif broken == 'missing_profile' and number == 1:
            frame['response']['output'][0]['name'] = 'read_intake'
        text = ('private-provider-output' if broken == 'malformed_private_output'
                else json.dumps(frame))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              stream=httpx.ByteStream(('data: ' + text + '\n\n').encode()))
    monkeypatch.setattr(compare.smoke, 'offline_response', response)
    report = asyncio.run(compare.run_comparison())
    assert report['outcome'] == 'error'
    row = report['runs'][1]
    assert row['outcome'] == 'error' and row['error_code'] == 'comparison_case_failed'
    assert len(row['requests']) <= 8
    assert 'private-provider-output' not in json.dumps(report)
    assert 'private-provider-output' not in str(capsys.readouterr())
