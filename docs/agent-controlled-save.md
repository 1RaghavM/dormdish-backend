# Agent controlled save

Explicit "save" / "generate and save" and exact authorized replacement for
`POST /agent/conversations/{id}/messages`. No new endpoint.

## Flag and rollout

- `DORMDISH_CONTROLLED_SAVE_ENABLED` (default `false`). Any value other than `true` keeps today's
  behavior exactly: preview, `action_required`, then Apply via `POST /agent/actions/{id}/confirm`.
- Deploy `migrations/add_agent_controlled_save.sql` (after `enforce_agent_profile_safety.sql`)
  before enabling. Rollback: set the flag to `false`; the SQL is additive and can stay.

## Authorization

Decided only in SQL by `agent_save_turn`, inside the write transaction, from the stored text of
this request (`agent_runs.request->>'text'`, owner/conversation/request scoped, status `running`).
Model output, tool arguments, assistant text, and history are never consulted. Python does not
pre-check; it calls `agent_save_turn` instead of `agent_finish_turn` whenever the flag is on and
the turn produced a validated proposal.

`agent_save_intent(text, meal_type)` lowercases and trims the text, then:

- returns `NULL` if it ends with `?` or contains a whole-word negation
  (`don't`, `don’t`, `dont`, `do not`, `never`, `not`, `without`);
- `replace` if it matches `^(please\s+)?(replace|overwrite) my (breakfast|lunch|dinner)` (word
  boundary) and the named meal equals the request's `context.meal_type`;
- `save` if it matches `^(please\s+)?(save|generate and save|plan and save|make and save)` (word
  boundary);
- otherwise `NULL`.

## Outcomes

| Existing plan for the date | Intent | Result |
| --- | --- | --- |
| none | `save` | Atomic save. No `action_required`; `complete.saved_plan_id` = new plan id. |
| one, same hall | `replace` naming the request meal | Atomic replacement of that meal only (other meals preserved); `complete.saved_plan_id` = existing plan id. |
| one | `save` only | No write: preview + `action_required` (Apply), as today. |
| any | none | No write: preview + `action_required`, as today. |

The proposal's `meal_type` must also equal the request's `context.meal_type`, else preview.

The atomic write is one transaction: `agent_finish_turn` (stores the run outcome and the
server-held action) then the existing `agent_confirm_action`, then the run's events are rewritten
to drop `action_required`, renumber the terminal `complete`, and set its `saved_plan_id`. Events
already streamed before `action_required` are unchanged, so the client sees only the new
terminal `complete`.

Guards are the existing confirm guards: ownership, conversation expiry, action expiry, proposal
version, exact target plan version read at turn start (CAS), one plan per date at the same hall,
profile version, quota.

## Errors

A guard failure rolls back everything (no run outcome, no action, no plan write). The route then
persists a terminal `error` event (no action) and streams it. It never retries the write and never
falls back to the legacy generator.

| `error.code` | Cause | `retryable` |
| --- | --- | --- |
| `stale_profile` | Profile changed since the turn started (existing code) | true |
| `stale_plan` | Target plan changed, or a plan now exists for the date / another hall | true |
| `expired` | Proposal (menu snapshot) expired | true |
| `quota` | Agent request quota exhausted | true |

If the run deadline itself has passed, persisting the error also fails; the stream ends without a
terminal event and `GET /agent/runs/{request_id}` reports `run_expired` (existing behavior).

## Idempotency and lost responses

- A duplicate `request_id` is replayed by `agent_claim_turn` before any model run (existing).
  A repeated `agent_save_turn` for a finished run falls through to `agent_finish_turn`, which
  returns `conflict` because stored events differ; it never writes twice.
- If `agent_save_turn` returns an ambiguous/503 error, the route does not retry; it reads
  `GET /agent/runs/{request_id}` state (`agent_get_run`) and streams the stored outcome if
  terminal, otherwise ends the stream without a terminal event so the client reconciles.

## Deviation from the agreed contract

`agent_confirm_action(p_request_id)` treats an existing `agent_runs` row with that id as a replay,
and the turn's own run row always exists. The save therefore calls it with a deterministic derived
id, `md5('dormdish-controlled-save:' || request_id)::uuid`, which creates one internal confirmation
run row (same as Apply would) and consumes one quota unit (same as Apply). The turn's run, keyed by
the client's `request_id`, carries the user-visible outcome.

Profile restrictions and preferences are never written by this path.
