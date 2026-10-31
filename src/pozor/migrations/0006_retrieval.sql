-- Unknown old reply links remain NULL; never infer them from text.
ALTER TABLE messages ADD COLUMN reply_source_id INTEGER CHECK (reply_source_id > 0);
CREATE INDEX subjects_retrieval ON message_subjects(target_id, relation, message_id);
