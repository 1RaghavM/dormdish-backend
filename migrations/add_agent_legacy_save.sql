-- Additive, schema first; preserve existing plans and the legacy hall index.
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE OR REPLACE FUNCTION public.agent_save_legacy_plan(
    p_user_id uuid, p_plan_id uuid, p_dining_hall text, p_date date, p_payload jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_temp SET lock_timeout = '5s' AS $$
DECLARE meal text := p_payload->>'meal';
BEGIN
    IF p_user_id IS NULL OR p_plan_id IS NULL OR p_date IS NULL
        OR p_dining_hall IS NULL OR length(btrim(p_dining_hall)) NOT BETWEEN 1 AND 100
        OR jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
        OR (p_payload ? 'meal' AND (meal IS NULL OR meal NOT IN ('breakfast', 'lunch', 'dinner')))
        THEN RAISE EXCEPTION 'agent_invalid'; END IF;
    -- shortcut: share the confirmation lock until every writer uses a common per-owner/date protocol.
    LOCK TABLE saved_meal_plans IN SHARE ROW EXCLUSIVE MODE;
    IF EXISTS(SELECT 1 FROM saved_meal_plans WHERE user_id = p_user_id AND date = p_date
        AND (meal IS NULL OR meal_plan_data->>'meal' = meal)) THEN
        RETURN '[]'::jsonb;
    END IF;
    INSERT INTO saved_meal_plans(id, user_id, dining_hall, date, meal_plan_data, created_at, updated_at)
        VALUES(p_plan_id, p_user_id, p_dining_hall, p_date, p_payload, now(), now());
    RETURN jsonb_build_array(jsonb_build_object('id', p_plan_id));
END $$;
REVOKE ALL ON FUNCTION public.agent_save_legacy_plan(uuid, uuid, text, date, jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.agent_save_legacy_plan(uuid, uuid, text, date, jsonb) TO service_role;
COMMIT;
