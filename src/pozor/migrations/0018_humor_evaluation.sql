CREATE TABLE humor_evaluations (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    message_id INTEGER,
    left_variant TEXT NOT NULL,
    right_variant TEXT NOT NULL,
    case_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE humor_votes (
    evaluation_id INTEGER NOT NULL REFERENCES humor_evaluations(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    choice TEXT NOT NULL CHECK(choice IN ('A','B','tie')),
    PRIMARY KEY(evaluation_id,user_id)
);
