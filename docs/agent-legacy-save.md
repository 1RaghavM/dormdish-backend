# Atomic agent legacy autosave

All four agent-backed generation adapters use `agent_save_legacy_plan` instead of a table insert.
The database transaction checks the authenticated owner and requested date while holding the same
`SHARE ROW EXCLUSIVE` saved-plan lock used by action confirmation. A full-day candidate conflicts
with any existing plan for that owner/date; an individual meal conflicts with the same individual
meal across all halls. Different individual meals remain allowed, subject to the existing
`(user_id, date, dining_hall)` unique index. As in the existing individual-meal precheck, a full-day
row is not treated as an individual meal. Existing data and indexes are unchanged.

The RPC never updates another plan. A conflict returns no saved ID, preserving the generated preview
and `auto_saved:false`. A successful save returns the server-generated candidate ID. After an empty
or lost RPC reply, the adapter reads only that candidate's exact ID and authenticated owner; it never
adopts a different plan, retries the insert, or falls back to Gemini. Failed reconciliation leaves
`auto_saved:false` under the existing route error handling; clients must not interpret it as proof
that a timed-out write could never finish. Legacy routes have no client request-ID replay contract.

Only the `DORMDISH_AGENT_GENERATION_ENABLED=true` path uses the RPC. The kill switch and flag-off
Gemini path retain their existing inserts and response shape. This transaction closes the race
between agent-backed legacy writers; a flag-off writer that inserts after the transaction can still
create a different-hall row, since no global index or trigger is added.

## Deployment and rollback

Apply `migrations/add_agent_legacy_save.sql` before enabling agent generation, after independently
reviewing the migration and passing the companion acceptance tests in backend PR #28. The function
is security invoker, has a five-second lock timeout, and is executable only by `service_role`.
The migration is additive and rerunnable; it neither deduplicates nor deletes existing plans.
A missing RPC fails autosave closed rather than using an unguarded insert. Keep rollout disabled
until schema installation, current-head tests, deployment checks and independent reviews pass.
Rollback disables agent generation or enables its kill switch; retain the function and all data.

## Checks

Initial helper check: `python -m pytest -o addopts='' -q tests/test_agent_legacy_save_unit.py`:
4 passed. Companion PR #28 owns real PostgreSQL concurrent cross-hall requests, storage permissions,
lost-response recovery and legacy-route compatibility. Source merge depends on those tests and a
fresh independent review. No production migration or flag change was performed by this PR.
