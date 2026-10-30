CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    thread_id INTEGER NOT NULL DEFAULT 0,
    target_id INTEGER NOT NULL,
    initiator_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'closed')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    turn_count INTEGER NOT NULL DEFAULT 0 CHECK (turn_count BETWEEN 0 AND 4),
    created_at INTEGER NOT NULL,
    last_sent_at INTEGER,
    closed_reason TEXT,
    trigger_message_id INTEGER NOT NULL,
    UNIQUE (chat_id, trigger_message_id)
);
CREATE UNIQUE INDEX sessions_one_active_per_scope
    ON sessions(chat_id, thread_id) WHERE state = 'active';
CREATE INDEX sessions_target_cooldown ON sessions(chat_id, target_id, created_at);

CREATE TABLE command_receipts (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    result_text TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE command_cooldowns (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('target', 'initiator')),
    until_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id, kind)
);
