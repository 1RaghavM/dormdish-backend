# App-version gate tests

Dedicated backend tests for exact `X-App-Version` admission. Implementation dependency: `feat/dd-agent/app-version`, commit `2fa1b0ec47ef3aca6809bf4387c00281a54afe94`. Test branch: `tests/dd-agent/app-version`. Apply Ponytail full; no new dependencies or production calls.

Coverage uses authenticated FastAPI requests and disposable local PostgreSQL: missing/empty/older/newer/prefix/whitespace versions hide all capabilities and reject create, message, and confirmation before storage; current-version owners outside the internal list can create, send, and confirm at full rollout; blank configuration fails closed; custom exact version works; authentication, premium dependency, rollout, and kill switch remain authoritative. Old clients retain owner-scoped recovery and deletion.

Existing route fixtures send the current version so ownership, memory, save, recovery, and profile checks still exercise their original behavior.

Negative control against original source `b5407b9`: 11 behavioral assertion failures and 9 passes. Mismatched clients still received enabled capabilities and version configuration was ignored. An initial attempt before starting Docker failed in fixture setup and is excluded from this evidence.

Combined checks are running; this draft is not merge-ready yet. Source must merge first; do not expand rollout until source and independently reviewed tests pass and merge. Rollback keeps existing owner reads/deletion and stored data.
