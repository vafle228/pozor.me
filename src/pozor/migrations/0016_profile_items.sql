CREATE TABLE profile_items (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'observation' CHECK(kind IN ('hook','dossier','observation')),
    provenance TEXT NOT NULL DEFAULT 'legacy' CHECK(provenance IN ('legacy','reviewed','automatic')),
    topic TEXT NOT NULL,
    summary TEXT NOT NULL,
    attribution TEXT NOT NULL CHECK(attribution IN ('observed_self','imported_quote','third_party')),
    source_kind TEXT NOT NULL,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    episodes INTEGER NOT NULL CHECK(episodes>0),
    excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0,1)),
    quote TEXT,
    why_funny TEXT,
    hook_type TEXT,
    heat INTEGER CHECK(heat BETWEEN 1 AND 3)
);
CREATE INDEX profile_item_scope ON profile_items(chat_id,target_id,kind);
CREATE TABLE profile_item_sources (
    card_id INTEGER NOT NULL REFERENCES profile_items(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_text_hash TEXT NOT NULL,
    PRIMARY KEY(card_id,message_id)
);
INSERT INTO profile_items(id,chat_id,target_id,kind,provenance,topic,summary,attribution,
    source_kind,first_at,last_at,episodes,excluded)
SELECT c.id,c.chat_id,c.target_id,CASE WHEN c.profile_version=4 THEN 'dossier' ELSE 'observation' END,
    CASE WHEN c.profile_version=2 THEN 'reviewed' ELSE 'automatic' END,
    c.topic,c.summary,c.attribution,c.kind,c.first_at,c.last_at,c.episodes,c.excluded
FROM profile_cards c JOIN members member ON member.chat_id=c.chat_id AND member.user_id=c.target_id
WHERE c.profile_version IN (2,3,4) AND member.consent=1 AND member.opt_out=0
AND EXISTS (SELECT 1 FROM card_evidence ce WHERE ce.card_id=c.id)
AND NOT EXISTS (SELECT 1 FROM card_evidence ce JOIN messages m ON m.id=ce.message_id
    WHERE ce.card_id=c.id AND (ce.source_text_hash<>m.text_hash OR NOT EXISTS
        (SELECT 1 FROM v_entitled_messages e WHERE e.message_id=m.id AND e.target_id=c.target_id
         AND e.scope_chat_id=c.chat_id AND e.valid_until>julianday('now'))));
INSERT INTO profile_item_sources SELECT ce.* FROM card_evidence ce JOIN profile_items p ON p.id=ce.card_id;
DROP TRIGGER card_source_delete;
DROP TRIGGER card_source_edit;
DROP TRIGGER card_epoch_update;
DROP TRIGGER card_epoch_insert;
DROP TABLE card_evidence;
DROP TABLE profile_cards;
CREATE TRIGGER profile_source_delete BEFORE DELETE ON messages BEGIN
    DELETE FROM profile_items WHERE id IN (SELECT card_id FROM profile_item_sources WHERE message_id=old.id);
END;
CREATE TRIGGER profile_source_edit BEFORE UPDATE OF text_hash ON messages WHEN old.text_hash<>new.text_hash BEGIN
    DELETE FROM profile_items WHERE id IN (SELECT card_id FROM profile_item_sources WHERE message_id=old.id);
END;
CREATE TRIGGER profile_epoch_update AFTER UPDATE OF epoch ON data_epochs BEGIN
    DELETE FROM profile_items WHERE chat_id=new.chat_id AND target_id=new.target_id;
    DELETE FROM profile_watermarks WHERE chat_id=new.chat_id AND target_id=new.target_id;
END;
CREATE TRIGGER profile_epoch_insert AFTER INSERT ON data_epochs BEGIN
    DELETE FROM profile_items WHERE chat_id=new.chat_id AND target_id=new.target_id;
    DELETE FROM profile_watermarks WHERE chat_id=new.chat_id AND target_id=new.target_id;
END;
