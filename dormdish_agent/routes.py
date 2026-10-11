"""Authenticated v1 endpoints. Rollback keeps reads and deletion available."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import anyio
from hashlib import sha256
import os
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI

from dormdish_agent.conversations import AgentStore, StorageError, StoredAction
from dormdish_agent.models import (
    Contract, ConfirmActionRequest, Nutrition, PendingAction, RunState, TurnRequest, agent_event_adapter,
)
from dormdish_agent.runtime import AgentTurn
from dormdish_agent.tools import FoodContext


class CreateConversation(Contract):
    pass


def admit(user_id):
    """A stable server cohort; malformed configuration fails closed."""
    try:
        enabled = os.getenv('DORMDISH_AGENT_ENABLED', 'false')
        killed = os.getenv('DORMDISH_AGENT_KILL_SWITCH', 'false')
        percent = int(os.getenv('DORMDISH_AGENT_ROLLOUT_PERCENT', '0'))
        if enabled not in ('true', 'false') or killed not in ('true', 'false') or not 0 <= percent <= 100:
            raise ValueError()
        bucket = int.from_bytes(sha256(('dormdish-agent-v1:' + str(UUID(str(user_id)))).encode()).digest()[:4], 'big') % 100
    except ValueError:
        raise HTTPException(503, 'Agent rollout configuration is unavailable') from None
    if enabled != 'true' or killed == 'true' or bucket >= percent:
        raise HTTPException(503, 'DormDish conversation is currently unavailable')


async def storage_call(function, *args):
    try:
        return await asyncio.wait_for(asyncio.to_thread(function, *args), timeout=10)
    except HTTPException:
        raise
    except StorageError as exc:
        code = {'not_found': 404, 'conflict': 409, 'busy': 409, 'quota': 429,
                'expired': 409, 'invalid': 422, 'stale_plan': 409, 'stale_profile': 409}.get(exc.code, 503)
        raise HTTPException(code, 'Agent request ' + exc.code) from None
    except Exception:
        raise HTTPException(503, 'Agent storage unavailable; reconcile before retrying') from None


def create_router(get_current_user, require_premium, get_client, get_targets):
    router = APIRouter(prefix='/agent', tags=['Agent'], route_class=BoundedAgentRoute)

    def store(user=Depends(get_current_user)):
        return AgentStore(get_client(), user['id'])

    def enabled_user(user=Depends(require_premium)):
        admit(user['id'])
        return user

    @router.post('/conversations', status_code=201)
    async def create(body: CreateConversation, user=Depends(enabled_user), owner=Depends(store)):
        return await storage_call(owner.create_conversation)

    @router.get('/conversations/{conversation_id}')
    async def restore(conversation_id: UUID, owner=Depends(store)):
        return await storage_call(owner.get_conversation, conversation_id)

    @router.delete('/conversations/{conversation_id}', status_code=204)
    async def delete(conversation_id: UUID, owner=Depends(store)):
        await storage_call(owner.delete_conversation, conversation_id)
        return Response(status_code=204)

    @router.get('/runs/{request_id}')
    async def reconcile(request_id: UUID, owner=Depends(store)):
        return await storage_call(owner.get_run, request_id)

    @router.post('/actions/{action_id}/confirm')
    async def confirm(action_id: UUID, body: ConfirmActionRequest, user=Depends(enabled_user), owner=Depends(store)):
        try:
            return await storage_call(owner.confirm_action, action_id, body)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            # An RPC response can be lost after commit. Only read the result; never repeat the write.
            try:
                return await storage_call(owner.get_run, body.request_id)
            except HTTPException:
                raise exc from None

    @router.post('/conversations/{conversation_id}/messages')
    async def message(conversation_id: UUID, body: TurnRequest, user=Depends(enabled_user), owner=Depends(store)):
        # Ownership and quota errors stay HTTP errors, before opening the stream.
        claimed, state = await storage_call(owner.claim_turn, conversation_id, body)
        if not claimed:
            if state.status == 'running':
                raise HTTPException(409, 'Request is running; reconcile using its request ID')
            return StreamingResponse(replay(state.events), media_type='text/event-stream', headers=STREAM_HEADERS)
        finish_attempted = False
        try:
            history = await storage_call(owner.load_history, conversation_id)
            if body.context is None or body.context.meal_type is None:
                text = 'Choose a dining hall, date, and breakfast, lunch, or dinner so I can check the right menu.'
                append_event(state, 'text_delta', {'text': text})
                append_event(state, 'complete', {'citations': []})
                finish_attempted = True
                state = await storage_call(owner.finish_turn, state, [], text)
                return StreamingResponse(replay(state.events), media_type='text/event-stream', headers=STREAM_HEADERS)
            profile = await storage_call(owner.get_profile)
            trusted_user = {**profile['profile'], 'id': owner.user_id}
            context = await asyncio.wait_for(asyncio.to_thread(build_context, get_client(), trusted_user, body, get_targets), 10)
            target = await storage_call(read_target, owner, context)
        except asyncio.CancelledError:
            if not finish_attempted:
                await finish_failure(owner, state, 'cancelled', 'This request was cancelled.')
            raise
        except Exception as exc:
            if not finish_attempted:
                await finish_failure(owner, state, 'context_unavailable', 'The current planning context could not be verified.')
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(422, 'Select a supported meal and verify your dietary profile') from None
        return StreamingResponse(run_stream(owner, state, body, context, history, target, profile['version']),
                                 media_type='text/event-stream', headers=STREAM_HEADERS)

    return router


STREAM_HEADERS = {'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'}


class BoundedAgentRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()
        async def bounded(request: Request):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 32768:
                    raise HTTPException(413, 'Agent request is too large')
            request._body = bytes(body)
            try:
                return await handle(request)
            except RequestValidationError:
                raise HTTPException(422, 'Invalid agent request') from None
        return bounded


def append_event(state, kind, data):
    event = agent_event_adapter.validate_python({'run_id': state.id, 'sequence': len(state.events) + 1,
                                                'type': kind, 'data': data})
    state.events.append(event)
    if kind in ('complete', 'error'):
        state.status = kind
    return event


def frame(event):
    return ('data: ' + event.model_dump_json() + '\n\n').encode('utf-8')


async def replay(events):
    for event in events:
        yield frame(event)


def build_context(client, user, request, get_targets):
    hard = user.get('dietary_restrictions') or []
    if not isinstance(hard, list) or any(not isinstance(value, str) for value in hard):
        raise ValueError('Unsupported dietary profile')
    preference = user.get('dietary_preference')
    if preference and preference.lower().strip() not in ('none', 'no preference', 'omnivore'):
        hard = hard + [preference]
    restrictions, allergens = [], []
    for value in hard:
        key = value.strip().lower().replace('-', '_').replace(' ', '_')
        if key in ('vegetarian', 'vegan', 'pescatarian', 'halal', 'kosher'):
            restrictions.append(key)
        elif key in {'dairy_free', 'gluten_free'}:
            allergens.append({'dairy_free': 'milk', 'gluten_free': 'gluten'}[key])
        elif key in ('milk', 'dairy', 'egg', 'eggs', 'peanut', 'peanuts', 'tree_nut', 'tree_nuts',
                     'soy', 'soya', 'soybean', 'soybeans', 'wheat', 'gluten', 'barley', 'rye',
                     'fish', 'shellfish', 'sesame'):
            allergens.append(key)
        else:
            raise ValueError('Unsupported dietary restriction')
    rows = (client.table('user_preferences').select('category,item_name,item_type,meal_type,reason')
            .eq('user_id', str(user['id'])).limit(201).execute().data or [])
    if len(rows) > 200 or len(json.dumps(rows).encode()) > 32000:
        raise ValueError('Preference context is too large')
    return FoodContext(client, str(user['id']), user['university'], request.context,
                       get_targets(user), tuple(restrictions), tuple(allergens), {'items': rows})


def read_target(owner, context):
    rows = (owner.client.table('saved_meal_plans').select('id,dining_hall').eq('user_id', owner.user_id)
            .eq('date', context.planning.date.isoformat()).limit(2).execute().data or [])
    if not rows:
        return None
    if len(rows) != 1:
        raise HTTPException(409, 'Multiple plans exist for this date; resolve the existing plans')
    target = owner.get_saved_plan(UUID(rows[0]['id']))
    try:
        # Reuse the authenticated campus registry; legacy IDs/names are not new halls.
        saved_context = replace(context, planning=context.planning.model_copy(
            update={'dining_hall': target['plan']['dining_hall']}))
        if (saved_context.planning.dining_hall != context.planning.dining_hall
                or target['plan']['date'] != context.planning.date.isoformat()):
            raise ValueError('Different plan context')
    except ValueError:
        raise HTTPException(409, 'A plan already exists for this date at another hall; use that plan') from None
    return target


def stored_action(context, target, expected_profile_version=None):
    proposal = context.proposal
    if target:
        # Preserve the legacy spelling so the transaction's exact hall/version guard still applies.
        proposal = proposal.model_copy(update={'dining_hall': target['plan']['dining_hall']})
    payload = deepcopy(target['plan']['meal_plan_data']) if target else {'success': True}
    plan = payload.setdefault('meal_plan', {})
    meals = plan.setdefault('meals', {})
    meals[proposal.meal_type] = {'description': proposal.description,
        'items': [item.model_dump(mode='json') for item in proposal.items]}
    # Recompute across preserved meals; never replace a day's totals with one meal's total.
    nutrition = [Nutrition(**{key: item[key] for key in Nutrition.model_fields})
                 for meal in meals.values() for item in meal['items']]
    plan['totals'] = Nutrition(**{key: sum(getattr(item, key) for item in nutrition)
                                 for key in Nutrition.model_fields}).model_dump()
    plan['explanation'] = proposal.description
    payload['nutrition_targets'] = context.targets.model_dump()
    if len(meals) == 1:
        payload['meal'] = proposal.meal_type
    else:
        payload.pop('meal', None)
    # An edited plan cannot submit feedback for the prior generator's unchanged output.
    payload.pop('generation_id', None)
    payload.pop('ask_feedback', None)
    action = PendingAction(id=uuid4(), proposal=proposal,
                           expires_at=min(datetime.now(timezone.utc) + timedelta(hours=24), context.snapshot.expires_at))
    return StoredAction(action, payload, UUID(target['plan']['id']) if target else None,
                        target['version'] if target else None, expected_profile_version)


async def finish_failure(owner, state, code, message):
    if state.status == 'running':
        append_event(state, 'error', {'code': code, 'message': message, 'retryable': False})
    with anyio.CancelScope(shield=True):
        try:
            await asyncio.wait_for(asyncio.to_thread(owner.finish_turn, state, [], ''), timeout=5)
        except Exception:
            pass  # Unknown outcome remains recoverable by request ID; never retry a write.


async def run_stream(owner, state, request, context, history, target, expected_profile_version=None):
    turn, iterator, client = None, None, None
    finish_attempted = False
    emitted = 0
    try:
        client = AsyncOpenAI(max_retries=0, timeout=30)
        turn = AgentTurn(context, request, state.conversation_id, history, client=client)
        iterator = turn.stream()
        async for event in iterator:
            if event.type in ('complete', 'error'):
                break
            emitted += 1
            yield frame(event)
        state = turn.state
        if state.status == 'running':
            raise ValueError('Runtime ended without a terminal event')
        action = None
        if state.status == 'complete' and context.proposal is not None:
            action = await asyncio.to_thread(stored_action, context, target, expected_profile_version)
            terminal = state.events.pop()
            state.status = 'running'
            append_event(state, 'action_required', {'action': action.action.model_dump(mode='json')})
            append_event(state, terminal.type, terminal.data.model_dump(mode='json'))
        finish_attempted = True
        try:
            result = await storage_call(owner.finish_turn, state, turn.history, turn.assistant_text, action)
        except HTTPException as exc:
            if exc.status_code != 409 or exc.detail != 'Agent request stale_profile':
                raise
            # This explicit rejection rolled back; persist a new terminal error, never retry an ambiguous write.
            state.events = state.events[:emitted]
            state.status = 'running'
            append_event(state, 'error', {'code': 'stale_profile',
                'message': 'Your profile changed. Request a new preview before applying a meal.', 'retryable': True})
            result = await storage_call(owner.finish_turn, state, [], '')
        if result.events[:emitted] == state.events[:emitted]:
            async for data in replay(result.events[emitted:]):
                yield data
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except Exception:
        # No unpersisted terminal event: abrupt EOF instructs clients to reconcile.
        pass
    finally:
        with anyio.CancelScope(shield=True):
            if iterator is not None:
                try:
                    await asyncio.wait_for(iterator.aclose(), timeout=5)
                except (Exception, asyncio.CancelledError):
                    pass
            if not finish_attempted:
                if turn is not None:
                    state = turn.state
                if state.status == 'complete':
                    state.events.pop()
                    if state.events and state.events[-1].type == 'action_required':
                        state.events.pop()
                    state.status = 'running'
                await finish_failure(owner, state, 'cancelled', 'This request stopped. Reconcile before retrying.')
            if client is not None:
                try:
                    await asyncio.wait_for(client.close(), timeout=5)
                except (Exception, asyncio.CancelledError):
                    pass
