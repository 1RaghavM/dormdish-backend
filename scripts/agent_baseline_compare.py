"""Controlled synthetic dinner comparison, offline unless --live is explicitly selected."""
import argparse
from ast import literal_eval
import asyncio
from contextlib import redirect_stdout, redirect_stderr
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version
import io
import json
import math
import os
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from google import genai
from google.genai import types
from openai import AsyncOpenAI

# The legacy module loads .env at import; this harness accepts explicit environment keys only.
with patch('dotenv.load_dotenv', return_value=False):
    import meal_plan_agent as gemini
from scripts import agent_release_smoke as smoke
from dormdish_agent.models import Nutrition, PlanningContext, TurnRequest
from dormdish_agent.runtime import AgentTurn, MODEL
from dormdish_agent.tools import FoodContext
from services.meal_planning import build_menu_snapshot, validate_meal

GEMINI_MODEL = 'gemini-2.5-flash'
TARGETS = dict(calories=600, protein_g=36, fat_g=14, carbs_g=82)
MENU = [dict(id=name.lower(), name=name, meal='dinner', serving_size='1 cup',
             calories=cal, protein_g=pro, fat_g=fat, carbs_g=carb, allergens=[], dietary_tags=tags)
        for name, cal, pro, fat, carb, tags in [
            ('Rice', 200, 4, 1, 44, ['vegan']), ('Beans', 200, 12, 3, 30, ['vegan']),
            ('Tofu', 200, 20, 10, 8, ['vegan']), ('Mushrooms', 100, 3, 1, 18, ['vegan']),
            ('Chicken', 200, 30, 8, 0, [])]]
PROMPT = ('Read my profile, menu and intake, then preview a vegan dinner near 600 calories and '
          '36 g protein. I dislike mushrooms. Do not save or search. Explain any target shortfall.')
CONFIG = {'gemini': {'model': GEMINI_MODEL, 'temperature': 0.4, 'max_output_tokens': 2048,
                    'thinking_budget': 512, 'max_requests': 2, 'http_timeout_seconds': 10,
                    'cached_content': None, 'deadline_seconds': 45},
          'luna': {'model': MODEL, 'max_requests': 8, 'max_output_tokens': 2048,
                   'deadline_seconds': 45, 'http_timeout_seconds': 30}}


class DinnerContext(FoodContext):
    async def menu(self):
        now = datetime.now(timezone.utc)
        self.snapshot = build_menu_snapshot({'university': self.university,
            'dining_hall': self.planning.dining_hall, 'date': self.planning.date.isoformat(),
            'fetched_at': now.isoformat(), 'menu_data': {'items': MENU}},
            university=self.university, context=self.planning, now=now, max_age=timedelta(hours=24))
        return self.snapshot

    async def intake(self):
        return {'remaining_targets': TARGETS, 'completion_records_available': True,
                'limitation': 'Synthetic empty intake ledger.'}


def context():
    return DinnerContext(None, str(uuid4()), 'rutgers', PlanningContext(
        dining_hall='busch', date=date(2026, 10, 13), meal_type='dinner'), Nutrition(**TARGETS),
        restrictions=('vegan',), preferences={'dislikes': ['Mushrooms']})


def check_plan(ctx, items, totals):
    """Apply one trusted validator to both outputs; do not trust legacy fallback nutrition."""
    if any(type(v) not in (int, float) or not math.isfinite(v) for i in items for v in [i[k] for k in TARGETS]) or any(type(totals[k]) not in (int, float) or not math.isfinite(totals[k]) for k in TARGETS):
        raise ValueError('Invalid nutrition')
    lookup = {i['name']: i['menu_item_id'] for i in ctx.snapshot.items}
    selections = [{'menu_item_id': lookup[i['name']], 'servings': i['servings']} for i in items]
    proposal = validate_meal(ctx.snapshot, selections, university=ctx.university, context=ctx.planning,
                            now=datetime.now(timezone.utc), restrictions=ctx.restrictions, allergens=())
    if any(i['name'] == 'Mushrooms' for i in items):
        raise ValueError('Preference violated')
    for original, trusted in zip(items, proposal.items):
        if any(abs(original[k] - getattr(trusted, k)) > .01 for k in TARGETS):
            raise ValueError('Item nutrition differs')
    if any(abs(totals[k] - getattr(proposal.totals, k)) > .01 for k in TARGETS):
        raise ValueError('Totals differ')
    if any(abs(totals[k] - TARGETS[k]) > TARGETS[k] * .05 for k in ('calories', 'protein_g')):
        raise ValueError('Target not met')


def usage_record(usage, fields):
    result = {key: getattr(usage, key, None) if not isinstance(usage, dict) else usage.get(key)
              for key in fields}
    if any(value is not None and (type(value) is not int or value < 0) for value in result.values()):
        raise ValueError('Invalid usage counters')
    return result


class UsageStream(httpx.AsyncByteStream):
    def __init__(self, stream, record):
        self.stream, self.record = stream, record

    async def __aiter__(self):
        pending = b''
        async for chunk in self.stream:
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if line.startswith(b'data: ') and line[6:].strip() != b'[DONE]':
                    event = json.loads(line[6:])
                    if event.get('type') in ('response.completed', 'response.failed', 'response.incomplete'):
                        usage = event['response'].get('usage') or {}
                        self.record['usage'] = usage_record(usage, ('input_tokens', 'output_tokens', 'total_tokens'))
                        for details, key in [('input_tokens_details', 'cached_tokens'),
                                             ('output_tokens_details', 'reasoning_tokens')]:
                            self.record['usage'].update(usage_record(usage.get(details) or {}, (key,)))
            if len(pending) > 262144:
                raise ValueError('Stream line exceeded budget')
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class LunaTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner, requests):
        self.inner, self.requests = inner, requests

    async def handle_async_request(self, request):
        payload = json.loads(request.content)
        if (str(request.url) != 'https://api.openai.com/v1/responses' or payload.get('model') != MODEL
                or payload.get('stream') is not True or len(self.requests) >= 8):
            raise ValueError('Request outside comparison budget')
        record = {'usage': None}
        self.requests.append(record)
        response = await self.inner.handle_async_request(request)
        record['http_status'] = response.status_code
        response.stream = UsageStream(response.stream, record)
        return response

    async def aclose(self):
        await self.inner.aclose()


async def luna_case(ctx, requests, live):
    async def offline(request):
        response = smoke.offline_response(ctx, len(requests))
        if len(requests) == 4:
            event = json.loads((await response.aread()).decode()[6:])
            event['response']['output'][0]['arguments'] = json.dumps({'selections': [
                {'menu_item_id': i['menu_item_id'], 'servings': 1} for i in ctx.snapshot.items[:3]]})
            return httpx.Response(200, stream=httpx.ByteStream(('data: ' + json.dumps(event) + '\n\n').encode()))
        return response
    inner = httpx.AsyncHTTPTransport(retries=0) if live else httpx.MockTransport(offline)
    async with httpx.AsyncClient(transport=LunaTransport(inner, requests), trust_env=False) as http:
        async with AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'] if live else 'synthetic',
            base_url='https://api.openai.com/v1', http_client=http, max_retries=0, timeout=30) as client:
            turn = AgentTurn(ctx, TurnRequest(request_id=uuid4(), text=PROMPT), uuid4(), [], client=client,
                             deadline_seconds=CONFIG['luna']['deadline_seconds'])
            async for _ in turn.stream():
                pass
            calls = {i['call_id']: i['name'] for i in turn.history if i.get('type') == 'function_call'}
            evidence = {calls[i['call_id']]: literal_eval(i['output']) for i in turn.history
                        if i.get('type') == 'function_call_output' and i.get('call_id') in calls}
            if (evidence.get('read_profile', {}).get('targets') != TARGETS
                    or evidence.get('retrieve_menu', {}).get('items') != ctx.snapshot.items
                    or evidence.get('read_intake', {}).get('remaining_targets') != TARGETS):
                raise ValueError('Context tools did not succeed')
            if turn.state.status != 'complete' or ctx.proposal is None or turn.search_attempted:
                raise ValueError('No validated preview')
            check_plan(ctx, [i.model_dump() for i in ctx.proposal.items], ctx.proposal.totals.model_dump())


async def gemini_case(ctx, requests, live):
    deadline = monotonic() + CONFIG['gemini']['deadline_seconds']
    client = genai.Client(vertexai=False, api_key=os.environ['GEMINI_API_KEY'], http_options=types.HttpOptions(
        base_url='https://generativelanguage.googleapis.com', timeout=10000, client_args={'trust_env': False},
        retry_options=types.HttpRetryOptions(attempts=1))) if live else None
    def generate(**kwargs):
        if len(requests) >= 2 or kwargs['model'] != GEMINI_MODEL or monotonic() >= deadline:
            raise ValueError('Request outside comparison budget')
        record = {'usage': None}
        requests.append(record)
        if client:
            response = client.models.generate_content(**kwargs)
        else:
            meal = {'description': 'Synthetic dinner', 'items': [
                {k: i[k] for k in ('name', 'serving_size', *TARGETS)} | {'servings': 1} for i in MENU[:3]]}
            response = SimpleNamespace(text=json.dumps({'totals': TARGETS, 'meals': {'dinner': meal},
                'explanation': 'Synthetic fixture'}), usage_metadata=SimpleNamespace(
                prompt_token_count=20, candidates_token_count=10, thoughts_token_count=0,
                cached_content_token_count=0, total_token_count=30, tool_use_prompt_token_count=0))
        record['usage'] = usage_record(response.usage_metadata, ('prompt_token_count', 'candidates_token_count',
            'thoughts_token_count', 'cached_content_token_count', 'total_token_count', 'tool_use_prompt_token_count'))
        if monotonic() >= deadline:
            raise ValueError('Comparison deadline exceeded')
        return response
    try:
        with patch.object(gemini, '_get_client', return_value=SimpleNamespace(models=SimpleNamespace(generate_content=generate))), patch.dict(os.environ, {
            'MEALPLAN_MODEL': GEMINI_MODEL, 'MEALPLAN_TEMPERATURE': '0.4', 'MEALPLAN_MAX_TOKENS': '2048',
            'MEALPLAN_THINKING_BUDGET': '512', 'MEALPLAN_OVERLOAD_RETRIES': '1'}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result, _ = await gemini.generate_meal_plan_with_agent(user_payload={'dietary_preference': 'vegan'},
                menu_payload={'requested_dining_hall': 'busch', 'requested_date': ctx.planning.date.isoformat(),
                              'allowed_items': MENU}, nutrition_targets=TARGETS,
                user_preferences_context='Dislikes mushrooms. No food eaten today.', exclude_meals=['breakfast', 'lunch'])
        check_plan(ctx, [i.model_dump() for i in result.meals.dinner.items], result.totals.model_dump())
    finally:
        if client:
            client.close()


async def run_comparison(*, live=False, repetitions=1):
    report = {'mode': 'live' if live else 'offline', 'usage_is_synthetic': not live,
              'scope': 'controlled_synthetic_uncached_dinner', 'config': CONFIG, 'runs': [], 'outcome': 'error',
              'versions': {p: version(p) for p in (*smoke.PINS, 'google-genai')}}
    if type(repetitions) is not int or not 1 <= repetitions <= 3:
        return report | {'error_code': 'invalid_repetitions'}
    if live and any(not os.getenv(key, '').strip() for key in ('OPENAI_API_KEY', 'GEMINI_API_KEY')):
        return report | {'error_code': 'missing_api_key'}
    if any(report['versions'][p] != v for p, v in smoke.PINS.items()):
        return report | {'error_code': 'runtime_version_mismatch'}
    for repetition in range(repetitions):
        for name in (('gemini', 'luna') if repetition % 2 == 0 else ('luna', 'gemini')):
            row = {'provider': name, 'repetition': repetition + 1, 'requests': [], 'outcome': 'error'}
            started = monotonic()
            try:
                ctx = context()
                if name == 'gemini':
                    await ctx.menu()
                await (gemini_case if name == 'gemini' else luna_case)(ctx, row['requests'], live)
                required = ('prompt_token_count', 'candidates_token_count', 'total_token_count') if name == 'gemini' else ('input_tokens', 'output_tokens', 'total_tokens')
                if not row['requests'] or any(r['usage'] is None or any(r['usage'].get(k) is None for k in required) for r in row['requests']):
                    raise ValueError('Incomplete usage')
                row['outcome'] = 'passed'
            except Exception:
                row['error_code'] = 'comparison_case_failed'
            row['duration_seconds'] = round(monotonic() - started, 3)
            report['runs'].append(row)
    report['outcome'] = 'passed' if all(r['outcome'] == 'passed' for r in report['runs']) else 'error'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--repetitions', type=int, default=1)
    args = parser.parse_args()
    report = asyncio.run(run_comparison(live=args.live, repetitions=args.repetitions))
    print(json.dumps(report, sort_keys=True))
    return 0 if report['outcome'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
