-- Keep configured scope distinct from the later operator/admin enable switch.
ALTER TABLE chats ADD COLUMN allowed INTEGER NOT NULL DEFAULT 0 CHECK (allowed IN (0, 1));
