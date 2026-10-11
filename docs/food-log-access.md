# Food-log database access

Production schema inspection through the Supabase CLI on 2026-10-11 found that `food_log` has RLS enabled, but the policy named `Allow service role full access` applies to PUBLIC with `USING (true)`. Both `anon` and `authenticated` also have table access. This permits bypassing the backend's owner checks.

`migrations/secure_food_log_access.sql` removes that permissive policy and public client grants, retaining backend service-role CRUD. It changes no records or HTTP contracts and is safe to repeat. Native `FoodLogService` uses the existing authenticated backend endpoints; FastAPI supplies the owner and filters reads/deletes. The backend Supabase client must use its configured service key.

The implementation regression reconstructs the actual permissive policy/grants on synthetic rows, proves public reading before migration, then verifies denied client SELECT/INSERT/UPDATE/DELETE, unchanged stored rows, successful backend CRUD and repeated deployment. Focused run: 1 passed, 0 failed, 0 skipped in 5.67s. Complete source suite at `e5cc959c28ca30ffe31f4be996f0d8f4c8fbd05a`: **414 passed, 0 failed, 0 skipped** in 25.17 seconds; `/private/tmp/dd-food-log-access-full.log`. Dedicated independent compatibility tests and separate source/test reviews are required before applying this SQL.

Apply through the CLI to verified DormDish project `rszkbhgowcrvwiwcmntt`, then verify table privileges and policy metadata without reading user records. Keep the restriction if application troubleshooting is needed; do not restore public access as rollback. No application feature flag change is necessary.

Not yet checked: production application smoke test. This SQL has not been deployed.
