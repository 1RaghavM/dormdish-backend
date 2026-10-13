"""Bounded synthetic release smoke. Offline unless explicitly invoked with --live."""
import argparse
from ast import literal_eval
import asyncio
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
from time import monotonic
from uuid import uuid4

# Support direct invocation from any directory without importing the deployed app.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from openai import AsyncOpenAI

from dormdish_agent.models import Nutrition, PlanningContext, TurnRequest
from dormdish_agent.runtime import AgentTurn, MODEL
from dormdish_agent.tools import FoodContext
from services.meal_planning import build_menu_snapshot, remaining_targets

PINS = {'openai-agents': '0.8.1', 'openai': '2.26.0', 'pydantic': '2.12.3'}
PROMPT = ('Read my profile, menu and intake, then preview exactly one serving of the menu rice. '
          'Explain the intake coverage limitation. This is a synthetic test, not a save request; '
          'do not search the web.')


class SyntheticContext(FoodContext):
    async def menu(self):
        now = datetime.now(timezone.utc)
        row = {'university': self.university, 'dining_hall': self.planning.dining_hall,
               'date': self.planning.date.isoformat(), 'fetched_at': now.isoformat(),
               'menu_data': {'items': [{'id': 'rice', 'name': 'Rice', 'meal': 'dinner',
                   'serving_size': '1 cup', 'calories': 200, 'protein_g': 4, 'fat_g': 1,
                   'carbs_g': 44, 'allergens': [], 'dietary_tags': ['vegan']}]}}
        self.snapshot = build_menu_snapshot(row, university=self.university, context=self.planning,
                                             now=now, max_age=timedelta(hours=24))
        return self.snapshot

    async def intake(self):
        remaining = remaining_targets(self.targets, [dict(id='synthetic-lunch', user_id=self.user_id,
            date=self.planning.date.isoformat(), calories=300, protein_g=15, fat_g=10, carbs_g=40)],
            [], user_id=self.user_id, requested_date=self.planning.date)
        return {'remaining_after_food_logs': remaining.model_dump(), 'completion_records_available': False,
                'limitation': 'Synthetic logged lunch only; unlogged food is unknown.'}


def offline_response(context, number):
    if number <= 4:
        name = ['read_profile', 'retrieve_menu', 'read_intake', 'preview_meal'][number - 1]
        arguments = {'selections': [{'menu_item_id': context.snapshot.items[0]['menu_item_id'],
                                     'servings': 1}]} if number == 4 else {}
        output = [{'type': 'function_call', 'id': f'fc_{number}', 'call_id': f'call_{number}',
                   'name': name, 'arguments': json.dumps(arguments), 'status': 'completed'}]
    else:
        output = [{'type': 'message', 'role': 'assistant', 'id': 'msg_smoke', 'status': 'completed',
                   'content': [{'type': 'output_text', 'text': 'Synthetic meal preview.', 'annotations': []}]}]
    event = {'type': 'response.completed', 'sequence_number': 0, 'response': {
        'id': f'resp_{number}', 'object': 'response', 'created_at': 0, 'model': MODEL,
        'status': 'completed', 'output': output, 'usage': {'input_tokens': 20, 'output_tokens': 10,
        'total_tokens': 30, 'input_tokens_details': {'cached_tokens': 0},
        'output_tokens_details': {'reasoning_tokens': 0}}}}
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                          stream=httpx.ByteStream(('data: ' + json.dumps(event) + '\n\n').encode()))


class MeasuredStream(httpx.AsyncByteStream):
    """Retain only usage counters; never emit or retain model text in the report."""
    def __init__(self, stream, metrics):
        self.stream, self.metrics = stream, metrics

    async def __aiter__(self):
        pending = b''
        async for chunk in self.stream:
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if line.startswith(b'data: ') and line[6:].strip() != b'[DONE]':
                    event = json.loads(line[6:])
                    if event.get('type') == 'response.completed':
                        usage = event['response'].get('usage')
                        if usage:
                            for key in ('input_tokens', 'output_tokens', 'total_tokens'):
                                count = usage.get(key)
                                if type(count) is not int or count < 0:
                                    raise ValueError('Invalid usage metadata')
                                self.metrics[key] += count
                            self.metrics['responses_with_usage'] += 1
            if len(pending) > 262144:
                raise ValueError('Stream line budget exceeded')
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class MeasuredTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport, metrics):
        self.transport, self.metrics = transport, metrics

    async def handle_async_request(self, request):
        # No custom endpoint, search request, redirects or other API operations.
        if str(request.url) != 'https://api.openai.com/v1/responses':
            raise ValueError('Unexpected API endpoint')
        payload = json.loads(request.content)
        if payload.get('model') != MODEL or payload.get('stream') is not True:
            raise ValueError('Only the bounded streamed model scenario is allowed')
        self.metrics['requests'] += 1
        response = await self.transport.handle_async_request(request)
        if not response.is_success:
            self.metrics['http_status'] = response.status_code
        response.stream = MeasuredStream(response.stream, self.metrics)
        return response

    async def aclose(self):
        await self.transport.aclose()


async def run_smoke(*, live=False, deadline_seconds=45):
    started = monotonic()
    report = {'mode': 'live' if live else 'offline', 'model': MODEL, 'outcome': 'error',
              'error_code': None, 'requests': 0, 'responses_with_usage': 0, 'input_tokens': 0,
              'output_tokens': 0, 'total_tokens': 0, 'tool_calls': 0, 'model_calls': 0,
              'previews': 0, 'usage_is_synthetic': not live}
    try:
        report['versions'] = {package: version(package) for package in PINS}
        if report['versions'] != PINS or MODEL != 'gpt-6-luna':
            report['error_code'] = 'runtime_version_mismatch'
            return report
        if not 0 < deadline_seconds <= 45:
            report['error_code'] = 'invalid_deadline'
            return report
        key = os.environ.get('OPENAI_API_KEY', '').strip() if live else 'synthetic-no-network'
        if not key:
            report['error_code'] = 'missing_api_key'
            return report
        context = SyntheticContext(None, str(uuid4()), 'rutgers', PlanningContext(
            dining_hall='busch', date=date(2026, 10, 13), meal_type='dinner'),
            Nutrition(calories=2000, protein_g=100, fat_g=70, carbs_g=200), restrictions=('vegan',))
        transport = (httpx.AsyncHTTPTransport(retries=0) if live else httpx.MockTransport(
            lambda _: offline_response(context, report['requests'])))
        async with httpx.AsyncClient(transport=MeasuredTransport(transport, report), trust_env=False) as http:
            async with AsyncOpenAI(api_key=key, base_url='https://api.openai.com/v1', http_client=http,
                                   max_retries=0, timeout=30) as client:
                turn = AgentTurn(context, TurnRequest(request_id=uuid4(), text=PROMPT), uuid4(), [],
                                 client=client, deadline_seconds=deadline_seconds)
                async for event in turn.stream():
                    if event.type == 'meal_preview':
                        report['previews'] += 1
                report['tool_calls'], report['model_calls'] = context.tool_calls, context.model_calls
                calls = {item['call_id']: item['name'] for item in turn.history
                         if item.get('type') == 'function_call'}
                evidence = {}
                for item in turn.history:
                    if item.get('type') != 'function_call_output' or item.get('call_id') not in calls:
                        continue
                    try:
                        # The pinned SDK serializes function-return dictionaries with str(), not JSON.
                        output = literal_eval(item['output'])
                    except (SyntaxError, ValueError):
                        continue
                    if isinstance(output, dict):
                        evidence[calls[item['call_id']]] = output
                profile = evidence.get('read_profile', {})
                intake = evidence.get('read_intake', {})
                proposal = context.proposal
                if turn.state.status == 'error':
                    report['error_code'] = turn.state.events[-1].data.code
                elif (proposal is None or report['previews'] != 1
                      or profile.get('targets') != context.targets.model_dump()
                      or profile.get('restrictions') != context.restrictions
                      or profile.get('allergens') != context.allergens
                      or profile.get('preferences') != context.preferences
                      or profile.get('planning') != context.planning.model_dump(mode='json')
                      or context.snapshot is None
                      or evidence.get('retrieve_menu', {}).get('items') != context.snapshot.items
                      or intake.get('remaining_after_food_logs') != dict(
                          calories=1700, protein_g=85, fat_g=60, carbs_g=160)
                      or intake.get('completion_records_available') is not False
                      or evidence.get('preview_meal') != proposal.model_dump(mode='json')
                      or proposal.totals.model_dump() != dict(calories=200, protein_g=4, fat_g=1, carbs_g=44)
                      or report['responses_with_usage'] != report['requests'] or turn.search_attempted
                      or turn.state.events[-1].data.saved_plan_id is not None):
                    report['error_code'] = 'scenario_not_satisfied'
                else:
                    report['outcome'] = 'passed'
    except Exception:
        report['error_code'] = 'smoke_unavailable'
    finally:
        report['duration_seconds'] = round(monotonic() - started, 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Make bounded paid OpenAI calls with synthetic data')
    parser.add_argument('--deadline-seconds', type=float, default=45)
    args = parser.parse_args()
    report = asyncio.run(run_smoke(live=args.live, deadline_seconds=args.deadline_seconds))
    print(json.dumps(report, sort_keys=True))
    return 0 if report['outcome'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
