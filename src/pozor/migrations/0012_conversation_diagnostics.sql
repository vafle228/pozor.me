ALTER TABLE turn_inputs ADD COLUMN author_id INTEGER;
ALTER TABLE turns ADD COLUMN generation_reason TEXT;
ALTER TABLE turns ADD COLUMN fallback INTEGER NOT NULL DEFAULT 0 CHECK(fallback IN (0,1));
