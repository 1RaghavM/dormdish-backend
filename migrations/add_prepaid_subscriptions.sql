-- Pre-paid subscription purchases via Stripe (deferred activation)
CREATE TABLE IF NOT EXISTS prepaid_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    stripe_session_id TEXT NOT NULL UNIQUE,
    stripe_payment_intent_id TEXT,
    plan_type TEXT NOT NULL CHECK (plan_type IN ('monthly', 'yearly')),
    amount_cents INTEGER NOT NULL,
    revenuecat_duration TEXT NOT NULL CHECK (revenuecat_duration IN ('monthly', 'yearly')),
    activation_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'activated', 'refunded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ
);

CREATE INDEX idx_prepaid_subscriptions_status ON prepaid_subscriptions(status);
CREATE INDEX idx_prepaid_subscriptions_user_id ON prepaid_subscriptions(user_id);
CREATE INDEX idx_prepaid_subscriptions_activation ON prepaid_subscriptions(activation_date) WHERE status = 'pending';
