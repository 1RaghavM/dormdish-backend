"""Independent trust-boundary tests for the shared agent v1 contract."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dormdish_agent.models import (
    ConfirmActionRequest, ConversationState, MealItem, MealProposal, Nutrition, RunState, TurnRequest,
    agent_event_adapter,
)


REQUEST_ID = "11111111-1111-4111-8111-111111111111"


def turn(**changes):
    return {"request_id": REQUEST_ID, "text": "Find dinner", **changes}


@pytest.mark.parametrize("text", ["x", "x" * 4000, "  Find dinner 🍲  "])
def test_turn_accepts_bounded_text_and_preserves_unicode(text):
    request = TurnRequest.model_validate(turn(text=text))
    assert request.text == text.strip()
    assert str(request.request_id) == REQUEST_ID


@pytest.mark.parametrize("text", ["", " \t\n", "x" * 4001, None, 42])
def test_turn_rejects_empty_oversized_or_nontext_input(text):
    with pytest.raises(ValidationError):
        TurnRequest.model_validate(turn(text=text))


@pytest.mark.parametrize("request_id", ["not-a-uuid", "", None, 42])
def test_turn_requires_a_recoverable_uuid(request_id):
    with pytest.raises(ValidationError):
        TurnRequest.model_validate(turn(request_id=request_id))


@pytest.mark.parametrize("field", ["user_id", "subscription", "permissions", "tool_arguments"])
def test_client_cannot_supply_authority_fields(field):
    with pytest.raises(ValidationError):
        TurnRequest.model_validate(turn(**{field: "untrusted"}))


def test_planning_context_rejects_embedded_owner():
    with pytest.raises(ValidationError):
        TurnRequest.model_validate(turn(context={
            "dining_hall": "Busch Dining Hall", "date": "2026-10-10",
            "meal_type": "dinner", "user_id": REQUEST_ID,
        }))


@pytest.mark.parametrize("version", [0, -1, 1.5, "1", True, None])
def test_confirmation_rejects_invalid_or_coerced_proposal_versions(version):
    with pytest.raises(ValidationError):
        ConfirmActionRequest(request_id=REQUEST_ID, proposal_version=version)


@pytest.mark.parametrize("field", ["user_id", "proposal", "items", "tool_arguments"])
def test_confirmation_accepts_only_a_reference_to_server_held_action(field):
    with pytest.raises(ValidationError):
        ConfirmActionRequest.model_validate({
            "request_id": REQUEST_ID, "proposal_version": 1, field: {},
        })


def test_confirmation_preserves_exact_request_and_version():
    request = ConfirmActionRequest(request_id=REQUEST_ID, proposal_version=2)
    assert request.model_dump(mode="json") == {
        "request_id": REQUEST_ID, "proposal_version": 2,
    }


@pytest.fixture
def proposal():
    # Item amounts are already scaled: multiplying the first item by 2 again is wrong.
    return {
        "id": REQUEST_ID, "version": 1, "dining_hall": "Busch Dining Hall",
        "date": "2026-10-10", "meal_type": "dinner", "description": "Dinner",
        "items": [
            {"menu_item_id": "chicken", "name": "Chicken", "serving_size": "100 g",
             "servings": 2, "calories": 300, "protein_g": 50, "fat_g": 8, "carbs_g": 0},
            {"menu_item_id": "rice", "name": "Rice", "serving_size": "1 cup",
             "servings": 0.5, "calories": 100, "protein_g": 2, "fat_g": 0, "carbs_g": 22},
        ],
        "totals": {"calories": 400, "protein_g": 52, "fat_g": 8, "carbs_g": 22},
    }


def test_proposal_keeps_selected_date_and_already_scaled_item_totals(proposal):
    result = MealProposal.model_validate(proposal)
    assert result.model_dump(mode="json") == proposal


@pytest.mark.parametrize("servings", [0, -1, 20.001, float("inf"), float("-inf"), float("nan"), "1", True])
def test_portions_must_be_finite_positive_bounded_numbers(proposal, servings):
    with pytest.raises(ValidationError):
        MealItem.model_validate({**proposal["items"][0], "servings": servings})


@pytest.mark.parametrize("servings", [0.01, 1, 20])
def test_valid_fractional_and_boundary_portions(proposal, servings):
    assert MealItem.model_validate({**proposal["items"][0], "servings": servings}).servings == servings


@pytest.mark.parametrize("nutrient", ["calories", "protein_g", "fat_g", "carbs_g"])
@pytest.mark.parametrize("amount", [-1, 100001, float("inf"), float("nan"), "100", True])
def test_nutrition_rejects_unsafe_or_coerced_amounts(proposal, nutrient, amount):
    with pytest.raises(ValidationError):
        Nutrition.model_validate({**proposal["totals"], nutrient: amount})


@pytest.mark.parametrize("nutrient", ["calories", "protein_g", "fat_g", "carbs_g"])
def test_stale_totals_after_a_substitution_are_rejected(proposal, nutrient):
    proposal["items"][0][nutrient] += 10
    with pytest.raises(ValidationError):
        MealProposal.model_validate(proposal)


def test_duplicate_menu_identifiers_rejected_even_with_consistent_totals(proposal):
    proposal["items"][1]["menu_item_id"] = proposal["items"][0]["menu_item_id"]
    with pytest.raises(ValidationError):
        MealProposal.model_validate(proposal)


def event(kind="status", sequence=1, **changes):
    payload = {
        "status": {"message": "Checking dinner options"},
        "complete": {"saved_plan_id": None, "preferences_changed": False, "citations": []},
        "error": {"code": "timeout", "message": "Try again", "retryable": True},
    }
    return {"version": 1, "sequence": sequence, "run_id": REQUEST_ID,
            "type": kind, "data": payload[kind], **changes}


def run(events, status="running"):
    return {"id": REQUEST_ID, "conversation_id": "22222222-2222-4222-8222-222222222222",
            "status": status, "events": events}


@pytest.mark.parametrize("changes", [
    {"version": 2}, {"sequence": 0}, {"sequence": "1"}, {"sequence": True},
    {"run_id": "invalid"}, {"type": "unknown"}, {"data": {}}, {"user_id": REQUEST_ID},
])
def test_event_envelope_rejects_invalid_metadata_or_payload(changes):
    with pytest.raises(ValidationError):
        agent_event_adapter.validate_python(event(**changes))


@pytest.mark.parametrize("status", ["complete", "error"])
def test_recovered_terminal_run_preserves_outcome(status):
    data = run([event(), event(status, 2)], status)
    assert RunState.model_validate(data).model_dump(mode="json") == data


@pytest.mark.parametrize("events", [[], [event()]])
def test_unfinished_run_remains_running_and_never_implies_success(events):
    assert RunState.model_validate(run(events)).status == "running"
    with pytest.raises(ValidationError):
        RunState.model_validate(run(events, "complete"))


@pytest.mark.parametrize("events,status", [
    ([event(sequence=2)], "running"),
    ([event(), event(sequence=1)], "running"),
    ([event(run_id="33333333-3333-4333-8333-333333333333")], "running"),
    ([event("complete"), event(sequence=2)], "complete"),
    ([event("complete"), event("complete", 2)], "complete"),
    ([event("error"), event("complete", 2)], "complete"),
    ([event("complete")], "running"),
    ([event("error")], "complete"),
])
def test_recovery_rejects_wrong_run_gaps_duplicate_terminals_or_outcome(events, status):
    with pytest.raises(ValidationError):
        RunState.model_validate(run(events, status))


@pytest.mark.parametrize("items", [[], 31])
def test_proposal_rejects_empty_or_oversized_meals(proposal, items):
    if items == 31:
        proposal["items"] = [{**proposal["items"][0], "menu_item_id": f"item-{i}"} for i in range(31)]
        proposal["totals"] = {key: proposal["items"][0][key] * 31 for key in proposal["totals"]}
    else:
        proposal["items"] = items
        proposal["totals"] = {key: 0 for key in proposal["totals"]}
    with pytest.raises(ValidationError):
        MealProposal.model_validate(proposal)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "not a URL"])
def test_citations_only_allow_clickable_web_sources(url):
    data = event("complete")
    data["data"]["citations"] = [{"title": "Campus dining", "url": url}]
    with pytest.raises(ValidationError):
        agent_event_adapter.validate_python(data)


def test_completion_preserves_canonical_saved_id_and_citation():
    data = event("complete")
    data["data"].update(saved_plan_id=REQUEST_ID, citations=[{
        "title": "Campus dining", "url": "https://example.edu/dining",
    }])
    assert agent_event_adapter.validate_python(data).model_dump(mode="json") == data


def test_shared_fixture_roundtrips_all_events_and_restored_pending_action():
    fixture = json.loads((Path(__file__).parents[1] / "contracts/agent-v1.json").read_text())
    for key, model in [("turn", TurnRequest), ("confirm", ConfirmActionRequest),
                       ("conversation", ConversationState), ("run", RunState)]:
        assert model.model_validate(fixture[key]).model_dump(mode="json") == fixture[key]
    events = fixture["run"]["events"] + [fixture["error_event"]]
    assert {item["type"] for item in events} == {
        "status", "text_delta", "meal_preview", "action_required", "complete", "error",
    }
    for item in events:
        assert agent_event_adapter.validate_json(json.dumps(item)).model_dump(mode="json") == item
    action = fixture["conversation"]["pending_actions"][0]
    assert action == fixture["run"]["events"][3]["data"]["action"]
    assert "🌶️" in fixture["run"]["events"][1]["data"]["text"]


def test_real_sdk_import_and_required_offline_interfaces():
    from importlib.metadata import distribution
    import agents
    from agents import Agent, RunConfig, RunResult, Runner, WebSearchTool

    assert Path(agents.__file__).resolve() == Path(
        distribution("openai-agents").locate_file("agents/__init__.py")
    ).resolve()
    assert callable(Runner.run_streamed)
    assert callable(RunResult.to_input_list)
    assert RunConfig(tracing_disabled=True).tracing_disabled
    assert Agent(name="offline-smoke", model="gpt-6-luna", tools=[WebSearchTool()]).model == "gpt-6-luna"


@pytest.mark.parametrize("date", [0, "2026-10-10T00:00:00"])
def test_client_planning_date_must_be_a_calendar_date_string(date):
    with pytest.raises(ValidationError):
        TurnRequest.model_validate(turn(context={"dining_hall": "Busch", "date": date}))


@pytest.mark.parametrize("version", [True, 1.0])
def test_event_version_rejects_noninteger_json_values(version):
    with pytest.raises(ValidationError):
        agent_event_adapter.validate_python(event(version=version))


@pytest.mark.parametrize("kind,field", [("complete", "preferences_changed"), ("error", "retryable")])
@pytest.mark.parametrize("value", [0, "false"])
def test_terminal_flags_reject_coercion(kind, field, value):
    data = event(kind)
    data["data"][field] = value
    with pytest.raises(ValidationError):
        agent_event_adapter.validate_python(data)
