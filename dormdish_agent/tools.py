"""Read-only SDK tools. Identity and hard constraints come from the authenticated service."""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from uuid import UUID

from agents import RunContextWrapper, function_tool

from dormdish_agent.models import MealProposal, Nutrition, PlanningContext
from services.meal_planning import MenuSnapshot, Selection, build_menu_snapshot, remaining_targets, validate_meal

SearchTopic = Literal['opening_hours', 'location', 'dining_policies']


@dataclass
class FoodContext:
    client: Any
    user_id: str
    university: str
    planning: PlanningContext
    targets: Nutrition
    restrictions: tuple[str, ...] = ()
    allergens: tuple[str, ...] = ()
    preferences: dict = field(default_factory=dict)
    restaurant: str | None = None
    model_limit: int = 8
    validation_limit: int = 2
    snapshot: MenuSnapshot | None = field(default=None, init=False)
    proposal: MealProposal | None = field(default=None, init=False)
    validation_attempts: int = field(default=0, init=False)
    tool_calls: int = field(default=0, init=False)
    model_calls: int = field(default=0, init=False)
    public_search: Callable[[SearchTopic], Awaitable[dict]] | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self.user_id = str(UUID(self.user_id))
        # Use the same checked-in campus/hall registry as the app; never guess aliases.
        campuses = json.loads((Path(__file__).resolve().parents[1] / 'universities_config.json').read_text())['universities']
        campus = next((c for c in campuses if self.university in (c['id'], c['university'])), None)
        hall = next((h for h in campus['dining_halls'] if self.planning.dining_hall in
                     (h['id'], h['name'], h['slug'])), None) if campus else None
        if not hall or not self.planning.meal_type:
            raise ValueError('A supported campus, hall, and meal are required')
        self.university = campus['university']
        self.planning = self.planning.model_copy(update={'dining_hall': hall['slug']})

    def claim_tool(self):
        self.tool_calls += 1
        if self.tool_calls > 16:
            raise ValueError('Tool budget exhausted')

    def claim_model(self):
        self.model_calls += 1
        if self.model_calls > self.model_limit:
            raise ValueError('Model request budget exhausted')

    async def menu(self) -> MenuSnapshot:
        def read():
            return (self.client.table('menus').select('*').eq('university', self.university)
                    .eq('dining_hall', self.planning.dining_hall).eq('date', self.planning.date.isoformat())
                    .order('fetched_at', desc=True).limit(10).execute().data or [])
        rows = await asyncio.to_thread(read)
        for row in rows:
            if row.get('meal') not in (None, self.planning.meal_type):
                continue
            if self.restaurant:
                items = (row.get('menu_data') or {}).get('items') or []
                row = {**row, 'menu_data': {'items': [i for i in items if isinstance(i, dict)
                       and str(i.get('restaurant') or '').lower() == self.restaurant.lower()]}}
            self.snapshot = build_menu_snapshot(row, university=self.university, context=self.planning,
                                                now=datetime.now(timezone.utc), max_age=timedelta(hours=24))
            if len(json.dumps(self.snapshot.items).encode()) > 128_000:
                raise ValueError('Menu exceeds the safe context size')
            return self.snapshot
        raise ValueError('No current menu is available for this meal')

    async def intake(self) -> dict:
        from services.meal_completion import CompletionStore, enabled
        if enabled():
            evidence = await asyncio.to_thread(CompletionStore(self.client, self.user_id).rpc,
                "meal_completion_intake", p_date=self.planning.date.isoformat())
            remaining = remaining_targets(self.targets, evidence["food_logs"], evidence["completions"],
                user_id=self.user_id, requested_date=self.planning.date)
            return {"remaining_targets": remaining.model_dump(), "completion_records_available": True,
                    "limitation": "Only logged food and server-synced checkoffs are counted; unlogged food is unknown."}
        def read():
            return (self.client.table('food_log').select('id,user_id,date,calories,protein_g,fat_g,carbs_g')
                    .eq('user_id', self.user_id).eq('date', self.planning.date.isoformat())
                    .limit(501).execute().data or [])
        rows = await asyncio.to_thread(read)
        if len(rows) > 500:
            raise ValueError('Too many intake records to safely calculate the remaining target')
        remaining = remaining_targets(self.targets, rows, [], user_id=self.user_id,
                                      requested_date=self.planning.date)
        # shortcut: device meal completion is not synced, require server completion evidence before claiming full intake coverage.
        return {'remaining_after_food_logs': remaining.model_dump(), 'completion_records_available': False,
                'limitation': 'Meal completion is not synced. Ask about unlogged eaten meals; this is not a complete remaining target.'}

    def preview(self, selections: list[Selection]) -> dict:
        self.validation_attempts += 1
        self.proposal = None
        if self.validation_attempts > self.validation_limit:
            raise ValueError('The single meal-validation repair attempt was exhausted')
        if self.snapshot is None:
            raise ValueError('Retrieve the current menu before selecting a meal')
        self.proposal = validate_meal(self.snapshot, [s.model_dump() for s in selections],
                                     university=self.university, context=self.planning,
                                     now=datetime.now(timezone.utc), restrictions=self.restrictions,
                                     allergens=self.allergens)
        return self.proposal.model_dump(mode='json')


def _safe_tool_error(_context, _error):
    return 'This tool could not verify the requested data. Do not invent an answer; explain the limitation.'


@function_tool(failure_error_function=_safe_tool_error)
async def read_profile(context: RunContextWrapper[FoodContext]) -> dict:
    """Read current trusted goals, hard restrictions, and soft preferences without identifying profile data."""
    data = context.context
    data.claim_tool()
    return {'targets': data.targets.model_dump(), 'restrictions': data.restrictions,
            'allergens': data.allergens, 'preferences': data.preferences,
            'planning': data.planning.model_dump(mode='json')}


@function_tool(failure_error_function=_safe_tool_error)
async def retrieve_menu(context: RunContextWrapper[FoodContext]) -> dict:
    """Retrieve the authoritative menu for the server-selected campus, hall, date, and meal."""
    context.context.claim_tool()
    snapshot = await context.context.menu()
    return {'fetched_at': snapshot.fetched_at.isoformat(), 'items': snapshot.items}


@function_tool(failure_error_function=_safe_tool_error)
async def read_intake(context: RunContextWrapper[FoodContext]) -> dict:
    """Read owner-scoped food logs and their remaining targets, with explicit completion coverage."""
    context.context.claim_tool()
    return await context.context.intake()


@function_tool(failure_error_function=_safe_tool_error)
async def preview_meal(context: RunContextWrapper[FoodContext], selections: list[Selection]) -> dict:
    """Validate canonical menu selections and calculate nutrition. At most one repair; no save occurs."""
    context.context.claim_tool()
    return context.context.preview(selections)


@function_tool(failure_error_function=_safe_tool_error)
async def search_public_dining(context: RunContextWrapper[FoodContext], topic: SearchTopic) -> dict:
    """Look up public hall hours, location or dining policies once. Never establishes selectable foods or allergy safety."""
    context.context.claim_tool()
    if context.context.public_search is None:
        raise ValueError('Public search is unavailable')
    return await context.context.public_search(topic)


READ_TOOLS = [read_profile, retrieve_menu, read_intake, preview_meal, search_public_dining]
