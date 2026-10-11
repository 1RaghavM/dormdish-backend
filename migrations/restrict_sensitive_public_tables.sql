-- Only the backend and admin dashboard (service_role, which bypasses RLS) use these tables.
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE public.password_reset_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.push_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users_backup_users_backup_20251021 ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.password_reset_codes, public.push_tokens, public.users_backup_users_backup_20251021
    FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.password_reset_codes, public.push_tokens TO service_role;
COMMIT;
