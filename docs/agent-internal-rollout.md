# Internal conversation rollout

`DORMDISH_AGENT_INTERNAL_USER_IDS` is an optional comma-separated list of authenticated user UUIDs. With `DORMDISH_AGENT_ENABLED=true` and `DORMDISH_AGENT_ROLLOUT_PERCENT=0`, only these users can create conversations, start turns, or apply proposals. UUIDs are normalized; surrounding whitespace is allowed. Unset or blank preserves existing percentage behavior. A malformed entry (including an empty comma-separated entry) fails closed with 503, even at 100%.

Internal membership never overrides the disabled flag, kill switch, invalid configuration, authentication, ownership, subscription policy, quotas, or write validation. Other users retain the existing stable percentage admission rule. The server uses the authenticated account ID; request text and model arguments cannot choose membership. Recovery reads and deletion remain available after rollback.

For a preview smoke, scope the settings to the exact preview Git branch, use only dedicated synthetic test account IDs, and keep the percentage at zero. Production settings stay untouched. Keep `DORMDISH_AGENT_GENERATION_ENABLED` off: this conversation allowlist does not gate legacy generation routes. Disable the agent flag or set its kill switch to roll back; remove temporary preview settings after testing. No schema change or user-data rewrite is required.

Acceptance depends on a separately authored and independently reviewed test PR. Rollout stays disabled until the migration's schema, deployment, live-model, recovery, and native acceptance gates pass.
