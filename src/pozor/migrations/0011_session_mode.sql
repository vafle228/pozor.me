ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'roast' CHECK(mode IN ('roast','dialogue'));
