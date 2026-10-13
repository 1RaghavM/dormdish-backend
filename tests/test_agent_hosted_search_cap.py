"""A capped hosted lookup can return an ignored searching entry beside its result."""
from ast import literal_eval
import json

import httpx
import pytest

from tests.test_agent_public_search import SOURCE, is_search, search_result
from tests.test_agent_runtime import SECRET, answer, exercise, response, tool_call, tool_outputs
from tests.test_agent_tools import food_context


def capped_search(statuses=('completed', 'searching'), *, response_status='completed', citations=1):
    body = search_result(count=citations).json()
    completed = body['output'][0]
    body['status'] = response_status
    body['output'] = [
        {'type': 'reasoning', 'id': 'reasoning_public', 'summary': []},
        *[dict(completed, id=f'web_{i}', status=status) for i, status in enumerate(statuses)],
        body['output'][-1],
    ]
    return httpx.Response(200, json=body)


def test_one_completed_search_survives_an_ignored_attempt_and_preserves_public_boundary():
    context = food_context(preferences={'favorites': ['private-favorite']}, allergens=('private-allergen',))

    def respond(payload, number):
        if number == 1:
            return response([tool_call('read_profile')])
        if number == 2:
            assert 'private-favorite' in tool_outputs(payload)[0]
            return response([tool_call('search_public_dining', {'topic': 'opening_hours'}, number)])
        if is_search(payload):
            assert payload['input'] == 'Rutgers University; busch-dining-hall; 2026-10-10; opening hours'
            assert payload['model'] == 'gpt-6-luna' and payload['store'] is False
            assert payload['max_tool_calls'] == 1 and payload['max_output_tokens'] == 2048
            assert payload['tools'] == [{'type': 'web_search', 'search_context_size': 'low'}]
            wire = json.dumps(payload)
            assert not any(private in wire for private in (
                SECRET, context.user_id, 'private-favorite', 'private-allergen', 'private-history'))
            return capped_search()
        output = tool_outputs(payload)[-1]
        assert 'could not verify' not in output
        evidence = literal_eval(output)
        assert evidence['answer'] == 'Dining hours are posted by the university.'
        assert evidence['citations'] == [{'title': 'University dining', 'url': SOURCE}]
        assert evidence['selectable_menu_evidence'] is False
        return answer('Consult the university dining page.')

    turn, events, requests = exercise(respond, context=context,
        history=[{'role': 'assistant', 'content': 'private-history'}])
    assert len(requests) == 4 and sum(map(is_search, requests)) == 1
    assert events[-1].type == 'complete' and str(events[-1].data.citations[0].url) == SOURCE
    assert SOURCE in turn.assistant_text and context.client.queries == []
    assert turn.context.proposal is None


@pytest.mark.parametrize('options', [
    {'statuses': ('completed', 'completed', 'searching')},
    {'statuses': ('searching',)},
    {'statuses': ('searching', 'completed')},
    {'statuses': ()},
    {'statuses': ('completed', 'failed')},
    {'statuses': ('failed', 'searching')},
    {'response_status': 'failed'},
    {'response_status': 'incomplete'},
    {'citations': 0},
])
def test_capped_lookup_still_rejects_unverified_evidence_without_retry(options):
    def respond(payload, number):
        if number in (1, 3):
            return response([tool_call('search_public_dining', {'topic': 'location'}, number)])
        if is_search(payload):
            return capped_search(**options)
        assert len(tool_outputs(payload)) == 2
        assert all('could not verify' in output for output in tool_outputs(payload))
        return answer('Current public information could not be verified.')

    turn, events, requests = exercise(respond)
    assert len(requests) == 4 and sum(map(is_search, requests)) == 1
    assert events[-1].type == 'complete' and events[-1].data.citations == []
    assert turn.context.client.queries == [] and turn.context.proposal is None
