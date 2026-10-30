CREATE TABLE source_chats (
    namespace TEXT PRIMARY KEY,
    export_chat_id INTEGER NOT NULL,
    destination_chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    mapping_status TEXT NOT NULL CHECK (mapping_status IN ('approved', 'blocked')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE imports (
    batch_id TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('staged', 'active', 'rolled_back', 'expired')),
    operator_id INTEGER,
    target_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    source_namespace TEXT NOT NULL,
    source_file TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT NOT NULL,
    FOREIGN KEY(source_namespace) REFERENCES source_chats(namespace)
);
CREATE INDEX imports_expiry ON imports(status, expires_at);

CREATE TABLE import_items (
    batch_id TEXT NOT NULL REFERENCES imports(batch_id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK (relation IN ('self', 'mention', 'reply', 'context')),
    selected INTEGER NOT NULL DEFAULT 1 CHECK (selected IN (0, 1)),
    PRIMARY KEY(batch_id, message_id)
);

CREATE TABLE manual_items (
    item_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES imports(batch_id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('operator_note', 'pasted_quote')),
    source_label TEXT NOT NULL,
    source_url TEXT,
    occurred_at TEXT,
    text_hash TEXT NOT NULL
);

CREATE INDEX manual_items_target ON manual_items(batch_id, target_id);
