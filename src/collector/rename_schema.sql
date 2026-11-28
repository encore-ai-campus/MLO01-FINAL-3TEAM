-- Apply before the schema definitions. A rerun sees only the new names.
DO $migration$
DECLARE
    target record;
    relation regclass;
    rename text;
    old_name text;
    new_name text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('voc-schema-names'));

    -- Let the v4 initializer rebuild the active Silver table from Bronze.
    -- Keep the archived v1 layout and rows available under the historical name.
    IF EXISTS (SELECT FROM pg_attribute WHERE attrelid=to_regclass('voc.silver_messages')
               AND attname='classification_status' AND NOT attisdropped) THEN
        ALTER TABLE voc.silver_messages RENAME TO silver_messages_v1;
    END IF;

    FOR target IN SELECT * FROM (VALUES
        ('bronze_messages','bronze_message'),
        ('silver_messages','silver_message'),
        ('review_events','review_event'),
        ('gold_label','feature_snapshot_label')
    ) AS names(old_name,new_name)
    LOOP
        IF to_regclass('voc.' || target.old_name) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE voc.%I RENAME TO %I', target.old_name, target.new_name);
        END IF;
    END LOOP;

    FOR target IN SELECT * FROM (VALUES
        ('public.category', 'TABLE', ARRAY['category_uuid:category_id','domain_uuid:domain_id']),
        ('public.channel', 'TABLE', ARRAY['channel_uuid:channel_id','company_uuid:company_id']),
        ('public.chat', 'TABLE', ARRAY['chat_uuid:chat_id','company_uuid:company_id','customer_uuid:customer_id','domain_uuid:domain_id','channel_uuid:channel_id','assignee_uuid:assignee_id','category_uuid:category_id']),
        ('public.chat_message', 'TABLE', ARRAY['chat_message_uuid:chat_message_id','chat_uuid:chat_id','staff_user_uuid:staff_user_id']),
        ('public.collab_request', 'TABLE', ARRAY['collab_request_uuid:collab_request_id','ticket_uuid:ticket_id','requester_uuid:requester_id','recipient_uuid:recipient_id','review_channel_uuid:review_channel_id']),
        ('public.collab_request_attachment', 'TABLE', ARRAY['collab_request_attachment_uuid:collab_request_attachment_id','collab_request_uuid:collab_request_id']),
        ('public.collab_review_reply', 'TABLE', ARRAY['collab_review_reply_uuid:collab_review_reply_id','collab_request_uuid:collab_request_id','author_uuid:author_id']),
        ('public.company', 'TABLE', ARRAY['company_uuid:company_id','domain_uuid:domain_id']),
        ('public.customer', 'TABLE', ARRAY['customer_uuid:customer_id','company_uuid:company_id']),
        ('public.domain', 'TABLE', ARRAY['domain_uuid:domain_id']),
        ('public.event_type', 'TABLE', ARRAY['event_type_uuid:event_type_id']),
        ('public.followup_contact', 'TABLE', ARRAY['contact_uuid:contact_id','company_uuid:company_id','ticket_uuid:ticket_id','chat_uuid:chat_id','created_by_uuid:created_by_id','assignee_uuid:assignee_id','completed_by_uuid:completed_by_id']),
        ('public.followup_track', 'TABLE', ARRAY['company_uuid:company_id','staff_user_uuid:staff_user_id','ticket_uuid:ticket_id']),
        ('public.permission', 'TABLE', ARRAY['permission_uuid:permission_id']),
        ('public.staff_permission', 'TABLE', ARRAY['staff_user_uuid:staff_user_id','permission_uuid:permission_id']),
        ('public.staff_session', 'TABLE', ARRAY['staff_user_uuid:staff_user_id']),
        ('public.staff_user', 'TABLE', ARRAY['staff_user_uuid:staff_user_id','company_uuid:company_id','team_uuid:team_id','display_preferences:display_preferences_json']),
        ('public.team', 'TABLE', ARRAY['team_uuid:team_id','company_uuid:company_id']),
        ('public.ticket', 'TABLE', ARRAY['ticket_uuid:ticket_id','company_uuid:company_id','domain_uuid:domain_id','channel_uuid:channel_id','customer_uuid:customer_id','assigned_team_uuid:assigned_team_id','assignee_uuid:assignee_id','category_uuid:category_id','category_manually_set:is_category_manual']),
        ('public.ticket_category_team_route', 'TABLE', ARRAY['company_uuid:company_id','domain_uuid:domain_id','category_uuid:category_id','team_uuid:team_id']),
        ('public.ticket_duplicate_candidate', 'TABLE', ARRAY['ticket_uuid:ticket_id','candidate_ticket_uuid:candidate_ticket_id']),
        ('public.ticket_event', 'TABLE', ARRAY['ticket_event_uuid:ticket_event_id','ticket_uuid:ticket_id','event_type_uuid:event_type_id','actor_uuid:actor_id','recipient_uuid:recipient_id','collab_request_uuid:collab_request_id','details:detail_json']),
        ('public.ticket_message', 'TABLE', ARRAY['ticket_message_uuid:ticket_message_id','ticket_uuid:ticket_id','staff_user_uuid:staff_user_id','reviewed_feature_uuid:reviewed_feature_id']),
        ('public.ticket_message_attachment', 'TABLE', ARRAY['attachment_uuid:attachment_id','ticket_message_uuid:ticket_message_id']),
        ('public.ticket_review_channel', 'TABLE', ARRAY['review_channel_uuid:review_channel_id','company_uuid:company_id']),
        ('public.ticket_review_channel_member', 'TABLE', ARRAY['company_uuid:company_id','review_channel_uuid:review_channel_id','staff_user_uuid:staff_user_id']),
        ('public.visible_ticket', 'VIEW', ARRAY['ticket_uuid:ticket_id','company_uuid:company_id','domain_uuid:domain_id','channel_uuid:channel_id','customer_uuid:customer_id','assigned_team_uuid:assigned_team_id','assignee_uuid:assignee_id','category_uuid:category_id','category_manually_set:is_category_manual']),
        ('voc.bronze_message', 'TABLE', ARRAY['company_id:source_company_id','channel:source_channel_type','connection_id:source_connection_id','context:context_json','raw_payload:raw_payload_json']),
        ('voc.classification_category_map', 'TABLE', ARRAY['domain_uuid:domain_id','category_uuid:category_id']),
        ('voc.classification_job', 'TABLE', ARRAY['company_uuid:company_id','feature_uuid:feature_snapshot_id']),
        ('voc.classification_projection', 'TABLE', ARRAY['ticket_uuid:ticket_id']),
        ('voc.classification_rollout', 'TABLE', ARRAY['company_uuid:company_id']),
        ('voc.counselor_review', 'TABLE', ARRAY['review_uuid:review_id','feature_uuid:feature_snapshot_id','reviewer_uuid:reviewer_id','category_uuid:category_id']),
        ('voc.external_model_evaluation', 'TABLE', ARRAY['company_uuid:company_id','eval_dataset_uuid:eval_dataset_id']),
        ('voc.feature_snapshot', 'TABLE', ARRAY['feature_uuid:feature_snapshot_id','company_uuid:company_id','domain_uuid:domain_id','ticket_uuid:ticket_id','avg_first_reply_7d_seconds:avg_first_reply_7d_ms']),
        ('voc.feature_snapshot_label', 'TABLE', ARRAY['gold_label_uuid:feature_snapshot_label_id','feature_uuid:feature_snapshot_id','category_uuid:category_id','reviewer_uuid:reviewer_id','completion_message_uuid:completion_message_id']),
        ('voc.model_review_benchmark', 'TABLE', ARRAY['company_uuid:company_id']),
        ('voc.model_review_company_domains', 'TABLE', ARRAY['company_uuid:company_id']),
        ('voc.model_review_domain', 'TABLE', ARRAY['domain_uuid:domain_id']),
        ('voc.model_review_policy', 'TABLE', ARRAY['company_uuid:company_id']),
        ('voc.model_text_history', 'TABLE', ARRAY['company_uuid:company_id']),
        ('voc.retraining_dispatch', 'TABLE', ARRAY['company_uuid:company_id']),
        ('voc.retraining_policy', 'TABLE', ARRAY['company_uuid:company_id','enabled:is_enabled']),
        ('voc.silver_message', 'TABLE', ARRAY['company_id:source_company_id','channel:source_channel_type','connection_id:source_connection_id','source_metadata:source_metadata_json']),
        ('voc.ticket_analysis_outbox', 'TABLE', ARRAY['company_uuid:company_id']),
        ('voc.ticket_number_counter', 'TABLE', ARRAY['company_uuid:company_id']),
        ('voc.ticket_source_connection', 'TABLE', ARRAY['company_id:source_company_id','channel:source_channel_type','connection_id:source_connection_id','company_uuid:company_id','channel_uuid:channel_id','auto_customer_from:auto_customer_start_at']),
        ('voc.training_capture', 'TABLE', ARRAY['singleton:is_singleton']),
        ('voc.training_dataset', 'TABLE', ARRAY['dataset_uuid:dataset_id','company_uuid:company_id']),
        ('voc.training_dataset_item', 'TABLE', ARRAY['dataset_uuid:dataset_id','feature_uuid:feature_snapshot_id','sentiment_label_uuid:sentiment_label_id','category_label_uuid:category_label_id']),
        ('voc.training_run', 'TABLE', ARRAY['train_dataset_uuid:train_dataset_id','eval_dataset_uuid:eval_dataset_id','label_cutoff:label_cutoff_at']),
        ('voc.training_stop_request', 'TABLE', ARRAY['company_uuid:company_id']),
        ('voc.training_ticket_baseline', 'TABLE', ARRAY['ticket_uuid:ticket_id'])
    ) AS relations(name,kind,columns)
    UNION ALL
    -- Settings tables were created in the installer's first schema.
    SELECT schema_name || '.' || settings.name, 'TABLE', settings.columns
    FROM (VALUES
        ('messenger_destination', ARRAY['company_uuid:company_id','team_uuid:team_id','staff_user_uuid:staff_user_id','channel:messenger_channel']),
        ('quick_reply', ARRAY['reply_uuid:reply_id','company_uuid:company_id','owner_uuid:owner_id','team_uuid:team_id','domain_uuid:domain_id','category_uuid:category_id']),
        ('staff_assignment_policy', ARRAY['company_uuid:company_id','staff_user_uuid:staff_user_id','enabled:is_enabled'])
    ) AS settings(name,columns)
    CROSS JOIN (VALUES ('voc'), ('public')) AS schemas(schema_name)
    LOOP
        relation := to_regclass(target.name);
        IF relation IS NULL THEN
            CONTINUE;
        END IF;
        FOREACH rename IN ARRAY target.columns LOOP
            old_name := split_part(rename, ':', 1);
            new_name := split_part(rename, ':', 2);
            IF EXISTS (SELECT FROM pg_attribute WHERE attrelid=relation
                       AND attname=old_name AND NOT attisdropped) THEN
                -- The source company text column moves first. After migration,
                -- company_id in the same table is the internal UUID.
                IF old_name = 'company_id' AND NOT EXISTS (
                    SELECT FROM pg_attribute WHERE attrelid=relation AND attname=old_name
                    AND atttypid='text'::regtype AND NOT attisdropped
                ) THEN
                    CONTINUE;
                END IF;
                EXECUTE format('ALTER %s %s RENAME COLUMN %I TO %I',
                               target.kind, relation, old_name, new_name);
                IF old_name = 'avg_first_reply_7d_seconds' THEN
                    UPDATE voc.feature_snapshot
                    SET avg_first_reply_7d_ms=avg_first_reply_7d_ms*1000
                    WHERE avg_first_reply_7d_ms IS NOT NULL;
                END IF;
            END IF;
        END LOOP;
    END LOOP;
END
$migration$;
