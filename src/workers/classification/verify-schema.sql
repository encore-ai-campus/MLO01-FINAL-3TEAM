-- Schema DDL is installed by an operator. The runtime login only checks the contract.
SELECT request_id, event_id, model_version, assignment_checked_at, uses_customer_features, feature_snapshot_id FROM voc.classification_job LIMIT 0;
SELECT assigned_route, assigned_version, served_version, outcome, http_ms, cost_krw FROM voc.classification_attempt LIMIT 0;
SELECT company_id, scope_key, stable_version, candidate_version, configured_percent, failures, first_failure_at, tripped_at, reason FROM voc.classification_rollout LIMIT 0;
DO $$
BEGIN
    IF NOT has_table_privilege(current_user,'voc.classification_rollout','INSERT')
       OR NOT has_table_privilege(current_user,'voc.classification_rollout','UPDATE') THEN
        RAISE EXCEPTION 'The worker requires INSERT and UPDATE on voc.classification_rollout';
    END IF;
END $$;
SELECT ticket_id, analysis_kind, request_id FROM voc.classification_projection LIMIT 0;
SELECT domain_id, label, category_id FROM voc.classification_category_map LIMIT 0;
SELECT company_id, event_id, queued_at, published_at FROM voc.ticket_analysis_outbox LIMIT 0;
SELECT event_id, body_version, schema_version, message_kind FROM voc.silver_message LIMIT 0;
SELECT event_id, body_version, preprocess_version, model_text FROM voc.model_text_history LIMIT 0;
SELECT ticket_id, is_category_manual, sentiment_status, category_status FROM public.ticket LIMIT 0;
