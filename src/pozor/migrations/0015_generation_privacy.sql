-- A journal may contain other people's tail context. Invalidate the entire
-- chat on privacy/source changes; deleting the target alone is insufficient.
CREATE TRIGGER generation_epoch_update AFTER UPDATE OF epoch ON data_epochs BEGIN
    DELETE FROM generations WHERE chat_id=new.chat_id;
END;
CREATE TRIGGER generation_epoch_insert AFTER INSERT ON data_epochs BEGIN
    DELETE FROM generations WHERE chat_id=new.chat_id;
END;
CREATE TRIGGER generation_source_delete BEFORE DELETE ON messages BEGIN
    DELETE FROM generations WHERE chat_id=old.scope_chat_id;
END;
CREATE TRIGGER generation_source_edit BEFORE UPDATE OF text_hash ON messages
WHEN old.text_hash<>new.text_hash BEGIN
    DELETE FROM generations WHERE chat_id=old.scope_chat_id;
END;
