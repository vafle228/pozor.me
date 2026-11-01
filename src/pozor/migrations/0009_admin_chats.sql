ALTER TABLE chats ADD COLUMN managed_by_admin INTEGER NOT NULL DEFAULT 0
    CHECK (managed_by_admin IN (0, 1));
ALTER TABLE chats ADD COLUMN title TEXT;
-- Apply the shorter cooldown to already running installations as well.
UPDATE command_cooldowns SET until_at = min(until_at,
    CAST(strftime('%s','now') AS INTEGER) + 10);
