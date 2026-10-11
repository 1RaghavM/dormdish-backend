"""Shared agent generation behind the four legacy Generate Meal routes; callers keep their own saves."""
import asyncio
from contextlib import aclosing
import os
import time
from uuid import uuid4

from openai import AsyncOpenAI

from dormdish_agent.models import Nutrition, TurnRequest
from dormdish_agent.routes import build_context
from dormdish_agent.runtime import AgentTurn

DEADLINE_SECONDS = 45


def enabled():
    return (os.getenv('DORMDISH_AGENT_GENERATION_ENABLED', 'false') == 'true'
            and os.getenv('DORMDISH_AGENT_KILL_SWITCH', 'false') != 'true')


async def events(client, user, get_targets, dining_hall, date, targets, restaurant=None):
    """Yield legacy `thinking` chunks, then one `complete` chunk; raise before any write on failure.

    `targets` maps each requested meal, in order, to its legacy per-meal target hint.
    One deadline, model-call budget and validation repair are shared by every meal.
    """
    meals = list(targets)
    if not meals:
        raise ValueError('No meals requested')
    deadline = time.monotonic() + DEADLINE_SECONDS
    model_budget, attempts_left = min(16, 8 * len(meals)), len(meals) + 1
    plan, texts = {}, []
    openai = AsyncOpenAI(max_retries=0, timeout=30)
    try:
        for index, meal in enumerate(meals):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or model_budget <= 0:
                raise ValueError('Generation budget exhausted')
            hint = targets[meal]
            request = TurnRequest(request_id=uuid4(), context={'dining_hall': dining_hall, 'date': date, 'meal_type': meal},
                text=f'Generate my {meal} from the current menu. Aim for about {hint["calories"]} kcal, '
                     f'{hint["protein_g"]} g protein, {hint["fat_g"]} g fat and {hint["carbs_g"]} g carbs. '
                     'Return one validated meal preview.')
            context = await asyncio.to_thread(build_context, client, user, request, get_targets)
            context.restaurant = restaurant
            context.model_limit = min(8, model_budget)
            context.validation_limit = min(2, attempts_left - (len(meals) - index - 1))
            turn = AgentTurn(context, request, uuid4(), [], client=openai, deadline_seconds=min(DEADLINE_SECONDS, remaining))
            async with aclosing(turn.stream()) as stream:
                async for event in stream:
                    if event.type == 'status':
                        yield {'type': 'thinking', 'content': event.data.message}
            model_budget -= context.model_calls
            attempts_left -= context.validation_attempts
            proposal = context.proposal
            if turn.state.status != 'complete' or proposal is None or proposal.meal_type != meal:
                raise ValueError('No validated meal')
            texts.append(turn.assistant_text.strip())
            plan[meal] = {'description': texts[-1][:4000],
                          'items': [item.model_dump(mode='json') for item in proposal.items]}
    finally:
        await openai.close()
    items = [item for meal in plan.values() for item in meal['items']]
    totals = Nutrition(**{key: sum(item[key] for item in items) for key in Nutrition.model_fields}).model_dump()
    explanation = '\n\n'.join(text for text in texts if text)
    yield {'type': 'complete', 'meal_plan': {'meals': plan, 'totals': totals, 'explanation': explanation},
           'raw_response': explanation}


async def generate(*args, **kwargs):
    """Non-streaming form of `events`: returns `(meal_plan, raw_text)`."""
    async for chunk in events(*args, **kwargs):
        pass
    return chunk['meal_plan'], chunk['raw_response']
