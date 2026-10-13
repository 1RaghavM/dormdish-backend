# Hosted-search completion validation

The October 13, 2026 bounded `gpt-6-luna` public-dining smoke returned HTTP 200
with response status `completed`, one completed `web_search_call`, one subsequent
`searching` call, a reasoning item, a message and one citation. The previous check
rejected this response because it counted output entries rather than completed calls.

The [official Responses reference](https://developers.openai.com/api/reference/python/resources/responses/methods/create)
defines `max_tool_calls` as a limit on processed built-in calls and says further
attempts are ignored. Interpreting the trailing `searching` entry as an unprocessed
attempt is an inference from the observed response, not a documented status guarantee.

Validation accepts only a completed overall response whose first search completed
and whose remaining search entries, if any, are `searching`. Multiple completed
searches, failed or unknown statuses, pending-before-completed results, and results
without a completed search remain errors. Answer and citation validation are unchanged.
The request still permits one processed hosted call, one API request without retries,
a 15-second timeout and 2,048 output tokens. Only the public university, hall, date
and enumerated topic are sent; private profile and conversation data are excluded.

Scope: `AgentTurn._search_public`, its `search_public_dining` caller, and the existing
runtime/search tests. There are no schema, client contract, dependency or flag changes.
The dedicated companion test PR is a merge dependency. Rollout remains disabled
until reviewed source and tests are merged and the coordinator verifies the live path.
Rollback uses the existing runtime kill switch without deleting user data.

This source change makes no Gemini changes or comparisons. Its author did not run
paid requests or access credentials; the coordinator owns the bounded live retest.
