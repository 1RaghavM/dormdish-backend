-- Additive, schema first. Custom authentication uses public.users; service RPCs only.
BEGIN;
ALTER TABLE public.saved_meal_plans ADD COLUMN completion_version uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE public.food_log ADD COLUMN source_id text;
CREATE FUNCTION public.meal_completion_version() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF ROW(NEW.user_id, NEW.date, NEW.meal_plan_data) IS DISTINCT FROM
       ROW(OLD.user_id, OLD.date, OLD.meal_plan_data) THEN
        NEW.completion_version := gen_random_uuid();
    ELSE
        NEW.completion_version := OLD.completion_version;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER meal_completion_version BEFORE UPDATE ON public.saved_meal_plans
    FOR EACH ROW EXECUTE FUNCTION public.meal_completion_version();

-- No FK to saved plans: replacing/deleting a plan must not erase food already eaten.
CREATE TABLE public.meal_completions (
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    plan_id uuid NOT NULL,
    plan_version uuid NOT NULL,
    date date NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    recorded_items jsonb NOT NULL CHECK (jsonb_typeof(recorded_items) = 'array' AND jsonb_array_length(recorded_items) <= 90),
    selected_items jsonb NOT NULL CHECK (jsonb_typeof(selected_items) = 'array' AND jsonb_array_length(selected_items) <= 90),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, plan_id, plan_version),
    CHECK (octet_length(recorded_items::text) <= 65536)
);
CREATE INDEX ON public.meal_completions(user_id, date);
CREATE TABLE public.meal_completion_requests (
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    request jsonb NOT NULL CHECK (octet_length(request::text) <= 16384),
    state jsonb NOT NULL CHECK (octet_length(state::text) <= 98304),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, request_id)
);
CREATE INDEX ON public.meal_completion_requests(user_id, created_at);
ALTER TABLE public.meal_completions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meal_completion_requests ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.meal_completions, public.meal_completion_requests FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.meal_completions, public.meal_completion_requests TO service_role;

CREATE FUNCTION public.meal_completion_state(p_row meal_completions) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT (to_jsonb(p_row) - 'user_id' - 'updated_at') || jsonb_build_object(
        'items', coalesce(jsonb_agg(food), '[]'), 'totals', jsonb_build_object(
            'calories', coalesce(sum((food->'totals'->>'calories')::numeric), 0),
            'protein_g', coalesce(sum((food->'totals'->>'protein_g')::numeric), 0),
            'fat_g', coalesce(sum((food->'totals'->>'fat_g')::numeric), 0),
            'carbs_g', coalesce(sum((food->'totals'->>'carbs_g')::numeric), 0)))
        FROM jsonb_array_elements(p_row.recorded_items) food WHERE EXISTS
            (SELECT 1 FROM jsonb_array_elements(p_row.selected_items) chosen
             WHERE chosen->>'meal_type' = food->>'meal_type' AND chosen->'index' = food->'index')
$$;
CREATE FUNCTION public.meal_completion_current(p_user_id uuid, p_plan_id uuid) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE plan saved_meal_plans; state jsonb;
BEGIN
    SELECT * INTO plan FROM saved_meal_plans WHERE id = p_plan_id AND user_id = p_user_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'completion_not_found'; END IF;
    SELECT meal_completion_state(c) INTO state FROM meal_completions c
        WHERE user_id = p_user_id AND plan_id = p_plan_id AND plan_version = plan.completion_version;
    RETURN jsonb_build_object('plan', to_jsonb(plan), 'state', coalesce(state,
        jsonb_build_object('plan_id', plan.id, 'plan_version', plan.completion_version,
            'date', plan.date, 'revision', 0, 'recorded_items', '[]'::jsonb, 'selected_items', '[]'::jsonb,
            'items', '[]'::jsonb, 'totals', jsonb_build_object('calories',0,'protein_g',0,'fat_g',0,'carbs_g',0))));
END $$;
CREATE FUNCTION public.meal_completion_list(p_user_id uuid, p_date date) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE result jsonb;
BEGIN
    SELECT coalesce(jsonb_agg(meal_completion_state(c) ORDER BY plan_id, plan_version), '[]') INTO result
        FROM meal_completions c WHERE user_id = p_user_id AND date = p_date;
    RETURN result;
END $$;
CREATE FUNCTION public.meal_completion_request(p_user_id uuid, p_request_id uuid) RETURNS jsonb
LANGUAGE sql SET search_path = public, pg_temp AS $$
    SELECT jsonb_build_object('request', request, 'state', state) FROM meal_completion_requests
        WHERE user_id = p_user_id AND request_id = p_request_id
$$;
CREATE FUNCTION public.meal_completion_write(p_user_id uuid, p_plan_id uuid, p_request jsonb, p_items jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE receipt meal_completion_requests; prior meal_completions; result meal_completions;
    plan saved_meal_plans; version uuid; chosen_date date; selected jsonb; item jsonb;
    catalog jsonb; recorded jsonb; saved_food jsonb; nutrient text; v_request_id uuid; expected integer; state jsonb;
BEGIN
    PERFORM 1 FROM users WHERE id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'completion_not_found'; END IF;
    v_request_id := (p_request->>'request_id')::uuid;
    version := (p_request->>'plan_version')::uuid;
    chosen_date := (p_request->>'date')::date;
    expected := (p_request->>'expected_revision')::integer;
    selected := p_request->'selected_items';
    IF v_request_id IS NULL OR version IS NULL OR chosen_date IS NULL OR expected IS NULL OR expected < 0
        OR p_request->>'plan_id' IS DISTINCT FROM p_plan_id::text
        OR p_request->>'mode' IS NULL OR p_request->>'mode' NOT IN ('current', 'frozen')
        OR jsonb_typeof(selected) IS DISTINCT FROM 'array' OR jsonb_array_length(selected) > 90
        OR octet_length(p_request::text) > 16384 THEN RAISE EXCEPTION 'completion_invalid'; END IF;
    SELECT * INTO receipt FROM meal_completion_requests WHERE user_id = p_user_id AND meal_completion_requests.request_id = v_request_id;
    IF FOUND THEN
        IF receipt.request <> p_request THEN RAISE EXCEPTION 'completion_conflict'; END IF;
        RETURN receipt.state;
    END IF;
    IF (SELECT count(*) FROM meal_completion_requests WHERE user_id = p_user_id
        AND created_at > now() - interval '1 day') >= 1000 THEN RAISE EXCEPTION 'completion_quota'; END IF;
    SELECT * INTO prior FROM meal_completions WHERE user_id = p_user_id AND plan_id = p_plan_id AND plan_version = version;
    IF expected <> coalesce(prior.revision, 0) OR (prior.date IS NOT NULL AND prior.date <> chosen_date)
        THEN RAISE EXCEPTION 'completion_conflict'; END IF;
    IF p_request->>'mode' = 'current' THEN
        -- Lock order agrees with agent saves: owner first, then saved plan.
        SELECT * INTO plan FROM saved_meal_plans WHERE id = p_plan_id AND user_id = p_user_id FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'completion_not_found'; END IF;
        IF plan.completion_version <> version OR plan.date <> chosen_date THEN RAISE EXCEPTION 'completion_stale_plan'; END IF;
        catalog := p_items;
        -- Verify the service snapshot against the locked row as well as its version.
        FOR item IN SELECT value FROM jsonb_array_elements(catalog) LOOP
            saved_food := coalesce(plan.meal_plan_data->'meal_plan', plan.meal_plan_data)
                ->'meals'->(item->>'meal_type')->'items'->((item->>'index')::integer);
            IF saved_food IS NULL OR item->>'name' IS DISTINCT FROM saved_food->>'name'
                OR item->>'source_id' IS DISTINCT FROM ('completion:' || p_plan_id || ':' || version || ':' ||
                    (item->>'meal_type') || ':' || (item->>'index')) THEN RAISE EXCEPTION 'completion_invalid'; END IF;
            FOREACH nutrient IN ARRAY ARRAY['calories','protein_g','fat_g','carbs_g'] LOOP
                IF jsonb_typeof(saved_food->nutrient) IS DISTINCT FROM 'number'
                    OR NOT ((saved_food->>nutrient)::numeric BETWEEN 0 AND 100000)
                    OR item->'totals'->nutrient IS DISTINCT FROM saved_food->nutrient
                    THEN RAISE EXCEPTION 'completion_invalid'; END IF;
            END LOOP;
        END LOOP;
    ELSE
        IF prior.plan_id IS NULL THEN RAISE EXCEPTION 'completion_not_found'; END IF;
        catalog := prior.recorded_items;
    END IF;
    IF jsonb_typeof(catalog) IS DISTINCT FROM 'array' OR jsonb_array_length(catalog) > 90
        OR octet_length(catalog::text) > 65536 THEN RAISE EXCEPTION 'completion_invalid'; END IF;
    IF (SELECT count(*) FROM jsonb_array_elements(selected)) <>
       (SELECT count(DISTINCT value) FROM jsonb_array_elements(selected)) THEN RAISE EXCEPTION 'completion_invalid'; END IF;
    recorded := coalesce(prior.recorded_items, '[]');
    FOR item IN SELECT value FROM jsonb_array_elements(selected) LOOP
        IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(catalog) f
            WHERE f->>'meal_type' = item->>'meal_type' AND f->'index' = item->'index')
            THEN RAISE EXCEPTION 'completion_invalid'; END IF;
        IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(recorded) f
            WHERE f->>'meal_type' = item->>'meal_type' AND f->'index' = item->'index') THEN
            recorded := recorded || (SELECT jsonb_build_array(f) FROM jsonb_array_elements(catalog) f
                WHERE f->>'meal_type' = item->>'meal_type' AND f->'index' = item->'index');
        END IF;
    END LOOP;
    IF prior.plan_id IS NULL AND (SELECT count(*) FROM meal_completions WHERE user_id = p_user_id AND date = chosen_date) >= 100
        THEN RAISE EXCEPTION 'completion_quota'; END IF;
    INSERT INTO meal_completions(user_id, plan_id, plan_version, date, revision, recorded_items, selected_items)
        VALUES(p_user_id, p_plan_id, version, chosen_date, expected + 1, recorded, selected)
        ON CONFLICT (user_id, plan_id, plan_version) DO UPDATE SET revision = EXCLUDED.revision,
            recorded_items = EXCLUDED.recorded_items, selected_items = EXCLUDED.selected_items, updated_at = now()
        RETURNING * INTO result;
    state := meal_completion_state(result);
    INSERT INTO meal_completion_requests(user_id, request_id, request, state) VALUES(p_user_id, v_request_id, p_request, state);
    RETURN state;
END $$;
CREATE FUNCTION public.meal_completion_intake(p_user_id uuid, p_date date) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE logs jsonb; completed jsonb;
BEGIN
    SELECT coalesce(jsonb_agg(to_jsonb(l)), '[]') INTO logs FROM
        (SELECT id, user_id, date, source_id, calories, protein_g, fat_g, carbs_g FROM food_log
         WHERE user_id = p_user_id AND date::text = p_date::text LIMIT 501) l;
    SELECT coalesce(jsonb_agg(jsonb_build_object('user_id', p_user_id, 'date', p_date,
        'completed', true, 'source_id', food->>'source_id', 'totals', food->'totals')), '[]') INTO completed
        FROM meal_completions c, jsonb_array_elements(c.recorded_items) food
        WHERE c.user_id = p_user_id AND c.date = p_date AND EXISTS
            (SELECT 1 FROM jsonb_array_elements(c.selected_items) chosen
             WHERE chosen->>'meal_type' = food->>'meal_type' AND chosen->'index' = food->'index');
    IF jsonb_array_length(logs) > 500 OR jsonb_array_length(completed) > 500 THEN RAISE EXCEPTION 'completion_quota'; END IF;
    RETURN jsonb_build_object('food_logs', logs, 'completions', completed);
END $$;
REVOKE ALL ON FUNCTION public.meal_completion_version(), public.meal_completion_state(meal_completions),
    public.meal_completion_current(uuid, uuid), public.meal_completion_list(uuid, date),
    public.meal_completion_request(uuid, uuid), public.meal_completion_write(uuid, uuid, jsonb, jsonb),
    public.meal_completion_intake(uuid, date) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.meal_completion_state(meal_completions), public.meal_completion_current(uuid, uuid),
    public.meal_completion_list(uuid, date), public.meal_completion_request(uuid, uuid),
    public.meal_completion_write(uuid, uuid, jsonb, jsonb), public.meal_completion_intake(uuid, date) TO service_role;
COMMIT;
