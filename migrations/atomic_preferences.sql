-- Apply after add_agent_persistence.sql, before deploying the RPC-backed preference manager.
BEGIN;

CREATE FUNCTION public.preference_set(p_user_id uuid, p_category text, p_item_name text,
    p_item_type text, p_meal_type text, p_meal_plan_id text DEFAULT NULL, p_reason text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE chosen uuid; result user_preferences;
BEGIN
    IF p_category IS NULL OR p_category NOT IN ('favorite', 'dislike')
        OR p_item_type IS NULL OR p_item_type NOT IN ('food_item', 'meal_plan')
        OR p_meal_type IS NULL OR p_meal_type NOT IN ('breakfast', 'lunch', 'dinner')
        OR p_item_name IS NULL OR length(btrim(p_item_name)) NOT BETWEEN 1 AND 200
        OR length(p_reason) > 1000 OR length(p_meal_plan_id) > 200
        THEN RAISE EXCEPTION 'preference_invalid'; END IF;
    -- Same owner lock/order as agent_claim_turn; one choice and replay invalidation commit together.
    PERFORM 1 FROM users WHERE id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'preference_not_found'; END IF;
    SELECT id INTO chosen FROM user_preferences WHERE user_id = p_user_id
        AND lower(btrim(item_name)) = lower(btrim(p_item_name)) AND meal_type = p_meal_type
        ORDER BY created_at DESC, id LIMIT 1;
    DELETE FROM user_preferences WHERE user_id = p_user_id
        AND lower(btrim(item_name)) = lower(btrim(p_item_name)) AND meal_type = p_meal_type
        AND id IS DISTINCT FROM chosen;
    IF chosen IS NULL THEN
        INSERT INTO user_preferences(user_id, category, item_name, item_type, meal_type, meal_plan_id, reason)
            VALUES(p_user_id, p_category, btrim(p_item_name), p_item_type, p_meal_type, p_meal_plan_id, p_reason)
            RETURNING * INTO result;
    ELSE
        UPDATE user_preferences SET category = p_category, item_name = btrim(p_item_name),
            item_type = p_item_type, meal_plan_id = p_meal_plan_id, reason = p_reason
            WHERE user_id = p_user_id AND id = chosen RETURNING * INTO result;
    END IF;
    PERFORM agent_clear_history(p_user_id);
    RETURN jsonb_build_object('status', 'success', 'data', jsonb_build_array(to_jsonb(result)));
END $$;

CREATE FUNCTION public.preference_forget(p_user_id uuid, p_category text, p_item_name text)
RETURNS jsonb LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE removed integer;
BEGIN
    IF p_category IS NULL OR p_category NOT IN ('favorite', 'dislike')
        OR p_item_name IS NULL OR length(btrim(p_item_name)) NOT BETWEEN 1 AND 200
        THEN RAISE EXCEPTION 'preference_invalid'; END IF;
    PERFORM 1 FROM users WHERE id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'preference_not_found'; END IF;
    -- Existing delete endpoints forget the named category across meals.
    DELETE FROM user_preferences WHERE user_id = p_user_id AND category = p_category
        AND lower(btrim(item_name)) = lower(btrim(p_item_name));
    GET DIAGNOSTICS removed = ROW_COUNT;
    PERFORM agent_clear_history(p_user_id);
    RETURN jsonb_build_object('status', CASE WHEN removed > 0 THEN 'deleted' ELSE 'not_found' END);
END $$;

REVOKE ALL ON FUNCTION public.preference_set(uuid, text, text, text, text, text, text),
    public.preference_forget(uuid, text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.preference_set(uuid, text, text, text, text, text, text),
    public.preference_forget(uuid, text, text) TO service_role;
COMMIT;
