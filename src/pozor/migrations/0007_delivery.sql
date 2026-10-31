CREATE TABLE turns (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_no INTEGER NOT NULL CHECK(turn_no BETWEEN 1 AND 4),
    revision INTEGER NOT NULL,
    evidence_epoch INTEGER,
    deadline REAL,
    status TEXT NOT NULL CHECK(status IN
        ('pending','queued','generating','ready','sending','sent','cancelled','failed','unknown')),
    text TEXT,
    evidence_ids TEXT,
    context_hash TEXT,
    token_count INTEGER,
    PRIMARY KEY(session_id, turn_no)
);
CREATE TABLE turn_inputs (
    session_id TEXT NOT NULL,
    turn_no INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY(session_id, turn_no, source_message_id),
    FOREIGN KEY(session_id,turn_no) REFERENCES turns(session_id,turn_no) ON DELETE CASCADE
);
CREATE TABLE outbox (
    session_id TEXT NOT NULL,
    turn_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ready','sending','sent','unknown','cancelled')),
    next_attempt_at REAL NOT NULL,
    telegram_message_id INTEGER,
    error_code TEXT,
    PRIMARY KEY(session_id, turn_no),
    FOREIGN KEY(session_id,turn_no) REFERENCES turns(session_id,turn_no) ON DELETE CASCADE
);
CREATE TABLE delivery_limits (
    scope TEXT PRIMARY KEY,
    next_at REAL NOT NULL
);
CREATE TABLE interaction_receipts (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY(chat_id,message_id)
);
