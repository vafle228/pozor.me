ALTER TABLE chats ADD COLUMN roast_level INTEGER NOT NULL DEFAULT 3 CHECK(roast_level BETWEEN 1 AND 3);
CREATE TRIGGER hook_excluded AFTER UPDATE OF excluded ON profile_items
WHEN new.kind='hook' AND old.excluded=0 AND new.excluded=1 BEGIN
    UPDATE sessions SET state='closed',closed_reason='hook_excluded',revision=revision+1
    WHERE chat_id=new.chat_id AND target_id=new.target_id AND state='active';
    DELETE FROM generations WHERE chat_id=new.chat_id AND target_id=new.target_id;
END;
