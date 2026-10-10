"""Authenticated v1 endpoints. Rollback keeps reads and deletion available."""
import asyncio
from hashlib import sha256
import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from dormdish_agent.conversations import AgentStore, StorageError
from dormdish_agent.models import Contract


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
    except StorageError as exc:
        code = {'not_found': 404, 'conflict': 409, 'busy': 409, 'quota': 429,
                'expired': 409, 'invalid': 422, 'stale_plan': 409}.get(exc.code, 503)
        raise HTTPException(code, 'Agent request ' + exc.code) from None
    except Exception:
        raise HTTPException(503, 'Agent storage unavailable; reconcile before retrying') from None


def create_router(get_current_user, require_premium, get_client, get_targets):
    router = APIRouter(prefix='/agent', tags=['Agent'])

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

    return router
