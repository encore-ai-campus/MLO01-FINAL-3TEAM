-- Schema DDL is installed by an operator. The runtime login only checks the contract.
SELECT request_id, event_id, model_version, assignment_checked_at FROM voc.classification_job LIMIT 0;
SELECT served_version, outcome FROM voc.classification_attempt LIMIT 0;
SELECT ticket_uuid, analysis_kind, request_id FROM voc.classification_projection LIMIT 0;
SELECT domain_uuid, label, category_uuid FROM voc.classification_category_map LIMIT 0;
SELECT company_uuid, event_id, queued_at, published_at FROM voc.ticket_analysis_outbox LIMIT 0;
SELECT event_id, body_version, schema_version, message_kind FROM voc.silver_messages LIMIT 0;
SELECT event_id, body_version, preprocess_version, model_text FROM voc.model_text_history LIMIT 0;
SELECT ticket_uuid, category_manually_set, sentiment_status, category_status FROM public.ticket LIMIT 0;
