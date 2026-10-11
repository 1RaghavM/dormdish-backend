# Explicit memory test evidence

Dedicated test branch `tests/dd-agent/explicit-memory` stacks source contract `46852c4` from feature PR19. The initial checkpoint authors authenticated API and actual PostgreSQL tests from the wire contract; the implementation migration is not yet integrated, so tests are **not yet run**. The suite uses disposable local databases only, with model construction forbidden for memory commands. No paid API or production database is used.

Coverage: supported instructions and typed provenance, non-authorizing text, global/meal conflict resolution, exact versus ambiguous forgetting, owner/auth boundaries, duplicate payload collisions, committed-response recovery, default-off and old button/read/delete compatibility. Transaction rollback, epoch races and SQL privilege cases follow after the implementation checkpoint.

Run after integrating source: `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_explicit_memory.py -q`.

Keep deployment disabled. Source PR19, preference PR13/14, routes PR10/11 and completion PR18 are explicit dependencies. Independent source and test reviews, combined checks and schema-first rollout are required. This test PR has no production migrations or activation changes.
