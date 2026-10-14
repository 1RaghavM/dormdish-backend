# Native agent availability

`GET /agent/availability` uses the same authentication and premium dependency as new conversations. It returns HTTP 200 with three booleans:

```json
{"conversation_enabled":false,"completion_enabled":false,"preference_editing_enabled":false}
```

Availability and new conversation/message/confirmation writes require `X-App-Version` to exactly match `DORMDISH_AGENT_APP_VERSION` (default `2.2.5`). Missing or different versions return disabled availability and reject new agent writes with503. Blank configuration fails closed. Owned conversation/run reads and deletion remain available for recovery and cleanup. This client-declared version is compatibility information, not authentication.

Conversation availability reuses the server's existing internal-user/cohort admission and kill switch. Disabled, noncohort, and malformed rollout configuration return all false. Completion and preference editing additionally require their existing `DORMDISH_COMPLETION_ENABLED` and `DORMDISH_EXPLICIT_MEMORY_ENABLED` flags respectively. Missing/invalid authentication and premium denial retain their usual HTTP errors. The response is `Cache-Control: no-store` and does not access feature storage or write data.

The native client should fail closed when this endpoint is unavailable, refresh for each authenticated account, and clear availability on logout. These booleans control presentation; existing operation endpoints continue enforcing their own permissions and flags.

Deploy this additive endpoint before the dependent native client. No migration is required. For the approved 2.2.5 rollout, set `DORMDISH_AGENT_APP_VERSION=2.2.5` and retain the existing rollout percentage, enabled, and kill-switch controls. An explicitly configured `2.2.4` overrides the source default until the deployment configuration is updated. The exact-version change disables new Ask DormDish work on 2.2.4 while preserving owned recovery and deletion. Rollback restores the prior configured version or uses the existing kill switch. Existing clients ignore the endpoint. The companion availability test PR is a merge dependency; do not activate the native feature until both source and tests pass review.
