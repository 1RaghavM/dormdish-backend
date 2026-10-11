-- Apply after enforce_agent_daily_save_policy.sql. Old pending actions have no trusted snapshot and fail closed.
BEGIN;
ALTER TABLE public.agent_actions ADD COLUMN expected_profile_version text
    CHECK (expected_profile_version IS NULL OR expected_profile_version ~ '^[0-9a-f]{64}$');

CREATE FUNCTION public.agent_get_profile(p_user_id uuid) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE profile jsonb;
BEGIN
    SELECT jsonb_object_agg(key, to_jsonb(u)->key) INTO profile
    FROM users u CROSS JOIN unnest(ARRAY[
        'university',
        'dietary_preference',
        'dietary_restrictions',
        'age',
        'height_feet',
        'height_inches',
        'weight_lbs',
        'gender',
        'activity_level',
        'goal',
        'goal_weight_lbs',
        'weight_change_speed',
        'custom_calories',
        'custom_protein_g',
        'custom_fat_g',
        'custom_carbs_g']) AS fields(key)
    WHERE u.id = p_user_id;
    IF profile IS NULL THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    RETURN jsonb_build_object('profile', profile,
        'version', encode(sha256(convert_to(profile::text, 'UTF8')), 'hex'));
END $$;
REVOKE ALL ON FUNCTION public.agent_get_profile(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.agent_get_profile(uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.agent_finish_turn(p_user_id uuid, p_conversation_id uuid, p_request_id uuid,
    p_events jsonb, p_history jsonb, p_assistant_text text, p_action jsonb DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r agent_runs; e jsonb; n integer := 0; terminal text; action_count integer := 0;
BEGIN
    PERFORM 1 FROM agent_conversations WHERE user_id = p_user_id AND id = p_conversation_id AND expires_at > now() FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    SELECT * INTO r FROM agent_runs WHERE user_id = p_user_id AND conversation_id = p_conversation_id AND id = p_request_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    IF r.status <> 'running' THEN
        IF r.events = p_events THEN RETURN agent_get_run(p_user_id, p_request_id); END IF;
        RAISE EXCEPTION 'agent_conflict';
    END IF;
    IF r.deadline <= now() THEN RAISE EXCEPTION 'agent_expired'; END IF;
    IF jsonb_typeof(p_events) <> 'array' OR jsonb_array_length(p_events) NOT BETWEEN 1 AND 512
        OR octet_length(p_events::text) > 524288 OR length(p_assistant_text) > 64000
        OR jsonb_typeof(p_history) <> 'array' OR octet_length(p_action::text) > 262144 THEN RAISE EXCEPTION 'agent_invalid'; END IF;
    FOR e IN SELECT value FROM jsonb_array_elements(p_events) LOOP
        n := n + 1;
        IF terminal IS NOT NULL OR (e->>'sequence')::integer IS DISTINCT FROM n OR (e->>'run_id')::uuid IS DISTINCT FROM p_request_id
            OR (e->>'version')::integer IS DISTINCT FROM 1 THEN RAISE EXCEPTION 'agent_invalid'; END IF;
        IF e->>'type' IN ('complete', 'error') THEN terminal := e->>'type'; END IF;
        IF e->>'type' = 'action_required' THEN
            action_count := action_count + 1;
            IF e->'data'->'action' IS DISTINCT FROM p_action->'action' THEN RAISE EXCEPTION 'agent_invalid'; END IF;
        END IF;
    END LOOP;
    IF terminal IS NULL OR action_count <> (CASE WHEN p_action IS NULL THEN 0 ELSE 1 END)
        OR (p_action IS NOT NULL AND terminal <> 'complete') THEN RAISE EXCEPTION 'agent_invalid'; END IF;
    IF p_action IS NOT NULL THEN
        IF p_action->>'expected_profile_version' IS NULL
            OR p_action->>'expected_profile_version' !~ '^[0-9a-f]{64}$'
            THEN RAISE EXCEPTION 'agent_stale_profile'; END IF;
        IF (p_action->'action'->>'expires_at')::timestamptz NOT BETWEEN now() AND now() + interval '24 hours'
            THEN RAISE EXCEPTION 'agent_expired'; END IF;
        IF p_action->>'target_plan_id' IS NOT NULL AND NOT EXISTS(SELECT 1 FROM saved_meal_plans
            WHERE user_id = p_user_id AND id = (p_action->>'target_plan_id')::uuid)
            THEN RAISE EXCEPTION 'agent_not_found'; END IF;
        INSERT INTO agent_actions(user_id, id, conversation_id, run_id, proposal, save_payload, target_plan_id, expected_plan_version, expected_profile_version, expires_at)
            VALUES(p_user_id, (p_action->'action'->>'id')::uuid, p_conversation_id, p_request_id, p_action->'action'->'proposal',
                p_action->'save_payload', (p_action->>'target_plan_id')::uuid, p_action->>'expected_plan_version',
                p_action->>'expected_profile_version', (p_action->'action'->>'expires_at')::timestamptz);
    END IF;
    UPDATE agent_conversations SET
        messages = (SELECT COALESCE(jsonb_agg(value ORDER BY ord), '[]') FROM (
            SELECT value, ord FROM jsonb_array_elements(messages || jsonb_build_array(jsonb_build_object(
                'id', gen_random_uuid(), 'role', 'user', 'text', r.request->>'text', 'created_at', r.created_at)) ||
                CASE WHEN COALESCE(p_assistant_text, '') = '' THEN '[]'::jsonb ELSE jsonb_build_array(jsonb_build_object(
                'id', gen_random_uuid(), 'role', 'assistant', 'text', p_assistant_text, 'created_at', now())) END)
                WITH ORDINALITY AS items(value, ord) ORDER BY ord DESC LIMIT 100) last_messages),
        -- Keep tool calls/results together: reset oversized replay rather than slicing items.
        sdk_history = CASE WHEN history_epoch = r.history_epoch AND jsonb_array_length(p_history) <= 128 AND octet_length(p_history::text) <= 262144 THEN p_history ELSE '[]'::jsonb END
        WHERE user_id = p_user_id AND id = p_conversation_id;
    UPDATE agent_runs SET status = terminal, events = p_events WHERE user_id = p_user_id AND id = p_request_id;
    RETURN agent_get_run(p_user_id, p_request_id);
END $$;

CREATE OR REPLACE FUNCTION public.agent_confirm_action(p_user_id uuid, p_action_id uuid, p_request_id uuid, p_proposal_version integer) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE a agent_actions; r agent_runs; c uuid; plan jsonb; saved_id uuid; events jsonb;
    request jsonb := jsonb_build_object('action_id', p_action_id, 'proposal_version', p_proposal_version);
BEGIN
    PERFORM 1 FROM users WHERE id = p_user_id FOR UPDATE;
    SELECT conversation_id INTO c FROM agent_actions WHERE user_id = p_user_id AND id = p_action_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    PERFORM 1 FROM agent_conversations WHERE user_id = p_user_id AND id = c AND expires_at > now() FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    SELECT * INTO a FROM agent_actions WHERE user_id = p_user_id AND id = p_action_id FOR UPDATE;
    SELECT * INTO r FROM agent_runs WHERE user_id = p_user_id AND id = p_request_id;
    IF FOUND THEN
        IF r.conversation_id <> c OR r.request <> request THEN RAISE EXCEPTION 'agent_conflict'; END IF;
        RETURN agent_get_run(p_user_id, p_request_id);
    END IF;
    PERFORM agent_take_quota(p_user_id);
    IF (a.proposal->>'version')::integer IS DISTINCT FROM p_proposal_version THEN RAISE EXCEPTION 'agent_conflict'; END IF;
    IF a.applied_plan_id IS NULL THEN
        -- The users row lock above also serializes ordinary profile UPDATEs and account deletion.
        IF a.expected_profile_version IS NULL OR a.expected_profile_version IS DISTINCT FROM
            (agent_get_profile(p_user_id)->>'version') THEN RAISE EXCEPTION 'agent_stale_profile'; END IF;
        IF a.expires_at <= now() THEN RAISE EXCEPTION 'agent_expired'; END IF;
        IF EXISTS(SELECT 1 FROM agent_runs WHERE user_id = p_user_id AND conversation_id = c AND status = 'running')
            THEN RAISE EXCEPTION 'agent_busy'; END IF;
        -- shortcut: legacy writers lack version checks; remove this table lock after every writer shares a verified unique/version protocol.
        LOCK TABLE saved_meal_plans IN SHARE ROW EXCLUSIVE MODE;
        IF EXISTS(SELECT 1 FROM saved_meal_plans WHERE user_id = p_user_id
            AND date = (a.proposal->>'date')::date AND id IS DISTINCT FROM a.target_plan_id)
            THEN RAISE EXCEPTION 'agent_stale_plan'; END IF;
        IF a.target_plan_id IS NOT NULL THEN
            SELECT to_jsonb(s) INTO plan FROM saved_meal_plans s WHERE user_id = p_user_id AND id = a.target_plan_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
            IF plan->>'dining_hall' IS DISTINCT FROM a.proposal->>'dining_hall'
                OR plan->>'date' IS DISTINCT FROM a.proposal->>'date'
                THEN RAISE EXCEPTION 'agent_stale_plan'; END IF;
            IF md5(plan::text) <> a.expected_plan_version THEN RAISE EXCEPTION 'agent_stale_plan'; END IF;
            UPDATE saved_meal_plans SET meal_plan_data = a.save_payload, updated_at = now(),
                dining_hall = a.proposal->>'dining_hall', date = (a.proposal->>'date')::date
                WHERE user_id = p_user_id AND id = a.target_plan_id RETURNING id INTO saved_id;
        ELSE
            IF EXISTS(SELECT 1 FROM saved_meal_plans WHERE user_id = p_user_id
                AND dining_hall = a.proposal->>'dining_hall' AND date = (a.proposal->>'date')::date)
                THEN RAISE EXCEPTION 'agent_stale_plan'; END IF;
            saved_id := gen_random_uuid();
            INSERT INTO saved_meal_plans(id, user_id, dining_hall, date, meal_plan_data, created_at, updated_at)
                VALUES(saved_id, p_user_id, a.proposal->>'dining_hall', (a.proposal->>'date')::date, a.save_payload, now(), now());
        END IF;
        UPDATE agent_actions SET applied_plan_id = saved_id WHERE user_id = p_user_id AND id = p_action_id;
    ELSE saved_id := a.applied_plan_id;
    END IF;
    events := jsonb_build_array(jsonb_build_object('version', 1, 'sequence', 1, 'run_id', p_request_id, 'type', 'complete',
        'data', jsonb_build_object('saved_plan_id', saved_id, 'preferences_changed', false, 'citations', '[]'::jsonb)));
    INSERT INTO agent_runs(user_id, id, conversation_id, request, status, events) VALUES(p_user_id, p_request_id, c, request, 'complete', events);
    RETURN agent_get_run(p_user_id, p_request_id);
END $$;

COMMIT;
