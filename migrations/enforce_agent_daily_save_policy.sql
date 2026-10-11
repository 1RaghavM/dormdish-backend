-- Forward-only policy update after add_agent_persistence.sql. No data rewrite.
BEGIN;
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
