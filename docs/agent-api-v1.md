# Agent API v1

Foundation only: these models and fixtures do not activate routes or rollout. `contracts/agent-v1.json` is the canonical synthetic fixture shared with Swift. `dormdish_agent/models.py` validates wire data; authenticated services must additionally enforce ownership, policy, menu grounding, freshness, and stored action versions.

## HTTP

All routes require the existing authenticated session. The server derives user ID, permissions, subscription, and university; clients/model arguments cannot override them. UUIDs are UUID strings, dates are university-local `YYYY-MM-DD`, timestamps have a UTC offset. JSON object fields not documented below are rejected on requests. Validate authorization, request size, rate limits, and ownership before opening SSE; use ordinary HTTP errors then (401/404/409/422/429). Cross-owner resources return 404. Responses may gain additive fields; clients ignore unknown fields.

| Route | Body | Response |
| --- | --- | --- |
| POST /agent/conversations | `{}` | 201 ConversationState |
| GET /agent/conversations/{id} | none | 200 ConversationState |
| POST /agent/conversations/{id}/messages | TurnRequest | 200 text/event-stream |
| GET /agent/runs/{id} | none | 200 RunState; ID is the original turn request ID |
| POST /agent/actions/{id}/confirm | ConfirmActionRequest | 200 RunState; ID is confirmation request ID |
| DELETE /agent/conversations/{id} | none | 204; deletes owned messages, runs and pending actions |

TurnRequest: `request_id` UUID, `text` trimmed nonblank string of at most 4000 characters, optional `context` with `dining_hall` (1–100 characters), `date`, and optional `meal_type` (`breakfast`, `lunch`, `dinner`). A duplicate request ID returns the original outcome; a conflicting payload returns 409. Request IDs are scoped to the authenticated owner and must not permit cross-conversation replay. Turns within a conversation are serialized.

ConfirmActionRequest contains only `request_id` UUID and positive integer `proposal_version`. The action ID selects a server-held proposal. Reject ownership/version/expiry conflicts before writing; repeated application returns the original saved plan ID. Client-supplied meals, users, nutrition, or tool arguments are forbidden. Explicit generate-and-save authorization is checked by the service; an exploratory preview is never an authorization to save.

ConversationState: `id`, ordered `messages` (`id`, user/assistant `role`, `text`, `created_at`), `pending_actions`, and nullable `active_run_id`. Do not return private SDK/tool history. RunState: `id`, `conversation_id`, `status` (`running`, `complete`, `error`), and the ordered complete event list. A running result may have no events. A terminal run has exactly one final matching terminal event. Retention and pagination require separate persistence implementation before launch; history returned to clients must remain bounded.

## SSE

Each frame is `data: <JSON>\n\n` encoded UTF-8. SSE comments may serve as keepalives. The JSON envelope is `{version:1, sequence:1, run_id:"UUID", type:"status", data:{...}}`. Sequence is a positive integer contiguous from 1 per run. The run ID equals the client turn request ID. The JSON `type` is authoritative; an optional SSE event field must agree.

| type | data |
| --- | --- |
| status | `{message}` — progress from actual SDK/tool execution, never hidden reasoning |
| text_delta | `{text}` — append exactly, including whitespace and Unicode |
| meal_preview | `{proposal}` |
| action_required | `{action}` — a pending action, not a terminal event |
| complete | `{saved_plan_id:null|UUID, preferences_changed:false|true, citations:[]}` |
| error | `{code, message, retryable:false|true}` — sanitized user-facing message |

A normal run sends exactly one `complete` or `error`, then ends. No events follow it. `complete` with no saved ID is a successful conversational response/preview, not a saved meal. Canonical saved IDs refer to the existing saved-plan storage. `preferences_changed` triggers refresh of existing preferences UI. Citations are `{title,url}` with HTTP(S) URLs shown as clickable sources for web-derived claims.

Clients ignore unknown event types while advancing sequence, and ignore already-seen sequence numbers. A gap, changed run ID, malformed known payload, unsupported envelope version, or EOF without a terminal event requires `GET /agent/runs/{request_id}` reconciliation. An unknown event is never assumed terminal. Duplicate terminal frames cannot cause a second write or UI action. A retryable error permits a new request only after reconciliation establishes its outcome; never fall back to legacy generation after an ambiguous write. Cancellation stops consumption/generation where possible; reconcile the known server outcome before retrying.

## Meal and action shapes

MealProposal: `id` UUID, `version` positive integer, `dining_hall`, `date`, `meal_type`, `description` (≤4000 characters), `items` (1–30), `totals`. MealItem: `menu_item_id`, `name`, `serving_size` (each trimmed 1–200 characters), `servings` (>0 and ≤20), and `calories`, `protein_g`, `fat_g`, `carbs_g`. Nutrition values are finite numbers in [0,100000]; numeric strings and booleans are not numbers. Item nutrition already includes its servings. Totals equal the sum of item values (absolute tolerance 0.01); item keys are unique. These are wire safety bounds, not nutrition goals or evidence of dietary safety.

Menu item IDs are canonical IDs or stable keys within the server-held snapshot. Only server services calculate item nutrition from trusted menu data and validate restrictions/allergen metadata; model-provided nutrition is never trusted. Missing allergen data is unknown. Model output cannot create a trusted proposal simply by passing wire validation.

PendingAction: `id` UUID, full `proposal`, `expires_at` timezone-aware timestamp. Expiry and plan version are checked against storage at confirmation, not merely against a client clock. A proposal may be displayed before a pending action exists. Existing four generation contracts are untouched by this foundation and need explicit adapters later.

## SDK verification and deployment gates

Verified 2026-10-10 against [official SDK quickstart](https://developers.openai.com/api/docs/guides/agents/quickstart?lang=python), [running agents](https://developers.openai.com/api/docs/guides/agents/running-agents), and [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna): Python package `openai-agents`, Responses, streamed runs, and exact model ID `gpt-6-luna`. Account-specific access is a separate gate.

PyPI metadata verified `openai-agents==0.8.1` requires Pydantic ≥2.12.3; both manifests pin those two versions. The current 0.23.1 requires Starlette ≥1.3.1, incompatible with the existing FastAPI 0.116.2, so that framework migration is excluded. No callers referenced the removed local `agents/__init__.py` imitation. Gemini dependencies and behavior remain for scanning/rollback.

Before rollout: live model/tool smoke test, private-trace/log inspection, additive storage migrations, measured Vercel package/deadline/streaming limits, staging compatibility, and rollback drill. Foundation does not claim those gates pass. No production API calls are needed for contract tests.
