# DormDish Backend

FastAPI backend for DormDish's college dining and nutrition app. It serves campus menus, personalized meal plans, food scanning and logging, account management, subscriptions, and the Ask DormDish conversation API.

Meal generation supports the existing Gemini path and a feature-gated OpenAI Agents runtime. Shared services validate menu selections and calculate nutrition deterministically before a proposal can be saved. Food photo analysis uses Gemini.

## Local setup

Use **Python 3.11 or 3.12** (`pyproject.toml` requires `>=3.11,<3.13`). Run commands from the repository root:

```bash
git clone https://github.com/1RaghavM/dormdish-backend.git
cd dormdish-backend
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create a local `.env` with development credentials. `app.py` loads it through `python-dotenv`; the file is ignored by Git.

```dotenv
SUPABASE_URL=https://your-development-project.supabase.co
SUPABASE_SERVICE_KEY=your-development-service-role-key
GEMINI_API_KEY=your-gemini-api-key
OPENAI_API_KEY=your-openai-api-key
CORS_ORIGINS=http://localhost:3000
```

Supabase powers accounts, sessions, menus, saved plans, preferences, and food logs. Use a development database with the required schema; installing Python dependencies does not create tables. The SQL in `migrations/` contains incremental changes, not a complete fresh-database bootstrap. See the [schema deployment notes](docs/agent-schema-deployment.md) for context and the [agent persistence documentation](docs/agent-persistence.md) for its storage contract.

`SUPABASE_SERVICE_KEY` is preferred; `SUPABASE_KEY` is a fallback. Keep privileged keys on the server. Gemini credentials are needed for scanning and the default generation path; OpenAI credentials are needed when using the agent runtime.

Start the API:

```bash
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

- Interactive API documentation: <http://127.0.0.1:8000/docs>
- OpenAPI schema: <http://127.0.0.1:8000/openapi.json>
- Database health check: `curl http://127.0.0.1:8000/health`

The server can start without Supabase credentials, but database-backed features will not work and `/health` returns `503`. A healthy response checks database connectivity, not every integration.

## Configuration

Configure integrations only when developing their features:

| Feature | Environment variables |
| --- | --- |
| Gemini generation and scanning | `GEMINI_API_KEY`; optional `MEALPLAN_MODEL`, `FOODSCAN_MODEL` (both default to `gemini-2.5-flash`) |
| OpenAI agent | `OPENAI_API_KEY`, optional `OPENAI_BASE_URL` |
| RevenueCat | `REVENUECAT_SECRET_API_KEY`, `REVENUECAT_WEBHOOK_AUTH_KEY` |
| Stripe checkout and webhooks | `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_MONTHLY_PRICE_ID`, `STRIPE_YEARLY_PRICE_ID` |
| Prepaid subscription activation | `ACTIVATION_SECRET` |
| Google sign-in | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_AUTH_REDIRECT_URI` (falls back to `GOOGLE_REDIRECT_URI`) |
| Apple sign-in | `APPLE_CLIENT_ID`, `APPLE_WEB_SERVICE_ID` |
| Apple push notifications | `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `APNS_KEY_CONTENT` (base64-encoded `.p8` key) |
| Email | `SMTP_EMAIL`, `SMTP_PASSWORD`; Brevo uses `BREVO_API_KEY`, `BREVO_FROM_EMAIL`, `BREVO_FROM_NAME`, `BREVO_TEMPLATE_ID`, `BREVO_LIST_ID` |
| Other integrations | `USDA_API_KEY`, `TURNSTILE_SECRET_KEY`, `CRON_SECRET`, `ANNOUNCEMENT_ADMIN_KEY` |
| Local debugging and browser access | `DEBUG`, `CORS_ORIGINS` (comma-separated), `CORS_ORIGIN_REGEX` |

Google Calendar support also requires the optional Google packages listed as comments in `requirements.txt` and its OAuth redirect configuration in `google_calendar_service.py`.

### Ask DormDish feature gates

These are **code defaults**, not a statement about the current deployment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DORMDISH_AGENT_ENABLED` | `false` | Enable conversation admission. |
| `DORMDISH_AGENT_KILL_SWITCH` | `false` | Block agent writes and disable agent-backed legacy generation. |
| `DORMDISH_AGENT_ROLLOUT_PERCENT` | `0` | Stable percentage cohort for conversation admission. |
| `DORMDISH_AGENT_INTERNAL_USER_IDS` | Empty | Comma-separated authenticated user UUIDs admitted outside the percentage cohort. |
| `DORMDISH_AGENT_APP_VERSION` | `2.2.5` | Exact required `X-App-Version` header for new conversations, turns, and confirmations. |
| `DORMDISH_AGENT_GENERATION_ENABLED` | `false` | Route legacy generation endpoints through the agent adapter. |
| `DORMDISH_CONTROLLED_SAVE_ENABLED` | `false` | Offer controlled save actions from agent turns. |
| `DORMDISH_EXPLICIT_MEMORY_ENABLED` | `false` | Enable explicit preference memory updates. |
| `DORMDISH_COMPLETION_ENABLED` | `false` | Enable meal completion synchronization. |

Boolean gates use the literal strings `true` and `false`. Conversation access also requires authentication and premium access. Internal admission does not override the kill switch or app-version requirement. Legacy generation, memory, and completion have separate switches; the conversation allowlist does not globally gate those features. Owned conversation/run recovery and deletion remain available after conversation rollback.

See [internal rollout](docs/agent-internal-rollout.md), [app availability](docs/agent-app-availability.md), [legacy adapters](docs/agent-legacy-adapters.md), and [release deployment evidence](docs/agent-release-deployment.md) before changing rollout settings.

## API overview

Use `/docs` for request/response schemas and authentication requirements. Main route groups include:

| Area | Representative routes |
| --- | --- |
| Accounts | `/auth/register`, `/auth/login`, `/auth/profile`, `/auth/logout`, `/auth/account`; Google and Apple sign-in routes |
| Campus menus | `/universities`, `/dining-halls`, `/menu/{dining_hall}/{date}` |
| Meal generation | `/generate-meal-plan`, `/generate-meal`, `/generate-meal-plan-stream`, `/generate-meal-stream` |
| Saved plans | `/meal-plan/save`, `/meal-plan/history`, `/meal-plan/today`, `/meal-plan/{plan_id}` |
| Ask DormDish | `/agent/availability`, `/agent/conversations`, `/agent/conversations/{conversation_id}/messages`, `/agent/runs/{request_id}`, `/agent/actions/{action_id}/confirm` |
| Meal completion | `/meal-plan/{plan_id}/completion`, `/meal-completions`, `/meal-completion-requests/{request_id}` |
| Food tracking | `/food-log/add`, `/food-log/{date}`, `/barcode/lookup` |
| Food photos | `/food-scan/upload-url`, `/food-scan/analyze-json`, `/food-scan/analyze`, `/food-scan/feedback` |
| Preferences | `/preferences/favorite`, `/preferences/dislike`, `/preferences/all` |
| Subscriptions and referrals | `/checkout/create-session`, `/webhook/stripe`, `/referral/my-code`, `/referral/apply-code` |

The agent streams versioned server-sent events, persists runs for recovery, and requires explicit confirmation for proposed meal saves. See the [v1 API contract](docs/agent-api-v1.md), [route behavior](docs/agent-routes.md), [controlled saves](docs/agent-controlled-save.md), [meal completion API](docs/meal-completion-api.md), and [explicit memory API](docs/explicit-memory-api.md).

## Repository layout

```text
app.py                      Main FastAPI application and existing API routes
api/index.py                Vercel ASGI entry point
services/                   Shared meal validation, nutrition, and completion logic
dormdish_agent/              Agent contracts, runtime, tools, routes, and persistence
memory/                     Supabase-backed preference handling
meal_plan_agent.py          Gemini meal generation
food_scan_*.py              Photo analysis, upload handling, models, and validation
menu_cache.py               Gemini menu context caching
universities_config.json    Campus and dining hall configuration
refresh_menus.py             Scheduled menu ingestion
cleanup_menus.py             Scheduled cleanup
migrations/                 Incremental SQL schema and access-control changes
contracts/                  Versioned contract fixtures
tests/                      Unit, contract, and PostgreSQL integration tests
scripts/agent_release_smoke.py  Synthetic agent SDK smoke test
docs/                       API contracts, rollout notes, and test evidence
.github/workflows/          Scheduled operational jobs
```

## Testing

Install test dependencies in the virtual environment:

```bash
python -m pip install 'pytest>=8.2,<8.3' pytest-asyncio
python -m pytest
python scripts/agent_release_smoke.py
```

The smoke script uses synthetic data and a mocked HTTP transport by default; it does not call paid models or write to a database. Its explicit `--live` mode calls OpenAI and incurs usage charges.

PostgreSQL integration tests are opt-in and skip unless `DD_AGENT_LOCAL_PG=1`. They expect a running local Docker container named `dd-agent-migration-db`, with `psql` accessible as the `postgres` user. The fixtures create and remove throwaway databases inside that container; use a dedicated test container.

```bash
DD_AGENT_LOCAL_PG=1 python -m pytest
```

Report skipped database tests separately from passing tests. Existing [release test evidence](docs/release-2.2.5-test-evidence.md) records the revision and scope of prior checks; it is not a substitute for testing a new change.

## Deployment and scheduled jobs

[Vercel configuration](vercel.json) routes requests to `api/index.py`, which exports `app.py`'s FastAPI application. Runtime dependencies come from `requirements.txt`; `runtime.txt` specifies Python 3.11. Configure deployment secrets in the hosting environment and verify the actual build/runtime before rollout.

The repository configures both Vercel cron routes (menu refresh and daily reminders) and GitHub Actions jobs (menu refresh, cleanup, and prepaid subscription activation). Review `vercel.json` and `.github/workflows/` together when changing schedules to avoid overlapping work. Menu refresh and cleanup workflows use the GitHub `production` environment's `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`; activation uses `BACKEND_URL` and `ACTIVATION_SECRET`.

`vercel-build` is a deployment cleanup script that removes `.git` and `.venv` when present. Do not run it in a working checkout. Deployment and schema changes require their own verification; a source push or successful `/health` response does not prove feature rollout.
