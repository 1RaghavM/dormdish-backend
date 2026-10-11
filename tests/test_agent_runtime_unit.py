"""Small runtime checks; the independent companion suite owns broader acceptance cases."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agents import RunContextWrapper
from openai import AsyncOpenAI

from dormdish_agent.models import Nutrition, PlanningContext, RunState, TurnRequest
from dormdish_agent.runtime import AgentTurn
from dormdish_agent.tools import FoodContext, READ_TOOLS, preview_meal, retrieve_menu


class Database:
    def __init__(self, rows):
        self.data, self.filters = rows, []
    def table(self, table):
        self.table_name = table
        return self
    def select(self, _): return self
    def eq(self, name, value):
        self.filters.append((name, value))
        return self
    def order(self, *a, **kw): return self
    def limit(self, _): return self
    def execute(self): return self


def context(rows=()):
    return FoodContext(Database(list(rows)), str(uuid4()), 'rutgers',
        PlanningContext(dining_hall='busch', date=date(2026, 10, 10), meal_type='dinner'),
        Nutrition(calories=2000, protein_g=100, fat_g=70, carbs_g=200))


def test_tool_schemas_never_accept_owner_or_scope():
    assert [tool.name for tool in READ_TOOLS] == ['read_profile', 'retrieve_menu', 'read_intake', 'preview_meal', 'search_public_dining']
    for tool in READ_TOOLS:
        assert set(tool.params_json_schema['properties']) <= {'selections', 'topic'}
        assert tool.params_json_schema['additionalProperties'] is False


def test_menu_grounded_preview_and_single_repair():
    async def run():
        c = context([{'university': 'Rutgers University', 'dining_hall': 'busch-dining-hall',
            'date': '2026-10-10', 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'menu_data': {'items': [{'name': 'Rice', 'meal': 'dinner', 'serving_size': '1 cup',
                'calories': 200, 'protein_g': 4, 'fat_g': 1, 'carbs_g': 44}]}}])
        wrapped = RunContextWrapper(c)
        await retrieve_menu.on_invoke_tool(wrapped, '{}')
        await preview_meal.on_invoke_tool(wrapped, '{"selections":[{"menu_item_id":"fake","servings":1}]}')
        assert c.proposal is None
        key = c.snapshot.items[0]['menu_item_id']
        import json
        await preview_meal.on_invoke_tool(wrapped, json.dumps({'selections': [{'menu_item_id': key, 'servings': 2}]}))
        assert c.proposal.totals.calories == 400
        assert ('university', 'Rutgers University') in c.client.filters
        await preview_meal.on_invoke_tool(wrapped, json.dumps({'selections': [{'menu_item_id': key, 'servings': 1}]}))
        assert c.proposal is None
    asyncio.run(run())


def test_intake_scopes_owner_and_exposes_missing_completions():
    async def run():
        c = context()
        result = await c.intake()
        assert ('user_id', c.user_id) in c.client.filters
        assert ('date', '2026-10-10') in c.client.filters
        assert result['completion_records_available'] is False
        c.client.data = [{'user_id': str(uuid4()), 'date': '2026-10-10'}]
        with pytest.raises(ValueError): await c.intake()
    asyncio.run(run())


class Stream:
    def __init__(self, *, stall=False): self.cancelled, self.stall = False, stall
    async def stream_events(self):
        if self.stall: await asyncio.sleep(60)
        yield SimpleNamespace(type='raw_response_event', data=SimpleNamespace(type='response.output_text.delta', delta='Hello 🌶'))
    def to_input_list(self): return [{'role': 'assistant', 'content': 'Hello 🌶'}]
    def cancel(self): self.cancelled = True


@pytest.mark.parametrize('mode', ['success', 'deadline', 'disconnect'])
def test_stream_terminal_privacy_and_cancellation(monkeypatch, mode):
    async def run():
        stream = Stream(stall=mode == 'deadline')
        def start(agent, **kwargs):
            assert agent.model.model == 'gpt-6-luna'
            assert agent.model_settings.max_tokens == 2048
            assert agent.model_settings.store is False
            assert kwargs['run_config'].tracing_disabled
            assert kwargs['run_config'].trace_include_sensitive_data is False
            assert kwargs['max_turns'] == 8
            assert not {'conversation_id', 'previous_response_id', 'session'} & kwargs.keys()
            return stream
        monkeypatch.setattr('dormdish_agent.runtime.Runner.run_streamed', start)
        async with AsyncOpenAI(api_key='synthetic-test-key') as client:
            turn = AgentTurn(context(), TurnRequest(request_id=uuid4(), text='Dinner'), uuid4(), [],
                client=client, deadline_seconds=.01 if mode == 'deadline' else 45)
            gen = turn.stream()
            if mode == 'disconnect':
                await anext(gen)
                await gen.aclose()
                assert turn.state.events[-1].data.code == 'cancelled'
            else:
                events = [e async for e in gen]
                assert events[-1].type == ('error' if mode == 'deadline' else 'complete')
            assert stream.cancelled
            RunState.model_validate(turn.state.model_dump())
    asyncio.run(run())


def test_close_after_complete_does_not_append_error(monkeypatch):
    async def run():
        monkeypatch.setattr('dormdish_agent.runtime.Runner.run_streamed', lambda *a, **k: Stream())
        async with AsyncOpenAI(api_key='synthetic-test-key') as client:
            turn = AgentTurn(context(), TurnRequest(request_id=uuid4(), text='Dinner'), uuid4(), [], client=client)
            gen = turn.stream()
            while (await anext(gen)).type != 'complete': pass
            await gen.aclose()
            assert [e.type for e in turn.state.events] == ['text_delta', 'complete']
    asyncio.run(run())


def test_real_sdk_responses_tool_loop_and_usage_without_network():
    """Exercise the real Runner/Usage/tool/history path, mocking only HTTP."""
    import httpx
    import json
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == '/v1/responses'
        assert payload['model'] == 'gpt-6-luna'
        assert payload['store'] is False
        assert payload['max_output_tokens'] == 2048
        output = ([{'type': 'function_call', 'name': 'read_profile', 'call_id': 'call_1',
                    'arguments': '{}', 'id': 'fc_1', 'status': 'completed'}] if len(requests) == 1 else
                  [{'type': 'message', 'role': 'assistant', 'id': 'msg_1', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'Hello', 'annotations': []}]}])
        response = {'id': f'resp_{len(requests)}', 'object': 'response', 'created_at': 0,
            'model': 'gpt-6-luna', 'status': 'completed', 'output': output,
            'usage': {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30,
                'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}
        events = []
        if len(requests) == 2:
            events.append({'type': 'response.output_text.delta', 'delta': 'Hello', 'item_id': 'msg_1',
                           'output_index': 0, 'content_index': 0, 'sequence_number': 0})
        events.append({'type': 'response.completed', 'response': response, 'sequence_number': 1})
        body = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=body)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-test-key', http_client=http) as client:
                turn = AgentTurn(context(), TurnRequest(request_id=uuid4(), text='Hello'), uuid4(), [], client=client)
                events = [event async for event in turn.stream()]
                assert [event.type for event in events] == ['status', 'text_delta', 'complete']
                assert events[0].data.message == 'Checking your preferences'
                assert any(item.get('type') == 'function_call_output' for item in turn.history)
                assert len(requests) == 2
                assert all('previous_response_id' not in p and 'conversation' not in p for p in requests)
    asyncio.run(run())


def test_sdk_errors_do_not_log_private_content(monkeypatch, caplog):
    import logging
    def fail(*args, **kwargs):
        logging.getLogger('openai.agents').error('secret profile')
        logging.getLogger('openai._base_client').error('secret chat')
        raise RuntimeError('secret prompt')
    async def run():
        monkeypatch.setattr('dormdish_agent.runtime.Runner.run_streamed', fail)
        async with AsyncOpenAI(api_key='synthetic-test-key') as client:
            turn = AgentTurn(context(), TurnRequest(request_id=uuid4(), text='Dinner'), uuid4(), [], client=client)
            events = [e async for e in turn.stream()]
            assert events[-1].type == 'error'
            assert 'secret' not in events[-1].model_dump_json()
        assert 'secret' not in caplog.text
    asyncio.run(run())


def test_another_task_can_close_stream_without_leaking_sdk_privacy(monkeypatch):
    from dormdish_agent.runtime import _private_run
    async def run():
        monkeypatch.setattr('dormdish_agent.runtime.Runner.run_streamed', lambda *a, **k: Stream())
        async with AsyncOpenAI(api_key='synthetic-test-key') as client:
            turn = AgentTurn(context(), TurnRequest(request_id=uuid4(), text='Dinner'), uuid4(), [], client=client)
            gen = turn.stream()
            await anext(gen)
            assert not _private_run.get()
            await asyncio.create_task(gen.aclose())
            assert turn.state.status == 'error'
    asyncio.run(run())


def test_tool_budget_precedes_database_read():
    async def run():
        c = context()
        c.tool_calls = 16
        result = await retrieve_menu.on_invoke_tool(RunContextWrapper(c), '{}')
        assert 'could not verify' in result
        assert not c.client.filters
    asyncio.run(run())
