-- Additive only. Apply before enabling agent routes. Custom sessions use public.users.
BEGIN;
CREATE TABLE public.agent_quotas (
    user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    window_start timestamptz NOT NULL DEFAULT now(),
    requests integer NOT NULL DEFAULT 0
);
ALTER TABLE public.agent_quotas ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.agent_quotas FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.agent_quotas TO service_role;
CREATE FUNCTION public.agent_take_quota(p_user_id uuid) RETURNS void
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE used integer;
BEGIN
    INSERT INTO agent_quotas(user_id, requests) VALUES(p_user_id, 1)
    ON CONFLICT (user_id) DO UPDATE SET
        requests = CASE WHEN agent_quotas.window_start <= now() - interval '1 hour' THEN 1 ELSE agent_quotas.requests + 1 END,
        window_start = CASE WHEN agent_quotas.window_start <= now() - interval '1 hour' THEN now() ELSE agent_quotas.window_start END
    RETURNING requests INTO used;
    IF used > 60 THEN RAISE EXCEPTION 'agent_quota'; END IF;
END $$;
CREATE TABLE public.agent_conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    messages jsonb NOT NULL DEFAULT '[]',
    sdk_history jsonb NOT NULL DEFAULT '[]',
    history_epoch integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '30 days'),
    UNIQUE (user_id, id),
    CHECK (jsonb_typeof(messages) = 'array' AND jsonb_array_length(messages) <= 100),
    CHECK (jsonb_typeof(sdk_history) = 'array' AND jsonb_array_length(sdk_history) <= 128),
    CHECK (octet_length(sdk_history::text) <= 262144)
);
CREATE INDEX ON public.agent_conversations (expires_at);
CREATE TABLE public.agent_runs (
    user_id uuid NOT NULL,
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    request jsonb NOT NULL,
    history_epoch integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'complete', 'error')),
    events jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(events) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    deadline timestamptz NOT NULL DEFAULT (now() + interval '5 minutes'),
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, conversation_id) REFERENCES public.agent_conversations(user_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX agent_one_running_turn ON public.agent_runs (user_id, conversation_id) WHERE status = 'running';
CREATE INDEX ON public.agent_runs (user_id, created_at);
CREATE TABLE public.agent_actions (
    user_id uuid NOT NULL,
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    run_id uuid NOT NULL,
    proposal jsonb NOT NULL,
    save_payload jsonb NOT NULL,
    target_plan_id uuid,
    expected_plan_version text,
    expires_at timestamptz NOT NULL,
    applied_plan_id uuid,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, conversation_id) REFERENCES public.agent_conversations(user_id, id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, run_id) REFERENCES public.agent_runs(user_id, id) ON DELETE CASCADE,
    CHECK ((target_plan_id IS NULL) = (expected_plan_version IS NULL)),
    CHECK (jsonb_typeof(proposal) = 'object' AND jsonb_typeof(save_payload) = 'object')
);
CREATE INDEX ON public.agent_actions (expires_at);
ALTER TABLE public.agent_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_actions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.agent_conversations, public.agent_runs, public.agent_actions FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.agent_conversations, public.agent_runs, public.agent_actions TO service_role;

-- SECURITY INVOKER throughout: only the backend service role can call these.
CREATE FUNCTION public.agent_create_conversation(p_user_id uuid) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE c agent_conversations;
BEGIN
    PERFORM 1 FROM users WHERE id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    IF (SELECT count(*) FROM agent_conversations WHERE user_id = p_user_id AND expires_at > now()) >= 20
    THEN RAISE EXCEPTION 'agent_quota'; END IF;
    INSERT INTO agent_conversations(user_id) VALUES(p_user_id) RETURNING * INTO c;
    RETURN jsonb_build_object('id', c.id, 'messages', c.messages, 'pending_actions', '[]'::jsonb, 'active_run_id', NULL);
END $$;

CREATE FUNCTION public.agent_get_conversation(p_user_id uuid, p_conversation_id uuid, p_history boolean DEFAULT false) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE c agent_conversations;
BEGIN
    SELECT * INTO c FROM agent_conversations WHERE user_id = p_user_id AND id = p_conversation_id AND expires_at > now();
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    IF p_history THEN RETURN c.sdk_history; END IF;
    RETURN jsonb_build_object('id', c.id, 'messages', c.messages,
        'pending_actions', COALESCE((SELECT jsonb_agg(jsonb_build_object('id', id, 'proposal', proposal, 'expires_at', expires_at) ORDER BY expires_at, id)
            FROM agent_actions WHERE user_id = p_user_id AND conversation_id = c.id AND expires_at > now() AND applied_plan_id IS NULL), '[]'),
        'active_run_id', (SELECT id FROM agent_runs WHERE user_id = p_user_id AND conversation_id = c.id AND status = 'running'));
END $$;

CREATE FUNCTION public.agent_get_run(p_user_id uuid, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r agent_runs; c uuid;
BEGIN
    SELECT conversation_id INTO c FROM agent_runs WHERE user_id = p_user_id AND id = p_request_id;
    PERFORM 1 FROM agent_conversations WHERE user_id = p_user_id AND id = c AND expires_at > now() FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    UPDATE agent_runs SET status = 'error', events = jsonb_build_array(jsonb_build_object(
        'version', 1, 'sequence', 1, 'run_id', id, 'type', 'error', 'data', jsonb_build_object(
        'code', 'run_expired', 'message', 'This turn expired. Review saved state before trying again.', 'retryable', false)))
        WHERE user_id = p_user_id AND id = p_request_id AND status = 'running' AND deadline <= now();
    SELECT r0.* INTO r FROM agent_runs r0 JOIN agent_conversations c ON c.id = r0.conversation_id AND c.user_id = r0.user_id
        WHERE r0.user_id = p_user_id AND r0.id = p_request_id AND c.expires_at > now();
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    RETURN jsonb_build_object('id', r.id, 'conversation_id', r.conversation_id, 'status', r.status, 'events', r.events);
END $$;

CREATE FUNCTION public.agent_claim_turn(p_user_id uuid, p_conversation_id uuid, p_request_id uuid, p_request jsonb) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r agent_runs;
BEGIN
    PERFORM 1 FROM users WHERE id = p_user_id FOR UPDATE;
    PERFORM 1 FROM agent_conversations WHERE user_id = p_user_id AND id = p_conversation_id AND expires_at > now() FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    SELECT * INTO r FROM agent_runs WHERE user_id = p_user_id AND id = p_request_id;
    IF FOUND THEN
        IF r.conversation_id <> p_conversation_id OR r.request <> p_request THEN RAISE EXCEPTION 'agent_conflict'; END IF;
        RETURN jsonb_build_object('claimed', false, 'run', agent_get_run(p_user_id, p_request_id));
    END IF;
    -- A crashed worker cannot finish an expired run. Never restart its work implicitly.
    UPDATE agent_runs SET status = 'error', events = jsonb_build_array(jsonb_build_object(
        'version', 1, 'sequence', 1, 'run_id', id, 'type', 'error', 'data', jsonb_build_object(
        'code', 'run_expired', 'message', 'This turn expired. Review saved state before trying again.', 'retryable', false)))
        WHERE user_id = p_user_id AND conversation_id = p_conversation_id AND status = 'running' AND deadline <= now();
    IF EXISTS(SELECT 1 FROM agent_runs WHERE user_id = p_user_id AND conversation_id = p_conversation_id AND status = 'running')
        THEN RAISE EXCEPTION 'agent_busy'; END IF;
    PERFORM agent_take_quota(p_user_id);
    INSERT INTO agent_runs(user_id, id, conversation_id, request, history_epoch)
        SELECT p_user_id, p_request_id, p_conversation_id, p_request, history_epoch FROM agent_conversations
        WHERE user_id = p_user_id AND id = p_conversation_id;
    RETURN jsonb_build_object('claimed', true, 'run', agent_get_run(p_user_id, p_request_id));
END $$;

CREATE FUNCTION public.agent_finish_turn(p_user_id uuid, p_conversation_id uuid, p_request_id uuid,
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
        IF (p_action->'action'->>'expires_at')::timestamptz NOT BETWEEN now() AND now() + interval '24 hours'
            THEN RAISE EXCEPTION 'agent_expired'; END IF;
        IF p_action->>'target_plan_id' IS NOT NULL AND NOT EXISTS(SELECT 1 FROM saved_meal_plans
            WHERE user_id = p_user_id AND id = (p_action->>'target_plan_id')::uuid)
            THEN RAISE EXCEPTION 'agent_not_found'; END IF;
        INSERT INTO agent_actions(user_id, id, conversation_id, run_id, proposal, save_payload, target_plan_id, expected_plan_version, expires_at)
            VALUES(p_user_id, (p_action->'action'->>'id')::uuid, p_conversation_id, p_request_id, p_action->'action'->'proposal',
                p_action->'save_payload', (p_action->>'target_plan_id')::uuid, p_action->>'expected_plan_version',
                (p_action->'action'->>'expires_at')::timestamptz);
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

CREATE FUNCTION public.agent_get_plan(p_user_id uuid, p_plan_id uuid) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE plan jsonb;
BEGIN
    SELECT to_jsonb(s) INTO plan FROM saved_meal_plans s WHERE user_id = p_user_id AND id = p_plan_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    RETURN jsonb_build_object('plan', plan, 'version', md5(plan::text));
END $$;

CREATE FUNCTION public.agent_confirm_action(p_user_id uuid, p_action_id uuid, p_request_id uuid, p_proposal_version integer) RETURNS jsonb
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
        IF a.target_plan_id IS NOT NULL THEN
            SELECT to_jsonb(s) INTO plan FROM saved_meal_plans s WHERE user_id = p_user_id AND id = a.target_plan_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
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

CREATE FUNCTION public.agent_delete_conversation(p_user_id uuid, p_conversation_id uuid) RETURNS void
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    DELETE FROM agent_conversations WHERE user_id = p_user_id AND id = p_conversation_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
END $$;

CREATE FUNCTION public.agent_clear_history(p_user_id uuid) RETURNS void
LANGUAGE sql SET search_path = public, pg_temp AS $$
    UPDATE agent_conversations SET sdk_history = '[]'::jsonb, history_epoch = history_epoch + 1 WHERE user_id = p_user_id;
$$;

CREATE FUNCTION public.agent_purge_expired() RETURNS void
LANGUAGE sql SET search_path = public, pg_temp AS $$
    DELETE FROM agent_actions WHERE expires_at <= now() AND applied_plan_id IS NULL;
    DELETE FROM agent_conversations WHERE expires_at <= now();
$$;

-- No Supabase auth.uid() policy: application sessions are not Supabase JWT identities.
DO $$ DECLARE f regprocedure;
BEGIN
    FOR f IN SELECT oid::regprocedure FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname IN (
        'agent_take_quota', 'agent_create_conversation', 'agent_get_conversation', 'agent_get_run', 'agent_claim_turn', 'agent_finish_turn',
        'agent_get_plan', 'agent_confirm_action', 'agent_delete_conversation', 'agent_clear_history', 'agent_purge_expired') LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated', f);
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f);
    END LOOP;
END $$;
COMMIT;
