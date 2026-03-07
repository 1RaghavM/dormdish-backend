-- Announcements table
CREATE TABLE IF NOT EXISTS announcements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    body TEXT,
    type TEXT NOT NULL DEFAULT 'banner' CHECK (type IN ('banner', 'modal', 'bottom_sheet', 'full_screen')),
    icon_name TEXT,
    image_url TEXT,
    cta_text TEXT,
    cta_deeplink TEXT,
    bg_color TEXT DEFAULT '#34D399',
    priority INTEGER DEFAULT 80,
    source TEXT DEFAULT 'server',
    screen TEXT DEFAULT 'home',
    max_impressions_per_user INTEGER DEFAULT 1,
    auto_dismiss_seconds INTEGER,
    target_university_ids TEXT[],
    target_min_app_version TEXT,
    target_subscription_tiers TEXT[],
    target_platforms TEXT[] DEFAULT ARRAY['ios'],
    is_active BOOLEAN DEFAULT true,
    start_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    end_date TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Interaction tracking table
CREATE TABLE IF NOT EXISTS announcement_interactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    announcement_id UUID REFERENCES announcements(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    interaction_type TEXT NOT NULL CHECK (interaction_type IN ('impression', 'tap', 'dismiss')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast user+announcement lookups
CREATE INDEX IF NOT EXISTS idx_announcement_interactions_user
    ON announcement_interactions(user_id, announcement_id);

-- Index for active announcements query
CREATE INDEX IF NOT EXISTS idx_announcements_active
    ON announcements(is_active, start_date, end_date);
