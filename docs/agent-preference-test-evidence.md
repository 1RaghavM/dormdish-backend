# Independent preference transaction checks

Companion to backend PR #13, production source `4b56dc993a48f4fe84eea84178f998ce113bb849`. Apply Ponytail full; test ownership is limited to `tests/test_preference_transactions.py` and this evidence file. No production changes.

Initial focused run: **33 passed, 0 failed, 0 skipped** (12.10 seconds) with `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-foundation-uv/bin/python -m pytest -o addopts='' tests/test_preference_transactions.py -q`. This uses real PostgreSQL RPCs, the actual PreferenceManager, and actual FastAPI HTTP routes with synthetic authentication overrides. The existing Docker transport creates and drops a separate disposable database. No live credentials, models, or production rows are used.

Coverage includes a six-way favorite/dislike race with normalized names and stable IDs; existing contradictory rows with a category-scoped unique index; precise owner/meal/category boundaries; idempotent deletion; replacement and deletion rollback on history-trigger failure; all owned conversations and in-flight replay epochs; new history after forgetting; both untrusted database roles; authenticated HTTP identity, Unicode/apostrophes, and old response shapes; pre-storage input rejection; private database errors and no automatic write retries. Migration application leaves preexisting contradictory data unchanged until an explicit choice.

The first run's four failures were test-fixture errors: the existing HTTP exception handler returns `message`, not FastAPI's default `detail`. Assertions now check the exact existing envelope. No product defect was hidden or production code edited. Full-suite and mutation evidence follow before final handoff.

Merge/deployment gate: PR #13 changes existing preference buttons to RPCs regardless of agent flags. Verify and apply `add_agent_persistence.sql`, then `atomic_preferences.sql`, before merging/deploying that backend. Production SQL credentials and schema privileges remain unverified. This test PR and PR #13 each need a fresh independent reviewer; neither author approves their own changes. Keep runtime rollout disabled. Rollback preserves additive functions and data.
