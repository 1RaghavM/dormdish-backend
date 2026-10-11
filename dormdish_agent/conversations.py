"""Owner-scoped RPCs. Construct only from the authenticated session, never tool args.

Database writes are deliberately not retried: reconcile get_run(request_id) after
an ambiguous response. Meals passed to finish_turn must already be menu validated.
"""
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from dormdish_agent.models import (
    ConfirmActionRequest, ConversationState, PendingAction, RunState, TurnRequest,
)


class StorageError(Exception):
    """Safe code suitable for mapping to HTTP errors before starting SSE."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StoredAction:
    action: PendingAction
    # Exact existing saved_meal_plans.meal_plan_data built by the trusted service.
    save_payload: dict[str, Any]
    target_plan_id: UUID | None = None
    expected_plan_version: str | None = None
    expected_profile_version: str | None = None

    def payload(self):
        if (self.target_plan_id is None) != (self.expected_plan_version is None):
            raise ValueError("Replacement requires both target ID and stored version")
        return {
            "action": self.action.model_dump(mode="json"),
            "save_payload": self.save_payload,
            "target_plan_id": str(self.target_plan_id) if self.target_plan_id else None,
            "expected_plan_version": self.expected_plan_version,
            "expected_profile_version": self.expected_profile_version,
        }


class AgentStore:
    def __init__(self, client, authenticated_user_id: UUID | str):
        self.client = client
        self.user_id = str(UUID(str(authenticated_user_id)))

    def _rpc(self, name, **params):
        try:
            return self.client.rpc("agent_" + name, {"p_user_id": self.user_id, **params}).execute().data
        except Exception as exc:
            message = getattr(exc, "message", "")
            if message in {
                "agent_not_found", "agent_conflict", "agent_busy", "agent_quota",
                "agent_expired", "agent_invalid", "agent_stale_plan", "agent_stale_profile",
            }:
                raise StorageError(message.removeprefix("agent_")) from None
            # Do not expose PostgREST's request details, SQL, or private data.
            raise StorageError("unavailable") from None

    def create_conversation(self) -> ConversationState:
        return ConversationState.model_validate(self._rpc("create_conversation"))

    def get_conversation(self, conversation_id: UUID) -> ConversationState:
        return ConversationState.model_validate(self._rpc("get_conversation", p_conversation_id=str(conversation_id)))

    def load_history(self, conversation_id: UUID) -> list[dict]:
        # One continuation mechanism: replay this SDK to_input_list(), never transcripts.
        return self._rpc("get_conversation", p_conversation_id=str(conversation_id), p_history=True)

    def claim_turn(self, conversation_id: UUID, request: TurnRequest) -> tuple[bool, RunState]:
        data = self._rpc("claim_turn", p_conversation_id=str(conversation_id),
                         p_request_id=str(request.request_id), p_request=request.model_dump(mode="json"))
        return data["claimed"], RunState.model_validate(data["run"])

    def apply_memory_turn(self, conversation_id: UUID, request_id: UUID) -> RunState:
        return RunState.model_validate(self._rpc("memory_turn", p_conversation_id=str(conversation_id),
                                                 p_request_id=str(request_id)))

    def get_run(self, request_id: UUID) -> RunState:
        return RunState.model_validate(self._rpc("get_run", p_request_id=str(request_id)))

    def finish_turn(self, run: RunState, history: list[dict], assistant_text: str = "",
                    action: StoredAction | None = None) -> RunState:
        if run.status == "running":
            raise ValueError("A finish requires a terminal outcome")
        if len(assistant_text) > 64000 or len(run.events) > 512:
            raise ValueError("Outcome exceeds storage bounds")
        if not isinstance(history, list):
            raise ValueError("SDK history must be a list")
        # Validate JSON without permitting NaN; the database bounds whole replay.
        json.dumps(history, allow_nan=False)
        events = [event.model_dump(mode="json") for event in run.events]
        pending = action.payload() if action else None
        if len(json.dumps(events).encode()) > 524288 or (pending and len(json.dumps(pending, allow_nan=False).encode()) > 262144):
            raise ValueError("Outcome exceeds storage bounds")
        return RunState.model_validate(self._rpc("finish_turn", p_conversation_id=str(run.conversation_id),
            p_request_id=str(run.id), p_events=events, p_history=history,
            p_assistant_text=assistant_text, p_action=pending))

    def get_profile(self) -> dict:
        """Current planning inputs and an opaque SQL fingerprint of that exact snapshot."""
        return self._rpc("get_profile")

    def get_saved_plan(self, plan_id: UUID) -> dict:
        """Returns {plan, version}; pass this opaque version back for replacement."""
        return self._rpc("get_plan", p_plan_id=str(plan_id))

    def confirm_action(self, action_id: UUID, request: ConfirmActionRequest) -> RunState:
        return RunState.model_validate(self._rpc("confirm_action", p_action_id=str(action_id),
            p_request_id=str(request.request_id), p_proposal_version=request.proposal_version))

    def delete_conversation(self, conversation_id: UUID) -> None:
        self._rpc("delete_conversation", p_conversation_id=str(conversation_id))

    def clear_history(self) -> None:
        """Call after forgetting. Never repopulate SDK history from old messages."""
        self._rpc("clear_history")
