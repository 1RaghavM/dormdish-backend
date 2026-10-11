# Explicit memory test evidence

Dedicated test branch `tests/dd-agent/explicit-memory` stacks source contract `46852c4` from feature PR19. The initial checkpoint authors authenticated API and actual PostgreSQL tests from the wire contract; the implementation migration is not yet integrated, so tests are **not yet run**. The suite uses disposable local databases only, with model construction forbidden for memory commands. No paid API or production database is used.

Coverage: supported instructions and typed provenance, non-authorizing text, global/meal conflict resolution, exact versus ambiguous forgetting, owner/auth boundaries, duplicate payload collisions, committed-response recovery, default-off and old button/read/delete compatibility. Transaction rollback, epoch races and SQL privilege cases follow after the implementation checkpoint.

Run after integrating source: `DD_AGENT_LOCAL_PG=1 /private/tmp/dd-agent-runtime-uv/bin/python -m pytest tests/test_explicit_memory.py -q`.

Keep deployment disabled. Source PR19, preference PR13/14, routes PR10/11 and completion PR18 are explicit dependencies. Independent source and test reviews, combined checks and schema-first rollout are required. This test PR has no production migrations or activation changes.

## First executable checkpoint

Implementation `f8a51d9add17ceeb08f479ed7779e81757956dfc`, explicitly merged into test branch `c4fd0ede6f0996685b15fa1651d8d87582fb6440`. Initial 40 cases: 39 passed, 1 real behavioral failure (temporary “for this meal” instruction persisted globally). Expanded 57 cases: **54 passed, 3 failed**, 0 skipped, 24.09 seconds. The additional failures reproduce HTTP 404 deleting a preference with an encoded slash in both favorite and dislike categories.

A further parser regression set yielded **20 passed, 6 failed** (37 deselected), 8.73 seconds: temporary “for this meal”, “for now”, “this week”, “during this conversation”, and curly/backtick quoted values all incorrectly persisted. These are behavioral failures, not missing imports or fixture errors. Source owner received exact inputs; assertions remain unchanged. Current test file contains 63 cases, including rollback of both preference and receipt, SQL-role denial, owner-concurrent epoch serialization, stale in-flight resurrection prevention, source-text isolation, account deletion, and compatibility. Green final evidence is pending source fixes.

Local logs: `/private/tmp/dd-explicit-memory-tests-expanded.log`, `/private/tmp/dd-explicit-memory-parser-red.log`. Only synthetic local PostgreSQL was used.
