"""Owner-scoped, versioned checkoffs. Nutrition comes only from the saved snapshot."""
import asyncio
import os
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator

from dormdish_agent.models import CalendarDate, Contract, MealType, Nutrition
from dormdish_agent.routes import BoundedAgentRoute


class SelectedItem(Contract):
    meal_type: MealType
    index: Annotated[int, Field(strict=True, ge=0, lt=30)]


class CompletionRequest(Contract):
    request_id: UUID
    date: CalendarDate
    plan_version: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=2147483646)]
    mode: Literal['current', 'frozen'] = 'current'
    selected_items: Annotated[list[SelectedItem], Field(max_length=90)]

    @model_validator(mode='after')
    def unique_selection(self):
        keys = [(item.meal_type, item.index) for item in self.selected_items]
        if len(keys) != len(set(keys)):
            raise ValueError('Duplicate selected food')
        self.selected_items.sort(key=lambda item: (item.meal_type, item.index))
        return self


def enabled():
    return os.getenv('DORMDISH_COMPLETION_ENABLED', 'false') == 'true'


def saved_items(row):
    """Existing saved item amounts already include servings; do not multiply twice."""
    data = row['meal_plan_data']
    if not isinstance(data, dict) or not isinstance(data.get('meal_plan', data), dict):
        raise ValueError('Unsupported saved meal format')
    meals = data.get('meal_plan', data).get('meals')
    if not isinstance(meals, dict) or set(meals) - {'breakfast', 'lunch', 'dinner'}:
        raise ValueError('Unsupported saved meal format')
    result = []
    for meal, value in meals.items():
        if not isinstance(value, dict):
            raise ValueError('Unsupported saved meal format')
        foods = value.get('items')
        if not isinstance(foods, list) or not 1 <= len(foods) <= 30:
            raise ValueError('Unsupported saved food list')
        for index, item in enumerate(foods):
            if not isinstance(item, dict):
                raise ValueError('Unsupported saved food')
            name = item.get('name')
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
                raise ValueError('Invalid saved food name')
            totals = Nutrition(**{key: item.get(key) for key in Nutrition.model_fields})
            result.append({'meal_type': meal, 'index': index, 'name': name,
                'totals': totals.model_dump(),
                'source_id': f"completion:{row['id']}:{row['completion_version']}:{meal}:{index}"})
    if not result:
        raise ValueError('Saved meal has no foods')
    return result


class CompletionStore:
    def __init__(self, client, user_id):
        self.client, self.user_id = client, str(UUID(str(user_id)))

    def rpc(self, name, **params):
        try:
            return self.client.rpc(name, {'p_user_id': self.user_id, **params}).execute().data
        except Exception as error:
            message = getattr(error, 'message', '')
            code = next((code for code in ['not_found', 'conflict', 'stale_plan', 'quota', 'invalid']
                         if message == 'completion_' + code), None)
            raise HTTPException({'not_found': 404, 'conflict': 409, 'stale_plan': 409,
                                 'quota': 429, 'invalid': 422}.get(code, 503),
                                'Meal completion ' + (code or 'unavailable; reconcile before retrying')) from None

    def current(self, plan_id):
        result = self.rpc('meal_completion_current', p_plan_id=str(plan_id))
        try:
            result['available_items'] = saved_items(result['plan'])
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, 'Saved nutrition cannot be verified') from None
        return result

    def receipt(self, request_id):
        return self.rpc('meal_completion_request', p_request_id=str(request_id))

    def list(self, date):
        return self.rpc('meal_completion_list', p_date=date.isoformat())

    def write(self, plan_id, body):
        request = {'plan_id': str(plan_id), **body.model_dump(mode='json')}
        receipt = self.receipt(body.request_id)
        if receipt:
            if receipt['request'] != request:
                raise HTTPException(409, 'Meal completion conflict')
            return receipt['state']
        if body.mode == 'current':
            current = self.current(plan_id)
            row = current['plan']
            if row['completion_version'] != str(body.plan_version) or row['date'] != body.date.isoformat():
                raise HTTPException(409, 'Meal completion stale_plan')
            catalog = current['available_items']
        else:
            catalog = []  # The RPC uses only previously eaten, frozen foods for this version.
        return self.rpc('meal_completion_write', p_plan_id=str(plan_id), p_request=request,
                        p_items=catalog)


def create_router(get_current_user, get_client):
    router = APIRouter(tags=['Meal completion'], route_class=BoundedAgentRoute)

    def owner(user=Depends(get_current_user)):
        return CompletionStore(get_client(), user['id'])

    async def call(function, *args):
        try:
            return await asyncio.wait_for(asyncio.to_thread(function, *args), timeout=10)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, 'Meal completion unavailable; reconcile before retrying') from None

    @router.get('/meal-plan/{plan_id}/completion')
    async def current(plan_id: UUID, store=Depends(owner)):
        return await call(store.current, plan_id)

    @router.get('/meal-completions')
    async def by_date(date: CalendarDate, store=Depends(owner)):
        return await call(store.list, date)

    @router.get('/meal-completion-requests/{request_id}')
    async def receipt(request_id: UUID, store=Depends(owner)):
        result = await call(store.receipt, request_id)
        if result is None:
            raise HTTPException(404, 'Meal completion request not found')
        return result['state']

    @router.put('/meal-plan/{plan_id}/completion')
    async def write(plan_id: UUID, body: CompletionRequest, store=Depends(owner)):
        if not enabled():
            raise HTTPException(503, 'Meal completion is currently unavailable')
        return await call(store.write, plan_id, body)

    return router
