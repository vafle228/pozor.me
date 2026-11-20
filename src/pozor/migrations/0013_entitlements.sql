-- One source of truth for source retention. Consumers may compare valid_until
-- with their injected clock, so expiry checks remain deterministic in tests.
CREATE TABLE bot_identities (user_id INTEGER PRIMARY KEY);
ALTER TABLE messages ADD COLUMN author_is_bot INTEGER NOT NULL DEFAULT 0 CHECK(author_is_bot IN (0,1));

CREATE VIEW v_entitled_messages AS
WITH grants AS (
    SELECT m.scope_chat_id, s.target_id, m.id AS message_id, s.relation, s.attribution,
           julianday(m.occurred_at, '+' || c.retention_days || ' days') AS valid_until
    FROM messages m JOIN message_subjects s ON s.message_id=m.id
    JOIN chats c ON c.chat_id=m.scope_chat_id WHERE m.origin='live'
    UNION ALL
    SELECT m.scope_chat_id, i.target_id, m.id, ii.relation, s.attribution,
           julianday(i.retained_until)
    FROM messages m JOIN import_items ii ON ii.message_id=m.id
    JOIN imports i USING(batch_id)
    JOIN message_subjects s ON s.message_id=m.id AND s.target_id=i.target_id AND s.relation=ii.relation
    WHERE i.status='active' AND i.chat_id=m.scope_chat_id
    UNION ALL
    -- The live tail is context, never a claim about another participant.
    SELECT m.scope_chat_id, target.user_id, m.id, 'context', 'third_party',
           julianday(m.occurred_at, '+' || c.retention_days || ' days')
    FROM messages m JOIN chats c ON c.chat_id=m.scope_chat_id
    JOIN members author ON author.chat_id=c.chat_id AND author.user_id=m.author_id
    JOIN members target ON target.chat_id=c.chat_id AND target.user_id<>m.author_id
    WHERE m.origin='live' AND author.consent=1 AND author.opt_out=0
      AND target.consent=1 AND target.opt_out=0
      AND NOT EXISTS (SELECT 1 FROM forgotten_members f WHERE f.chat_id=c.chat_id
                      AND f.user_id IN (author.user_id,target.user_id))
)
SELECT g.scope_chat_id,g.target_id,g.message_id,g.relation,g.attribution,max(g.valid_until) AS valid_until
FROM grants g JOIN messages m ON m.id=g.message_id
WHERE m.author_is_bot=0 AND NOT EXISTS (SELECT 1 FROM bot_identities b WHERE b.user_id=m.author_id)
GROUP BY g.scope_chat_id,g.target_id,g.message_id,g.relation,g.attribution;
