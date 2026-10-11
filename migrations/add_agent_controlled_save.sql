-- Apply after enforce_agent_profile_safety.sql, before enabling DORMDISH_CONTROLLED_SAVE_ENABLED. Additive.
BEGIN;
SET LOCAL lock_timeout = '5s';

-- Deterministic: only the user's own stored turn text can authorize a write.
CREATE OR REPLACE FUNCTION public.agent_save_intent(p_text text, p_meal_type text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT CASE
        WHEN t ~ '\?$' OR t ~ '\m(don''t|don’t|dont|do not|never|not|without)\M' THEN NULL
        WHEN substring(t from '^(?:please\s+)?(?:replace|overwrite) my (breakfast|lunch|dinner)\M') = p_meal_type THEN 'replace'
        WHEN t ~ '^(please\s+)?(save|generate and save|plan and save|make and save)\M' THEN 'save'
    END
    FROM (SELECT lower(btrim(p_text, E' \t\r\n')) AS t) input
$$;

CREATE OR REPLACE FUNCTION public.agent_save_turn(p_user_id uuid, p_conversation_id uuid, p_request_id uuid,
    p_events jsonb, p_history jsonb, p_assistant_text text, p_action jsonb DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE request jsonb; intent text; saved jsonb; n integer := jsonb_array_length(p_events);
BEGIN
    SELECT r.request INTO request FROM agent_runs r WHERE r.user_id = p_user_id
        AND r.conversation_id = p_conversation_id AND r.id = p_request_id AND r.status = 'running';
    intent := agent_save_intent(request->>'text', request->'context'->>'meal_type');
    IF p_action IS NULL OR intent IS NULL
        OR p_action->'action'->'proposal'->>'meal_type' IS DISTINCT FROM request->'context'->>'meal_type'
        OR intent <> (CASE WHEN p_action->>'target_plan_id' IS NULL THEN 'save' ELSE 'replace' END) THEN
        RETURN agent_finish_turn(p_user_id, p_conversation_id, p_request_id, p_events, p_history, p_assistant_text, p_action);
    END IF;
    IF p_events->(n - 2)->>'type' IS DISTINCT FROM 'action_required' THEN RAISE EXCEPTION 'agent_invalid'; END IF;
    PERFORM agent_finish_turn(p_user_id, p_conversation_id, p_request_id, p_events, p_history, p_assistant_text, p_action);
    -- The turn's run row already exists, so confirm under a derived id; any guard failure rolls back the finish too.
    saved := agent_confirm_action(p_user_id, (p_action->'action'->>'id')::uuid,
        md5('dormdish-controlled-save:' || p_request_id)::uuid, (p_action->'action'->'proposal'->>'version')::integer);
    UPDATE agent_runs SET events = COALESCE((SELECT jsonb_agg(value ORDER BY ord)
            FROM jsonb_array_elements(p_events) WITH ORDINALITY e(value, ord) WHERE ord < n - 1), '[]')
        || jsonb_set(jsonb_set(p_events->(n - 1), '{sequence}', to_jsonb(n - 1)),
            '{data,saved_plan_id}', saved->'events'->0->'data'->'saved_plan_id')
        WHERE user_id = p_user_id AND id = p_request_id;
    RETURN agent_get_run(p_user_id, p_request_id);
END $$;

REVOKE ALL ON FUNCTION public.agent_save_intent(text, text),
    public.agent_save_turn(uuid, uuid, uuid, jsonb, jsonb, text, jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.agent_save_intent(text, text),
    public.agent_save_turn(uuid, uuid, uuid, jsonb, jsonb, text, jsonb) TO service_role;
COMMIT;
