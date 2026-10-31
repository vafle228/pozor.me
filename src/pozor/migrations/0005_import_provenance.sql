-- Invalidate derived evidence before correcting export-only provenance.
INSERT INTO data_epochs(chat_id,target_id,epoch)
SELECT DISTINCT m.scope_chat_id,s.target_id,1
FROM message_subjects s JOIN messages m ON m.id=s.message_id
WHERE m.origin='export' AND s.attribution='observed_self'
ON CONFLICT(chat_id,target_id) DO UPDATE SET epoch=epoch+1;

UPDATE message_subjects SET attribution='imported_quote'
WHERE attribution='observed_self'
  AND message_id IN (SELECT id FROM messages WHERE origin='export');
