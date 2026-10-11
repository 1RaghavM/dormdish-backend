-- Custom sessions access food logs through the authenticated backend, not Supabase JWTs.
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE public.food_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Allow service role full access" ON public.food_log;
REVOKE ALL ON public.food_log FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.food_log TO service_role;
COMMIT;
