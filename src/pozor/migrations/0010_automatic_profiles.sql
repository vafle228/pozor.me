CREATE TABLE automatic_profiles (
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    message_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    attempted_at REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending',
    analyzed INTEGER NOT NULL DEFAULT 0,
    sampled INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    PRIMARY KEY(chat_id,target_id)
);
CREATE TRIGGER automatic_profile_epoch_update AFTER UPDATE OF epoch ON data_epochs BEGIN
    DELETE FROM automatic_profiles WHERE chat_id=new.chat_id AND target_id=new.target_id;
END;
CREATE TRIGGER automatic_profile_epoch_insert AFTER INSERT ON data_epochs BEGIN
    DELETE FROM automatic_profiles WHERE chat_id=new.chat_id AND target_id=new.target_id;
END;
