-- Add custom nutrition goal override columns to users table.
-- When non-null, these override the algorithmically calculated values.
-- When null, calculated values from BMR/TDEE pipeline are used.

ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_calories integer;
ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_protein_g integer;
ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_fat_g integer;
ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_carbs_g integer;

COMMENT ON COLUMN users.custom_calories IS 'User-set calorie override. Null = use calculated value.';
COMMENT ON COLUMN users.custom_protein_g IS 'User-set protein override in grams. Null = use calculated value.';
COMMENT ON COLUMN users.custom_fat_g IS 'User-set fat override in grams. Null = use calculated value.';
COMMENT ON COLUMN users.custom_carbs_g IS 'User-set carbs override in grams. Null = use calculated value.';
