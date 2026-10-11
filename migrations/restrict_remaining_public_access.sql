-- Every consumer uses service_role (bypasses RLS); custom sessions never send Supabase JWTs.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['announcement_interactions','announcements','app_store_metrics','barcode_cache',
        'bug_reports_public','cleanup_logs','gemini_caches','instagram_analysis','instagram_insights',
        'instagram_posts','instagram_sync_status','meal_plan_generations','menus','referral_codes',
        'referral_events','referral_rewards','saved_meal_plans','school_requests','universities',
        'user_meal_plans','user_sessions','users','users_duplicate','waitlist_users','weight_history'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, anon, authenticated', t);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO service_role', t);
    END LOOP;
END $$;
DROP POLICY IF EXISTS "Allow service role full access" ON public.barcode_cache;
REVOKE EXECUTE ON FUNCTION public.set_updated_at(), public.update_user_bmr(), public.trigger_set_updated_at(),
    public.update_updated_at_column(), public.cleanup_expired_sessions(), public.cleanup_expired_meal_plans(),
    public.migrate_user_preferences_to_columns(),
    public.calculate_user_bmr(integer, integer, integer, numeric, character varying, character varying)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.set_updated_at(), public.update_user_bmr(), public.trigger_set_updated_at(),
    public.update_updated_at_column(), public.cleanup_expired_sessions(), public.cleanup_expired_meal_plans(),
    public.migrate_user_preferences_to_columns(),
    public.calculate_user_bmr(integer, integer, integer, numeric, character varying, character varying)
    TO service_role;
-- New objects created by postgres must not inherit client grants.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated;
COMMIT;
