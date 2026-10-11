-- Schema first; no conversational writes until DORMDISH_EXPLICIT_MEMORY_ENABLED=true.
BEGIN;
-- Widen only the known meal discriminator check; an unknown additional check fails closed.
ALTER TABLE public.user_preferences DROP CONSTRAINT IF EXISTS user_preferences_meal_type_check;
ALTER TABLE public.user_preferences ADD CONSTRAINT user_preferences_meal_type_check
    CHECK (meal_type IN ('breakfast', 'lunch', 'dinner', 'all'));
ALTER TABLE public.user_preferences
    ADD COLUMN scope text NOT NULL DEFAULT 'meal' CHECK (scope IN ('meal', 'global')),
    ADD COLUMN preference_kind text CHECK (preference_kind IN ('food_item','meal_plan','flavor','cuisine','texture')),
    ADD COLUMN source text NOT NULL DEFAULT 'legacy' CHECK (source IN ('legacy','button','explicit_chat')),
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN source_conversation_id uuid,
    ADD COLUMN source_request_id uuid,
    ADD CHECK ((scope = 'global') = (meal_type = 'all'));
ALTER TABLE public.user_preferences ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.user_preferences FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.user_preferences TO service_role;

CREATE FUNCTION public.preference_apply(p_user_id uuid, p_category text, p_item_name text,
    p_kind text, p_meal_type text, p_source text, p_conversation_id uuid DEFAULT NULL,
    p_request_id uuid DEFAULT NULL, p_meal_plan_id text DEFAULT NULL, p_reason text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE chosen uuid; result user_preferences; global_row user_preferences; other_meal text;
BEGIN
    p_item_name := preference_item_name(p_item_name);
    IF p_category IS NULL OR p_category NOT IN ('favorite','dislike')
        OR p_kind IS NULL OR p_kind NOT IN ('food_item','meal_plan','flavor','cuisine','texture')
        OR p_meal_type IS NULL OR p_meal_type NOT IN ('breakfast','lunch','dinner','all')
        OR p_source IS NULL OR p_source NOT IN ('button','explicit_chat')
        OR p_item_name IS NULL OR length(p_item_name) NOT BETWEEN 1 AND 200
        OR length(p_reason) > 1000 OR length(p_meal_plan_id) > 200
        THEN RAISE EXCEPTION 'preference_invalid'; END IF;
    PERFORM 1 FROM users WHERE id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'preference_not_found'; END IF;
    IF p_meal_type <> 'all' THEN
        SELECT * INTO global_row FROM user_preferences WHERE user_id = p_user_id AND meal_type = 'all'
            AND lower(preference_item_name(item_name)) = lower(p_item_name) ORDER BY created_at DESC, id LIMIT 1;
        IF FOUND THEN
            -- A dinner override must not erase the earlier breakfast/lunch preference.
            FOREACH other_meal IN ARRAY ARRAY['breakfast','lunch','dinner'] LOOP
                IF other_meal <> p_meal_type THEN
                    DELETE FROM user_preferences WHERE user_id = p_user_id AND meal_type = other_meal
                        AND lower(preference_item_name(item_name)) = lower(p_item_name);
                    INSERT INTO user_preferences(user_id,category,item_name,item_type,meal_type,meal_plan_id,reason,
                        scope,preference_kind,source,source_conversation_id,source_request_id)
                        VALUES(p_user_id,global_row.category,global_row.item_name,global_row.item_type,other_meal,
                            global_row.meal_plan_id,global_row.reason,'meal',global_row.preference_kind,
                            global_row.source,global_row.source_conversation_id,global_row.source_request_id);
                END IF;
            END LOOP;
            DELETE FROM user_preferences WHERE user_id = p_user_id AND meal_type = 'all'
                AND lower(preference_item_name(item_name)) = lower(p_item_name);
        END IF;
    END IF;
    SELECT id INTO chosen FROM user_preferences WHERE user_id = p_user_id
        AND lower(preference_item_name(item_name)) = lower(p_item_name)
        AND (p_meal_type = 'all' OR meal_type = p_meal_type)
        ORDER BY (meal_type = p_meal_type) DESC, created_at DESC, id LIMIT 1;
    DELETE FROM user_preferences WHERE user_id = p_user_id AND lower(preference_item_name(item_name)) = lower(p_item_name)
        AND (p_meal_type = 'all' OR meal_type = p_meal_type) AND id IS DISTINCT FROM chosen;
    IF chosen IS NULL THEN
        INSERT INTO user_preferences(user_id,category,item_name,item_type,meal_type,meal_plan_id,reason,
            scope,preference_kind,source,source_conversation_id,source_request_id)
            VALUES(p_user_id,p_category,p_item_name,CASE WHEN p_kind = 'meal_plan' THEN 'meal_plan' ELSE 'food_item' END,
                p_meal_type,p_meal_plan_id,p_reason,CASE WHEN p_meal_type = 'all' THEN 'global' ELSE 'meal' END,
                p_kind,p_source,p_conversation_id,p_request_id) RETURNING * INTO result;
    ELSE
        UPDATE user_preferences SET category=p_category,item_name=p_item_name,
            item_type=CASE WHEN p_kind = 'meal_plan' THEN 'meal_plan' ELSE 'food_item' END,
            meal_type=p_meal_type,meal_plan_id=p_meal_plan_id,reason=p_reason,
            scope=CASE WHEN p_meal_type = 'all' THEN 'global' ELSE 'meal' END,preference_kind=p_kind,
            source=p_source,source_conversation_id=p_conversation_id,source_request_id=p_request_id,updated_at=now()
            WHERE user_id=p_user_id AND id=chosen RETURNING * INTO result;
    END IF;
    PERFORM agent_clear_history(p_user_id);
    RETURN jsonb_build_object('status','success','data',jsonb_build_array(to_jsonb(result)));
END $$;

CREATE OR REPLACE FUNCTION public.preference_set(p_user_id uuid, p_category text, p_item_name text,
    p_item_type text, p_meal_type text, p_meal_plan_id text DEFAULT NULL, p_reason text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF p_item_type IS NULL OR p_item_type NOT IN ('food_item','meal_plan')
        OR p_meal_type IS NULL OR p_meal_type NOT IN ('breakfast','lunch','dinner')
        THEN RAISE EXCEPTION 'preference_invalid'; END IF;
    RETURN preference_apply(p_user_id,p_category,p_item_name,p_item_type,p_meal_type,'button',NULL,NULL,p_meal_plan_id,p_reason);
END $$;

CREATE FUNCTION public.agent_memory_intent(p_text text) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path = public, pg_temp AS $$
DECLARE text_value text; found text[]; item text; category text; kind text := 'food_item'; meal text := 'all'; operation text;
BEGIN
    text_value := regexp_replace(preference_item_name(p_text), '^please +', '', 'i');
    text_value := regexp_replace(text_value, '[.!]$', '');
    IF lower(text_value) = 'forget that preference' THEN RETURN jsonb_build_object('operation','reference'); END IF;
    IF p_text ~ '[[:cntrl:]]' THEN RETURN NULL; END IF;
    found := regexp_match(text_value, '^remember(?: that)? i (like|love|prefer|dislike|don[''’]t like|do not like) (.+)$', 'i');
    IF found IS NOT NULL THEN
        operation := 'set'; item := found[2];
        category := CASE WHEN lower(found[1]) IN ('dislike','don''t like','don’t like','do not like') THEN 'dislike' ELSE 'favorite' END;
    ELSE
        found := regexp_match(text_value, '^remember my (flavor|cuisine|texture) (preference|dislike): +(.+)$', 'i');
        IF found IS NOT NULL THEN
            operation := 'set'; kind := lower(found[1]); item := found[3];
            category := CASE WHEN lower(found[2]) = 'dislike' THEN 'dislike' ELSE 'favorite' END;
        ELSE
            found := regexp_match(text_value, '^forget my (flavor|cuisine|texture) (preference|dislike): +(.+)$', 'i');
            IF found IS NOT NULL THEN
                operation := 'forget'; kind := lower(found[1]); item := found[3];
                category := CASE WHEN lower(found[2])='dislike' THEN 'dislike' ELSE 'favorite' END;
            ELSE
                found := regexp_match(text_value, '^forget (?:my (favorite|dislike) of )?(.+)$', 'i');
                IF found IS NULL THEN RETURN NULL; END IF;
                operation := 'forget'; category := lower(found[1]); item := found[2];
            END IF;
        END IF;
    END IF;
    -- A JSON string closes the target boundary; free multiword text is ambiguous.
    found := regexp_match(item, '^("(?:[^"\\]|\\.)*")(?: for (breakfast|lunch|dinner))?$', 'i');
    IF found IS NOT NULL THEN
        BEGIN
            item := found[1]::jsonb #>> '{}';
        EXCEPTION WHEN data_exception THEN RETURN NULL;
        END;
    ELSE
        found := regexp_match(item, '^([[:alnum:]]+([''’-][[:alnum:]]+)*)(?: for (breakfast|lunch|dinner))?$', 'i');
        IF found IS NULL THEN RETURN NULL; END IF;
        item := found[1]; found := ARRAY[found[1],found[3]];
        IF item ~* '\m(tonight|today|tomorrow|temporarily|temporary|briefly|now|never|not|no|maybe|perhaps|probably|that|it|this|everything|allergies|allergy|allergic|restriction|restrictions)\M'
            THEN RETURN NULL; END IF;
    END IF;
    IF found[2] IS NOT NULL THEN
        IF operation <> 'set' THEN RETURN NULL; END IF;
        meal := lower(found[2]);
    END IF;
    item := preference_item_name(item);
    IF length(item) NOT BETWEEN 1 AND 200 OR item ~ '[[:cntrl:]]' THEN RETURN NULL; END IF;
    RETURN jsonb_build_object('operation',operation,'item_name',item,'category',category,'preference_kind',kind,'meal_type',meal,'exact_kind',kind<>'food_item');
END $$;

CREATE FUNCTION public.agent_memory_turn(p_user_id uuid, p_conversation_id uuid, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r agent_runs; intent jsonb; changed boolean := false; text_value text; events jsonb; found_rows jsonb; prior_id uuid; prior_intent jsonb;
BEGIN
    PERFORM 1 FROM users WHERE id=p_user_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    PERFORM 1 FROM agent_conversations WHERE user_id=p_user_id AND id=p_conversation_id AND expires_at>now() FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    SELECT * INTO r FROM agent_runs WHERE user_id=p_user_id AND conversation_id=p_conversation_id AND id=p_request_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_not_found'; END IF;
    IF r.status <> 'running' THEN RETURN agent_get_run(p_user_id,p_request_id); END IF;
    IF r.deadline<=now() THEN RAISE EXCEPTION 'agent_expired'; END IF;
    IF r.history_epoch IS DISTINCT FROM (SELECT history_epoch FROM agent_conversations
        WHERE user_id=p_user_id AND id=p_conversation_id) THEN
        text_value := 'Your preferences changed while this request was pending. Repeat the exact preference instruction if you still want it applied.';
    ELSE
        intent := agent_memory_intent(r.request->>'text');
    END IF;
    IF intent->>'operation'='reference' THEN
        SELECT id,agent_memory_intent(request->>'text') INTO prior_id,prior_intent FROM agent_runs WHERE user_id=p_user_id AND conversation_id=p_conversation_id AND id<>p_request_id
            ORDER BY created_at DESC,id DESC LIMIT 1;
        SELECT jsonb_agg(jsonb_build_object('operation','forget','item_name',item_name,'category',category,
            'preference_kind',coalesce(preference_kind,item_type),'meal_type',meal_type,'exact_scope',true,'exact_kind',true)) INTO found_rows
            FROM user_preferences WHERE user_id=p_user_id AND source='explicit_chat'
            AND source_conversation_id=p_conversation_id AND source_request_id=prior_id
            AND prior_intent->>'operation'='set' AND meal_type=prior_intent->>'meal_type'
            AND category=prior_intent->>'category' AND lower(item_name)=lower(prior_intent->>'item_name');
        intent := CASE WHEN jsonb_array_length(found_rows)=1 THEN found_rows->0 ELSE NULL END;
    END IF;
    IF text_value IS NOT NULL THEN
        NULL; -- A stale in-flight instruction cannot resurrect a forgotten preference.
    ELSIF intent IS NULL THEN
        text_value := 'Which preference should I remember or forget? Say Remember that I like "brown rice" or Forget "brown rice". Use a single name or a JSON-quoted literal, with an optional meal such as for dinner. Temporary requests stay temporary; change allergies and nutrition goals in your profile.';
    ELSIF intent->>'operation'='set' THEN
        PERFORM preference_apply(p_user_id,intent->>'category',intent->>'item_name',intent->>'preference_kind',
            intent->>'meal_type','explicit_chat',p_conversation_id,p_request_id);
        changed := true;
        text_value := 'Remembered your ' || (intent->>'category') || ' preference for ' || (intent->>'item_name') ||
            CASE WHEN intent->>'meal_type'='all' THEN ' for all meals.' ELSE ' for ' || (intent->>'meal_type') || '.' END;
    ELSE
        DELETE FROM user_preferences WHERE user_id=p_user_id
            AND lower(preference_item_name(item_name))=lower(intent->>'item_name')
            AND (intent->>'category' IS NULL OR category=intent->>'category')
            AND (coalesce((intent->>'exact_scope')::boolean,false)=false OR meal_type=intent->>'meal_type')
            AND (coalesce((intent->>'exact_kind')::boolean,false)=false OR coalesce(preference_kind,item_type)=intent->>'preference_kind');
        PERFORM agent_clear_history(p_user_id);
        changed := true;
        text_value := 'Forgot the preference for ' || (intent->>'item_name') || '.';
    END IF;
    events := jsonb_build_array(
        jsonb_build_object('version',1,'sequence',1,'run_id',p_request_id,'type','text_delta','data',jsonb_build_object('text',text_value)),
        jsonb_build_object('version',1,'sequence',2,'run_id',p_request_id,'type','complete','data',jsonb_build_object(
            'saved_plan_id',NULL,'preferences_changed',changed,'citations','[]'::jsonb)));
    RETURN agent_finish_turn(p_user_id,p_conversation_id,p_request_id,events,'[]'::jsonb,text_value);
END $$;
REVOKE ALL ON FUNCTION public.preference_apply(uuid,text,text,text,text,text,uuid,uuid,text,text),
    public.agent_memory_intent(text), public.agent_memory_turn(uuid,uuid,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.preference_apply(uuid,text,text,text,text,text,uuid,uuid,text,text),
    public.agent_memory_intent(text), public.agent_memory_turn(uuid,uuid,uuid) TO service_role;
COMMIT;
