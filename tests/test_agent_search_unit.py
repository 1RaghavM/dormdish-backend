"""Actual SDK/HTTP checks for isolated public search and shared request budgets."""
from ast import literal_eval
import json

import httpx
import pytest

from dormdish_agent.tools import search_public_dining
from tests.test_agent_runtime import SECRET, answer, exercise, response, tool_call, tool_outputs
from tests.test_agent_tools import food_context

URL = 'https://food.rutgers.edu/busch'


def public_result(*, status='completed', url=URL, citations=True, search=True):
    output = [{'type': 'web_search_call', 'id': 'ws_1', 'status': 'completed',
               'action': {'type': 'search', 'query': 'Rutgers Busch hours'}}] if search else []
    output.append({'type': 'message', 'id': 'msg_public', 'status': 'completed', 'role': 'assistant',
        'content': [{'type': 'output_text', 'text': 'See the official dining hours.',
                     'annotations': [{'type': 'url_citation', 'title': 'Official dining hours',
                         'url': url, 'start_index': 0, 'end_index': 30}] if citations else []}]})
    return httpx.Response(200, json={'id': 'resp_public', 'object': 'response', 'created_at': 0,
        'model': 'gpt-6-luna', 'status': status, 'output': output})


def test_public_search_isolated_from_private_context_and_carries_citations():
    context = food_context(preferences={'items': [SECRET]}, allergens=('milk',))
    def respond(payload, number):
        if number == 1:
            return response([tool_call('search_public_dining', {'topic': 'opening_hours'})])
        if number == 2:
            serialized = json.dumps(payload)
            assert SECRET not in serialized and context.user_id not in serialized and 'milk' not in serialized
            assert payload['input'] == 'Rutgers University; busch-dining-hall; 2026-10-10; opening hours'
            assert payload['max_tool_calls'] == 1 and payload['store'] is False
            assert payload['tool_choice'] == {'type': 'web_search'}
            assert payload['tools'] == [{'type': 'web_search', 'search_context_size': 'low'}]
            assert not {'previous_response_id', 'conversation'} & payload.keys()
            return public_result()
        evidence = literal_eval(tool_outputs(payload)[0])
        assert evidence['selectable_menu_evidence'] is False and evidence['citations'][0]['url'] == URL
        return answer('Check the cited official hours.')
    turn, events, requests = exercise(respond, context=context, history=[{'role': 'user', 'content': SECRET}])
    assert len(requests) == 3 and context.model_calls == 3
    assert events[-1].type == 'complete' and str(events[-1].data.citations[0].url) == URL
    assert events[-1].data.saved_plan_id is None and context.proposal is None
    assert context.client.queries == []


@pytest.mark.parametrize('options', [{'status': 'incomplete'}, {'url': 'file:///private/secret'},
                                     {'citations': False}, {'search': False}])
def test_unverified_search_result_returns_no_citations_or_proposal(options):
    def respond(payload, number):
        if number == 1:
            return response([tool_call('search_public_dining', {'topic': 'opening_hours'})])
        if number == 2:
            return public_result(**options)
        assert 'could not verify' in tool_outputs(payload)[0]
        return answer('Current hours could not be verified.')
    turn, events, requests = exercise(respond)
    assert len(requests) == 3 and events[-1].data.citations == [] and turn.context.proposal is None


def test_search_is_single_attempt_even_after_http_failure():
    def respond(payload, number):
        if number in (1, 3):
            return response([tool_call('search_public_dining', {'topic': 'location'}, index=number)])
        if number == 2:
            return httpx.Response(500, json={'error': {'message': SECRET}})
        assert len(tool_outputs(payload)) == 2
        assert all('could not verify' in output for output in tool_outputs(payload))
        return answer('Public information unavailable.')
    turn, events, requests = exercise(respond)
    assert len(requests) == 4 and sum(p.get('tool_choice') == {'type': 'web_search'} for p in requests) == 1
    assert events[-1].data.citations == []


def test_public_lookup_and_agent_share_eight_model_request_budget():
    def respond(payload, number):
        if number == 1:
            return response([tool_call('search_public_dining', {'topic': 'location'})])
        if number == 2:
            return public_result()
        return response([tool_call('read_profile', index=number)])
    turn, events, requests = exercise(respond)
    assert len(requests) == 8 and events[-1].type == 'error'
    assert turn.history == []


def test_search_tool_has_only_fixed_public_topics():
    properties = search_public_dining.params_json_schema['properties']
    assert set(properties) == {'topic'}
    assert set(properties['topic']['enum']) == {'opening_hours', 'location', 'dining_policies'}
    assert search_public_dining.params_json_schema['additionalProperties'] is False
