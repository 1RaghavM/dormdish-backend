# Synthetic baseline test evidence

Dedicated test branch: `tests/dd-agent/baseline-validation`, based on main
`3609664`. Companion implementation branch: `feat/dd-agent/baseline-validation`.
Ponytail full applied. Test ownership is limited to
`tests/test_agent_baseline_compare.py` and this evidence file.

The first test starts a fresh interpreter, forbids app/database imports and real
HTTP transports, and exercises the default command through the provider SDKs.
Initial checkpoint is not merge-ready: the implementation dependency is still
being authored, so these tests have not run. Additional tests will cover budgets,
usage accounting, result validation, privacy, and explicit live opt-in.

No live calls, credentials, deployment, or rollout changes are part of this test PR.
