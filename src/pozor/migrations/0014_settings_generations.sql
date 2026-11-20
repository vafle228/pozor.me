CREATE TABLE settings_overrides (
    name TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE settings_audit (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    previous_json TEXT,
    value_json TEXT,
    updated_by TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE generations (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    turn_no INTEGER,
    mode TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    code_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    model TEXT NOT NULL,
    params_json TEXT NOT NULL,
    request_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    selected_json TEXT NOT NULL,
    judge_reason TEXT,
    latency_ms REAL NOT NULL,
    fallback_reason TEXT,
    created_at REAL NOT NULL,
    UNIQUE(session_id,turn_no)
);
CREATE INDEX generations_recent ON generations(chat_id,created_at);
CREATE INDEX generations_target ON generations(chat_id,target_id,created_at);
CREATE TABLE joke_ratings (
    generation_id INTEGER NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK(rating IN (-1,0,1)),
    updated_at REAL NOT NULL,
    PRIMARY KEY(generation_id,user_id)
);
ALTER TABLE members ADD COLUMN roast_level INTEGER NOT NULL DEFAULT 2 CHECK(roast_level BETWEEN 1 AND 3);
ALTER TABLE sessions ADD COLUMN roast_level INTEGER NOT NULL DEFAULT 2 CHECK(roast_level BETWEEN 1 AND 3);
