ALTER TABLE imports ADD COLUMN retained_until TEXT;
UPDATE imports SET retained_until=datetime(created_at,'+90 days') WHERE status='active';
CREATE TABLE forgotten_members (
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    policy_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE profile_watermarks (
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY(chat_id,target_id)
);
CREATE TABLE profile_cards (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    summary TEXT NOT NULL,
    attribution TEXT NOT NULL CHECK(attribution IN ('observed_self','imported_quote','third_party')),
    kind TEXT NOT NULL CHECK(kind IN ('self_quote','self_pattern','imported_quote','third_party_joke')),
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    episodes INTEGER NOT NULL CHECK(episodes>0),
    excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0,1)),
    profile_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX profile_scope ON profile_cards(chat_id,target_id);
CREATE TABLE card_evidence (
    card_id INTEGER NOT NULL REFERENCES profile_cards(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_text_hash TEXT NOT NULL,
    PRIMARY KEY(card_id,message_id)
);
CREATE TRIGGER card_source_delete BEFORE DELETE ON messages BEGIN
    DELETE FROM profile_cards WHERE id IN (SELECT card_id FROM card_evidence WHERE message_id=old.id);
END;
CREATE TRIGGER card_source_edit BEFORE UPDATE OF text_hash ON messages WHEN old.text_hash<>new.text_hash BEGIN
    DELETE FROM profile_cards WHERE id IN (SELECT card_id FROM card_evidence WHERE message_id=old.id);
END;
CREATE TRIGGER card_epoch_update AFTER UPDATE OF epoch ON data_epochs BEGIN
    DELETE FROM profile_cards WHERE chat_id=new.chat_id AND target_id=new.target_id;
    DELETE FROM profile_watermarks WHERE chat_id=new.chat_id AND target_id=new.target_id;
END;
CREATE TRIGGER card_epoch_insert AFTER INSERT ON data_epochs BEGIN
    DELETE FROM profile_cards WHERE chat_id=new.chat_id AND target_id=new.target_id;
    DELETE FROM profile_watermarks WHERE chat_id=new.chat_id AND target_id=new.target_id;
END;
