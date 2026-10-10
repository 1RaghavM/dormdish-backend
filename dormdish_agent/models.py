"""Version 1 wire contracts. Ownership and menu validation belong to services."""
from datetime import date
from math import isclose
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, HttpUrl, StringConstraints,
    TypeAdapter, model_validator,
)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Hall = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
Amount = Annotated[float, Field(strict=True, ge=0, le=100_000, allow_inf_nan=False)]
MealType = Literal["breakfast", "lunch", "dinner"]


def calendar_date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("Expected YYYY-MM-DD calendar date")
    return date.fromisoformat(value)


CalendarDate = Annotated[date, BeforeValidator(calendar_date)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanningContext(Contract):
    dining_hall: Hall
    date: CalendarDate
    meal_type: MealType | None = None


class TurnRequest(Contract):
    request_id: UUID
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    context: PlanningContext | None = None


class ConfirmActionRequest(Contract):
    request_id: UUID
    proposal_version: PositiveInt


class Nutrition(Contract):
    calories: Amount
    protein_g: Amount
    fat_g: Amount
    carbs_g: Amount


class MealItem(Nutrition):
    menu_item_id: Name
    name: Name
    serving_size: Name
    servings: Annotated[float, Field(strict=True, gt=0, le=20, allow_inf_nan=False)]


class MealProposal(Contract):
    id: UUID
    version: PositiveInt
    dining_hall: Hall
    date: CalendarDate
    meal_type: MealType
    description: Annotated[str, Field(max_length=4000)]
    items: Annotated[list[MealItem], Field(min_length=1, max_length=30)]
    totals: Nutrition

    @model_validator(mode="after")
    def consistent_totals(self):
        if len({item.menu_item_id for item in self.items}) != len(self.items):
            raise ValueError("Duplicate menu item; combine servings first")
        # Item nutrition already includes servings; multiplying again double-counts.
        for nutrient in Nutrition.model_fields:
            expected = sum(getattr(item, nutrient) for item in self.items)
            if not isclose(getattr(self.totals, nutrient), expected, rel_tol=0, abs_tol=0.01):
                raise ValueError(f"Inconsistent {nutrient} total")
        return self


class PendingAction(Contract):
    id: UUID
    proposal: MealProposal
    expires_at: AwareDatetime


class Citation(Contract):
    title: Name
    url: HttpUrl


class StatusData(Contract):
    message: Name


class TextDeltaData(Contract):
    text: Annotated[str, Field(min_length=1, max_length=16000)]


class MealPreviewData(Contract):
    proposal: MealProposal


class ActionRequiredData(Contract):
    action: PendingAction


class CompleteData(Contract):
    saved_plan_id: UUID | None = None
    preferences_changed: Annotated[bool, Field(strict=True)] = False
    citations: Annotated[list[Citation], Field(max_length=20)] = Field(default_factory=list)


class ErrorData(Contract):
    code: Name
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    retryable: Annotated[bool, Field(strict=True)]


class EventEnvelope(Contract):
    version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    sequence: PositiveInt
    run_id: UUID


class StatusEvent(EventEnvelope):
    type: Literal["status"]
    data: StatusData


class TextDeltaEvent(EventEnvelope):
    type: Literal["text_delta"]
    data: TextDeltaData


class MealPreviewEvent(EventEnvelope):
    type: Literal["meal_preview"]
    data: MealPreviewData


class ActionRequiredEvent(EventEnvelope):
    type: Literal["action_required"]
    data: ActionRequiredData


class CompleteEvent(EventEnvelope):
    type: Literal["complete"]
    data: CompleteData


class ErrorEvent(EventEnvelope):
    type: Literal["error"]
    data: ErrorData


AgentEvent = Annotated[
    StatusEvent | TextDeltaEvent | MealPreviewEvent | ActionRequiredEvent | CompleteEvent | ErrorEvent,
    Field(discriminator="type"),
]
agent_event_adapter = TypeAdapter(AgentEvent)


class RunState(Contract):
    id: UUID
    conversation_id: UUID
    status: Literal["running", "complete", "error"]
    events: list[AgentEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered_events(self):
        terminal = None
        for sequence, event in enumerate(self.events, start=1):
            if terminal or event.sequence != sequence or event.run_id != self.id:
                raise ValueError("Run events must be ordered, owner-run matched, and terminal last")
            if event.type in {"complete", "error"}:
                terminal = event.type
        if (terminal or "running") != self.status:
            raise ValueError("Run status must match its terminal event")
        return self


class ConversationMessage(Contract):
    id: UUID
    role: Literal["user", "assistant"]
    text: Annotated[str, Field(min_length=1, max_length=64000)]
    created_at: AwareDatetime


class ConversationState(Contract):
    id: UUID
    messages: list[ConversationMessage] = Field(default_factory=list)
    pending_actions: list[PendingAction] = Field(default_factory=list)
    active_run_id: UUID | None = None
