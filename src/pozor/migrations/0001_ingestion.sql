CREATE TABLE chats (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    policy_version INTEGER NOT NULL DEFAULT 1 CHECK (policy_version > 0),
    retention_days INTEGER NOT NULL DEFAULT 30 CHECK (retention_days > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE members (
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    display_name TEXT,
    current_username TEXT,
    last_seen TEXT,
    consent INTEGER NOT NULL DEFAULT 0 CHECK (consent IN (0, 1)),
    consent_version INTEGER NOT NULL DEFAULT 1 CHECK (consent_version > 0),
    opt_out INTEGER NOT NULL DEFAULT 0 CHECK (opt_out IN (0, 1)),
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE aliases (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL COLLATE NOCASE,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source TEXT NOT NULL,
    FOREIGN KEY (chat_id, user_id) REFERENCES members(chat_id, user_id) ON DELETE CASCADE,
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX aliases_lookup ON aliases(chat_id, username, valid_from, valid_to);

CREATE TABLE inbox (
    update_id INTEGER PRIMARY KEY,
    normalized_payload TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    processed_at TEXT
);
CREATE INDEX inbox_pending ON inbox(received_at, update_id) WHERE processed_at IS NULL;

CREATE TABLE poller_state (
    bot_id INTEGER PRIMARY KEY,
    next_offset INTEGER NOT NULL CHECK (next_offset >= 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    scope_chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    source_namespace TEXT NOT NULL DEFAULT 'live',
    source_message_id INTEGER NOT NULL,
    thread_id INTEGER,
    author_id INTEGER,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    edited_at TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('live', 'export', 'manual')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source_namespace, source_message_id)
);
CREATE INDEX messages_author_time ON messages(scope_chat_id, author_id, occurred_at);
CREATE INDEX messages_thread_time ON messages(scope_chat_id, thread_id, occurred_at);

CREATE TABLE message_subjects (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('self', 'mention', 'reply', 'context', 'manual')),
    attribution TEXT NOT NULL CHECK (
        attribution IN ('observed_self', 'third_party', 'manual_claim', 'imported_quote')
    ),
    PRIMARY KEY (message_id, target_id, relation)
);
CREATE INDEX message_subjects_target ON message_subjects(target_id, message_id);

CREATE TABLE data_epochs (
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL,
    epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    PRIMARY KEY (chat_id, target_id)
);

CREATE VIRTUAL TABLE messages_fts USING fts5(text, content='messages', content_rowid='id');
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER messages_fts_update AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
